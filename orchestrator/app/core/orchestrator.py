import asyncio
import json
import logging
import time

import httpx

from app.models.schemas import TaskRequest, TaskResponse, OrchestratorDecision, CompetitionRequest
from app.services.role_service import resolve_roles
from app.services.ollama_service import generate, select_model
from app.services.prompt_service import build_system_prompt
from app.services.intent_service import classify_intent
from app.services.workflow_service import route_workflow, WorkflowType
from app.agents.base import AgentInput, AgentOutput
from app.agents.research import ResearchAgent
from app.agents.analysis import AnalysisAgent
from app.agents.planning import PlanningAgent
from app.agents.critic import CriticAgent
from app.agents.synthesizer import SynthesizerAgent
from app.evaluation.scorer import evaluate_output
from app.evaluation.adjudicator import adjudicate, AdjudicationStrategy
from app.memory.semantic import search_semantic

logger = logging.getLogger("pai.orchestrator")

# Agent registry
_agents = {
    "research": ResearchAgent(),
    "analysis": AnalysisAgent(),
    "planning": PlanningAgent(),
    "critic": CriticAgent(),
    "synthesizer": SynthesizerAgent(),
}

# Which agents compete for each workflow type
_COMPETITION_AGENTS: dict[WorkflowType, list[str]] = {
    WorkflowType.multi_agent_competition: ["research", "analysis"],
    WorkflowType.agent_research: ["research"],
    WorkflowType.agent_analysis: ["analysis"],
    WorkflowType.agent_planning: ["planning"],
}


def _build_role_dict(roles):
    """Build a role context dict from resolved roles."""
    return {
        "role": roles.primary.role.value,
        "domain": roles.primary.domain.value,
        "goals": roles.primary.goals,
        "preferences": roles.primary.preferences,
        "constraints": roles.primary.constraints,
    }


async def _run_agent(
    agent_name: str,
    request: TaskRequest,
    roles,
    http_client: httpx.AsyncClient,
    retrieved_context: list[str] | None = None,
) -> AgentOutput | None:
    """Run a named agent: build its prompt, call the model, parse the result."""
    agent = _agents.get(agent_name)
    if not agent:
        return None

    agent_input = AgentInput(
        request_id=request.request_id,
        task=request.input,
        role_context=_build_role_dict(roles),
        retrieved_context=retrieved_context or [],
    )

    system_prompt, user_prompt = await agent.build_prompt(agent_input)
    model = select_model(request.input)

    raw_response = await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
        http_client=http_client,
    )

    return agent.parse_response(raw_response, agent_input)


async def _run_competition(
    request: TaskRequest,
    roles,
    http_client: httpx.AsyncClient,
    agent_names: list[str],
    retrieved_context: list[str] | None = None,
    strategy: AdjudicationStrategy = AdjudicationStrategy.best_score,
) -> tuple[dict | None, str]:
    """
    Multi-agent competition:
    1. Spawn agents in parallel
    2. Evaluate all outputs
    3. Adjudicate
    4. Optionally synthesize
    """
    # 1. Run agents in parallel
    tasks = [
        _run_agent(name, request, roles, http_client, retrieved_context)
        for name in agent_names
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect successful outputs
    outputs: list[AgentOutput] = []
    for r in results:
        if isinstance(r, AgentOutput):
            outputs.append(r)
        elif isinstance(r, Exception):
            logger.error("agent_failed", extra={"error": str(r)})

    if not outputs:
        return None, "All agents failed to produce output."

    if len(outputs) == 1:
        return outputs[0].metadata or None, outputs[0].result

    # 2. Evaluate all outputs
    scores = [evaluate_output(o, request.input) for o in outputs]

    # 3. Adjudicate
    result = adjudicate(outputs, scores, strategy=strategy)

    logger.info(
        "competition_result",
        extra={
            "strategy": result.strategy.value,
            "winner": result.winner,
            "should_synthesize": result.should_synthesize,
            "agent_scores": {s.agent_name: s.total for s in result.scores},
        },
    )

    # 4. Optionally synthesize
    if result.should_synthesize and len(result.selected_outputs) > 1:
        synth_output = await _run_synthesis(
            request, roles, http_client, result.selected_outputs
        )
        if synth_output:
            return synth_output.metadata or None, synth_output.result

    # Return best single output
    best = result.selected_outputs[0]
    return best.metadata or None, best.result


async def _run_synthesis(
    request: TaskRequest,
    roles,
    http_client: httpx.AsyncClient,
    outputs: list[AgentOutput],
) -> AgentOutput | None:
    """Run the synthesizer agent to combine multiple agent outputs."""
    synthesizer = _agents["synthesizer"]

    # Pass agent outputs as retrieved context for synthesis
    context = [
        f"[{o.agent_name}] (confidence: {o.confidence})\n{o.result}"
        for o in outputs
    ]

    agent_input = AgentInput(
        request_id=request.request_id,
        task=request.input,
        role_context=_build_role_dict(roles),
        retrieved_context=context,
    )

    system_prompt, user_prompt = await synthesizer.build_prompt(agent_input)
    model = select_model(request.input)

    raw_response = await generate(
        prompt=user_prompt,
        system_prompt=system_prompt,
        model=model,
        http_client=http_client,
    )

    return synthesizer.parse_response(raw_response, agent_input)


async def _run_with_critic(
    request: TaskRequest,
    roles,
    http_client: httpx.AsyncClient,
    primary_output: AgentOutput,
) -> AgentOutput:
    """Run the critic agent against a primary output, then return the original with critique metadata."""
    critic_output = await _run_agent(
        "critic", request, roles, http_client,
        retrieved_context=[primary_output.result],
    )
    if critic_output:
        # Attach critique to original output's metadata
        primary_output.metadata["critique"] = {
            "strengths": critic_output.metadata.get("strengths", []),
            "weaknesses": critic_output.metadata.get("weaknesses", []),
            "risks": critic_output.metadata.get("risks", []),
            "improvements": critic_output.metadata.get("improvements", []),
        }
    return primary_output


async def handle_task(
    request: TaskRequest,
    http_client: httpx.AsyncClient,
) -> TaskResponse:
    """
    Central orchestration pipeline:
      Request → Intent → Role Resolution → Workflow Routing → Execution → Structured Output
    """
    start = time.perf_counter()

    # 1. Classify intent
    intent = classify_intent(request.input)

    # 2. Resolve roles (primary + optional secondary)
    roles = await resolve_roles(request.role, request.secondary_role)

    # 3. Route to workflow
    workflow = route_workflow(intent)

    # 4. Select model
    model = select_model(request.input)

    # 5. Record decision
    decision = OrchestratorDecision(
        request_id=request.request_id,
        roles=roles,
        model=model,
        workflow=workflow.value,
    )
    logger.info(
        "orchestrator_decision",
        extra={
            "request_id": str(decision.request_id),
            "intent": intent.value,
            "workflow": workflow.value,
            "primary_role": roles.primary.role.value,
            "secondary_role": roles.secondary.role.value if roles.secondary else None,
            "domain": roles.primary.domain.value,
            "model": model,
        },
    )

    # 6. Execute workflow
    structured = None
    content = ""
    retrieved_context: list[str] = []

    if workflow == WorkflowType.multi_agent_competition:
        # Multi-agent: research + analysis compete, evaluate, adjudicate, optional synthesis
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        agent_names = _COMPETITION_AGENTS[WorkflowType.multi_agent_competition]
        structured, content = await _run_competition(
            request, roles, http_client, agent_names, retrieved_context,
            strategy=AdjudicationStrategy.synthesize,
        )

    elif workflow in (WorkflowType.agent_research, WorkflowType.agent_analysis, WorkflowType.agent_planning):
        # Single-agent workflow with optional critic review
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        agent_names = _COMPETITION_AGENTS.get(workflow, [])
        agent_name = agent_names[0] if agent_names else "research"

        agent_output = await _run_agent(
            agent_name, request, roles, http_client, retrieved_context
        )
        if agent_output:
            content = agent_output.result
            structured = agent_output.metadata or None
        else:
            raw_response = await _direct_generate(request, roles, model, http_client)
            structured, content = _parse_response(raw_response, request)

    elif workflow == WorkflowType.retrieval_augmented:
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        system_prompt = build_system_prompt(roles)
        if retrieved_context:
            context_block = "\n\nRelevant context:\n" + "\n---\n".join(retrieved_context)
            user_prompt = request.input + context_block
        else:
            user_prompt = request.input

        raw_response = await generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model,
            http_client=http_client,
        )
        structured, content = _parse_response(raw_response, request)

    elif workflow == WorkflowType.execution:
        raw_response = await _direct_generate(request, roles, model, http_client)
        structured, content = _parse_response(raw_response, request)

    else:
        # Direct response (default)
        raw_response = await _direct_generate(request, roles, model, http_client)
        structured, content = _parse_response(raw_response, request)

    duration_ms = (time.perf_counter() - start) * 1000

    return TaskResponse(
        request_id=request.request_id,
        role=roles.primary.role.value,
        secondary_role=roles.secondary.role.value if roles.secondary else None,
        domain=roles.primary.domain.value,
        model=model,
        content=content,
        structured_output=structured,
        duration_ms=round(duration_ms, 2),
        workflow=workflow.value,
        intent=intent.value,
    )


async def _direct_generate(request, roles, model, http_client):
    """Standard direct model call with role-aware prompt."""
    system_prompt = build_system_prompt(roles)
    return await generate(
        prompt=request.input,
        system_prompt=system_prompt,
        model=model,
        http_client=http_client,
    )


def _parse_response(raw_response: str, request: TaskRequest) -> tuple[dict | None, str]:
    """Parse raw LLM response into (structured_output, content)."""
    try:
        structured = json.loads(raw_response)
        content = str(structured.get("answer", raw_response))
        return structured, content
    except (json.JSONDecodeError, AttributeError):
        logger.warning(
            "structured_parse_failed",
            extra={"request_id": str(request.request_id)},
        )
        return None, raw_response


async def handle_competition(
    request: CompetitionRequest,
    http_client: httpx.AsyncClient,
) -> TaskResponse:
    """
    Explicit multi-agent competition endpoint.
    User selects agents and adjudication strategy.
    """
    start = time.perf_counter()

    roles = await resolve_roles(request.role, request.secondary_role)
    model = select_model(request.input)

    # Validate agent names
    agent_names = [a for a in request.agents if a in _agents]
    if len(agent_names) < 2:
        agent_names = ["research", "analysis"]

    # Map strategy string to enum
    try:
        strategy = AdjudicationStrategy(request.strategy)
    except ValueError:
        strategy = AdjudicationStrategy.best_score

    # Retrieve context
    results = await search_semantic(request.input, limit=3, http_client=http_client)
    retrieved_context = [r["content"] for r in results]

    structured, content = await _run_competition(
        request, roles, http_client, agent_names, retrieved_context,
        strategy=strategy,
    )

    duration_ms = (time.perf_counter() - start) * 1000

    return TaskResponse(
        request_id=request.request_id,
        role=roles.primary.role.value,
        secondary_role=roles.secondary.role.value if roles.secondary else None,
        domain=roles.primary.domain.value,
        model=model,
        content=content or "",
        structured_output=structured,
        duration_ms=round(duration_ms, 2),
        workflow="multi_agent_competition",
        intent="competition",
    )
