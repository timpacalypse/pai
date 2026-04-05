import json
import logging

from sqlalchemy import text

from app.core.database import async_session
from app.models.schemas import TaskRequest, TaskResponse

logger = logging.getLogger("pai.memory")


async def log_episodic(request: TaskRequest, response: TaskResponse) -> None:
    """Persist a request/response pair to episodic_memory. Fire-and-forget safe."""
    try:
        async with async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO episodic_memory "
                    "(session_id, role, request_type, input_text, output_text, metadata) "
                    "VALUES (:sid, :role, :rtype, :input, :output, :meta)"
                ),
                {
                    "sid": str(request.request_id),
                    "role": response.role,
                    "rtype": response.workflow,
                    "input": request.input,
                    "output": response.content,
                    "meta": json.dumps({
                        "model": response.model,
                        "domain": response.domain,
                        "secondary_role": response.secondary_role,
                        "intent": response.intent,
                        "workflow": response.workflow,
                        "duration_ms": response.duration_ms,
                        "structured": response.structured_output is not None,
                    }),
                },
            )
            await session.commit()
    except Exception:
        logger.exception("episodic_memory_write_failed")


async def log_chat_turn(
    conversation_id: str,
    role: str,
    user_message: str,
    assistant_message: str,
    domain: str = "",
    duration_ms: float = 0,
) -> None:
    """Persist a chat conversation turn (user + assistant) to episodic_memory."""
    try:
        async with async_session() as session:
            await session.execute(
                text(
                    "INSERT INTO episodic_memory "
                    "(session_id, role, request_type, input_text, output_text, metadata) "
                    "VALUES (:sid, :role, 'chat', :input, :output, CAST(:meta AS jsonb))"
                ),
                {
                    "sid": conversation_id,
                    "role": role,
                    "input": user_message,
                    "output": assistant_message,
                    "meta": json.dumps({
                        "domain": domain,
                        "duration_ms": duration_ms,
                    }),
                },
            )
            await session.commit()
    except Exception:
        logger.exception("chat_memory_write_failed")


async def get_chat_history(conversation_id: str, limit: int = 50) -> list[dict]:
    """Retrieve chat turns for a conversation, ordered chronologically."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT input_text, output_text, role, created_at "
                "FROM episodic_memory "
                "WHERE session_id = :sid AND request_type = 'chat' "
                "ORDER BY created_at ASC LIMIT :limit"
            ),
            {"sid": conversation_id, "limit": limit},
        )
        turns = []
        for row in result.mappings():
            turns.append({"role_name": "user", "content": row["input_text"]})
            turns.append({"role_name": "assistant", "content": row["output_text"]})
        return turns


async def list_conversations(limit: int = 20) -> list[dict]:
    """List recent conversations with their first message as preview."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT session_id, MIN(input_text) as preview, "
                "  MIN(created_at) as started_at, MAX(created_at) as last_at, "
                "  COUNT(*) as turn_count "
                "FROM episodic_memory "
                "WHERE request_type = 'chat' "
                "GROUP BY session_id "
                "ORDER BY MAX(created_at) DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(r) for r in result.mappings()]
