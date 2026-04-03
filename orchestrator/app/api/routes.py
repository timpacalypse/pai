import logging
from fastapi import APIRouter, Request

from app.models.schemas import TaskRequest, TaskResponse, CompetitionRequest, RoleType, DomainType, ROLE_DOMAIN_MAP
from app.core.orchestrator import handle_task, handle_competition
from app.memory.episodic import log_episodic
from app.services.role_service import get_all_roles

logger = logging.getLogger("pai.api")

router = APIRouter()


@router.post("/task", response_model=TaskResponse)
async def create_task(task: TaskRequest, request: Request) -> TaskResponse:
    """Accept a task, orchestrate it, and return a structured response."""
    response = await handle_task(
        request=task,
        http_client=request.app.state.http_client,
    )

    # Persist to episodic memory (non-blocking best-effort)
    await log_episodic(task, response)

    return response


@router.post("/compete", response_model=TaskResponse)
async def compete_task(comp: CompetitionRequest, request: Request) -> TaskResponse:
    """Run multi-agent competition on a task with explicit agent and strategy selection."""
    response = await handle_competition(
        request=comp,
        http_client=request.app.state.http_client,
    )

    await log_episodic(comp, response)

    return response


@router.get("/roles")
async def list_roles():
    """List all available roles grouped by domain."""
    roles = get_all_roles()
    grouped: dict[str, list[dict]] = {}
    for role_ctx in roles:
        domain = role_ctx.domain.value
        if domain not in grouped:
            grouped[domain] = []
        grouped[domain].append({
            "role": role_ctx.role.value,
            "description": role_ctx.description,
            "goals": role_ctx.goals,
            "preferences": role_ctx.preferences,
            "constraints": role_ctx.constraints,
        })
    return {"domains": grouped, "total_roles": len(roles)}
