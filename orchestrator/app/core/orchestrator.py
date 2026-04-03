import json
import logging
import time

import httpx

from app.models.schemas import TaskRequest, TaskResponse, OrchestratorDecision
from app.services.role_service import resolve_roles
from app.services.ollama_service import generate, select_model
from app.services.prompt_service import build_system_prompt
from app.services.intent_service import classify_intent
from app.services.workflow_service import route_workflow, WorkflowType
from app.agents.base import AgentInput
from app.agents.research import ResearchAgent
from app.memory.semantic import search_semantic

logger = logging.getLogger("pai.orchestrator")

# Agent registry
_agents = {
    "research": ResearchAgent(),
}


async def _run_agent(
    agent_name: str,
    request: TaskRequest,
    roles,
    http_client: httpx.AsyncClient,
    retrieved_context: list[str] | None = None,
):
    """Run a named agent: build its prompt, call the model, parse the result."""
    agent = _agents.get(agent_name)
    if not agent:
        return None

    agent_input = AgentInput(
        request_id=request.request_id,
        task=request.input,
        role_context={
            "role": roles.primary.role.value,
            "domain": roles.primary.domain.value,
            "goals": roles.primary.goals,
            "preferences": roles.primary.preferences,
            "constraints": roles.primary.constraints,
        },
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

    if workflow == WorkflowType.retrieval_augmented:
        # Retrieve relevant context from semantic memory
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        # Build prompt with retrieved context
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

    elif workflow == WorkflowType.agent_research:
        # Retrieve context, then delegate to research agent
        results = await search_semantic(request.input, limit=3, http_client=http_client)
        retrieved_context = [r["content"] for r in results]

        agent_output = await _run_agent(
            "research", request, roles, http_client, retrieved_context
        )
        if agent_output:
            content = agent_output.result
            structured = agent_output.metadata or None
        else:
            # Fallback to direct
            raw_response = await _direct_generate(request, roles, model, http_client)
            structured, content = _parse_response(raw_response, request)

    elif workflow in (WorkflowType.agent_planning, WorkflowType.execution):
        # Planning and execution fall back to direct for now (agents coming in Sprint 3)
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
