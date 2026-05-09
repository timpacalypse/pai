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
from app.services.workflow_service import route_workflow, WorkflowType, select_agents_for_task
from app.services.llm_intent_service import classify_chat_intent
from app.agents.base import AgentInput, AgentOutput
from app.agents.research import ResearchAgent
from app.agents.analysis import AnalysisAgent
from app.agents.planning import PlanningAgent
from app.agents.critic import CriticAgent
from app.agents.synthesizer import SynthesizerAgent
from app.evaluation.scorer import evaluate_output, evaluate_output_llm
from app.evaluation.adjudicator import adjudicate, AdjudicationStrategy
from app.memory.semantic import search_semantic
from app.services.quality_service import store_scores
from app.services.procedural_memory import record_outcome, lookup_proven_workflow

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

    # Look up active prompt override for this agent
    override = ""
    try:
        from app.services.learning_service import get_active_override_for_agent
        override = await get_active_override_for_agent(agent.name)
        if override:
            logger.info("learning_override_applied", extra={"agent": agent.name, "override_len": len(override)})
    except Exception:
        pass  # Don't let override lookup break task execution

    agent_input = AgentInput(
        request_id=request.request_id,
        task=request.input,
        role_context=_build_role_dict(roles),
        retrieved_context=retrieved_context or [],
        prompt_override=override,
    )

    system_prompt, user_prompt = await agent.build_prompt_with_override(agent_input)
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

    # 2. Evaluate all outputs (LLM-based with heuristic fallback)
    eval_tasks = [
        evaluate_output_llm(o, request.input, http_client)
        for o in outputs
    ]
    scores = await asyncio.gather(*eval_tasks)

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

    # 3b. Persist quality scores
    try:
        intent = await classify_intent(request.input)
        model = select_model(request.input)
        await store_scores(
            request_id=request.request_id,
            intent=intent.value,
            workflow="multi_agent_competition",
            model=model,
            scores=[s.model_dump() for s in result.scores],
            winner=result.winner,
        )
    except Exception as e:
        logger.warning("quality_store_failed", extra={"error": str(e)})

    # 3c. Record procedural memory
    try:
        intent_val = (await classify_intent(request.input)).value
        avg = sum(s.total for s in result.scores) / max(len(result.scores), 1)
        await record_outcome(intent_val, "multi_agent_competition", agent_names, avg)
    except Exception as e:
        logger.warning("procedural_record_failed: %s", str(e))

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
      Request → Auto-Infer Roles → Intent → Smart Agent Selection → Execution → Structured Output
    """
    start = time.perf_counter()

    # 1. Classify intent
    intent = await classify_intent(request.input)

    # 2. Resolve roles — LLM-inferred if not explicitly set
    if request.role:
        roles = await resolve_roles(request.role, request.secondary_role)
    else:
        classification = await classify_chat_intent(request.input, http_client)
        roles = await resolve_roles(classification["role"], None)

    # 3. Smart agent selection based on prompt + intent
    #    Check procedural memory first for proven patterns
    proven = None
    try:
        proven = await lookup_proven_workflow(intent.value)
    except Exception:
        pass

    if proven and proven["agents"]:
        selected_agents = [a for a in proven["agents"] if a in _agents]
        logger.info("procedural_routing", extra={
            "pattern": proven["workflow_name"],
            "success_rate": proven["success_rate"],
            "agents": selected_agents,
        })
    else:
        selected_agents = select_agents_for_task(request.input, intent)

    # 4. Determine workflow from agent selection
    if len(selected_agents) >= 2:
        workflow = WorkflowType.multi_agent_competition
    elif len(selected_agents) == 1:
        agent_workflow_map = {
            "research": WorkflowType.agent_research,
            "analysis": WorkflowType.agent_analysis,
            "planning": WorkflowType.agent_planning,
        }
        workflow = agent_workflow_map.get(selected_agents[0], WorkflowType.agent_research)
    else:
        workflow = route_workflow(intent)

    # 5. Select model
    model = select_model(request.input)

    # 6. Record decision
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
            "selected_agents": selected_agents,
            "primary_role": roles.primary.role.value,
            "secondary_role": roles.secondary.role.value if roles.secondary else None,
            "domain": roles.primary.domain.value,
            "model": model,
        },
    )

    # 7. Execute workflow
    structured = None
    content = ""
    retrieved_context: list[str] = []

    if workflow == WorkflowType.multi_agent_competition:
        # Multi-agent: all selected agents compete, evaluate, synthesize
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        structured, content = await _run_competition(
            request, roles, http_client, selected_agents, retrieved_context,
            strategy=AdjudicationStrategy.synthesize,
        )

    elif workflow in (WorkflowType.agent_research, WorkflowType.agent_analysis, WorkflowType.agent_planning):
        # Single-agent workflow
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        agent_name = selected_agents[0] if selected_agents else "research"
        agent_output = await _run_agent(
            agent_name, request, roles, http_client, retrieved_context
        )
        if agent_output:
            # Critic quality gate for high-stakes workflows (analysis, planning)
            if workflow in (WorkflowType.agent_analysis, WorkflowType.agent_planning):
                try:
                    agent_output = await _run_with_critic(
                        request, roles, http_client, agent_output
                    )
                except Exception as e:
                    logger.warning("critic_pass_failed", extra={"error": str(e)})

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

    else:
        # Direct response / execution (default)
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
        answer = structured.get("answer", raw_response)
        # If the answer is not a plain string, serialize it readably
        if isinstance(answer, (dict, list)):
            content = json.dumps(answer, indent=2)
        else:
            content = str(answer)
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
    User can optionally select agents and adjudication strategy,
    or let the system auto-select.
    """
    start = time.perf_counter()

    # Auto-infer roles if not specified
    if request.role:
        roles = await resolve_roles(request.role, request.secondary_role)
    else:
        classification = await classify_chat_intent(request.input, http_client)
        roles = await resolve_roles(classification["role"], None)

    model = select_model(request.input)

    # Validate agent names — fall back to auto-selection if needed
    agent_names = [a for a in request.agents if a in _agents]
    if len(agent_names) < 2:
        intent = await classify_intent(request.input)
        agent_names = select_agents_for_task(request.input, intent)
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
