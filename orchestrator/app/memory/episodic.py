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
