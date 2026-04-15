"""Conversation persistence — links chat sessions to users."""

import logging
from uuid import UUID
from sqlalchemy import text
from app.core.database import async_session

logger = logging.getLogger("pai.services.conversation")

_TITLE_SYSTEM = (
    "Generate a concise 3-6 word title summarizing this conversation. "
    "No quotes, no punctuation at the end, no prefixes like 'Title:'. "
    "Just the title words."
)


async def generate_title(message: str, http_client=None) -> str:
    """Generate a short conversation title from the first user message."""
    try:
        from app.services.ollama_service import generate
        raw = await generate(
            prompt=message[:200],
            system_prompt=_TITLE_SYSTEM,
            model="qwen3:4b",
            http_client=http_client,
        )
        title = raw.strip().strip('"').strip("'").strip()
        # Remove any <think>...</think> blocks from qwen3
        import re
        title = re.sub(r"<think>.*?</think>", "", title, flags=re.DOTALL).strip()
        if len(title) > 80:
            title = title[:77] + "..."
        return title or message[:60]
    except Exception as e:
        logger.warning("title_generation_failed: %s", e)
        return message[:60]


async def ensure_conversation(conversation_id: UUID, user_id: int, title: str = "", http_client=None) -> None:
    """Create a conversation record if it doesn't exist."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id FROM conversations WHERE id = CAST(:cid AS UUID)"),
            {"cid": str(conversation_id)},
        )
        if result.fetchone():
            await session.execute(
                text("UPDATE conversations SET updated_at = NOW() WHERE id = CAST(:cid AS UUID)"),
                {"cid": str(conversation_id)},
            )
        else:
            # Generate a short title from the first message
            generated_title = await generate_title(title, http_client=http_client)
            await session.execute(
                text(
                    "INSERT INTO conversations (id, user_id, title) "
                    "VALUES (CAST(:cid AS UUID), :uid, :title)"
                ),
                {"cid": str(conversation_id), "uid": user_id, "title": generated_title},
            )
        await session.commit()


async def get_user_conversations(user_id: int, limit: int = 30) -> list[dict]:
    """List a user's conversations with preview and turn count."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT c.id, c.title, c.created_at, c.updated_at, "
                "  COALESCE(e.turn_count, 0) AS turn_count, "
                "  e.preview "
                "FROM conversations c "
                "LEFT JOIN LATERAL ( "
                "  SELECT COUNT(*) AS turn_count, "
                "    MIN(input_text) AS preview "
                "  FROM episodic_memory "
                "  WHERE session_id = c.id AND request_type = 'chat' "
                ") e ON TRUE "
                "WHERE c.user_id = :uid "
                "ORDER BY c.updated_at DESC LIMIT :limit"
            ),
            {"uid": user_id, "limit": limit},
        )
        rows = []
        for r in result.mappings():
            rows.append({
                "id": str(r["id"]),
                "title": r["title"] or (r["preview"][:60] + "..." if r["preview"] and len(r["preview"]) > 60 else r["preview"] or "New chat"),
                "turn_count": r["turn_count"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return rows


async def update_conversation_title(conversation_id: UUID, title: str) -> None:
    """Update the title of a conversation."""
    async with async_session() as session:
        await session.execute(
            text("UPDATE conversations SET title = :title WHERE id = CAST(:cid AS UUID)"),
            {"cid": str(conversation_id), "title": title[:500]},
        )
        await session.commit()


async def delete_conversation(conversation_id: UUID) -> bool:
    """Delete a conversation and its chat history."""
    async with async_session() as session:
        # Delete episodic memory turns
        await session.execute(
            text("DELETE FROM episodic_memory WHERE session_id = :sid AND request_type = 'chat'"),
            {"sid": str(conversation_id)},
        )
        # Delete conversation record
        result = await session.execute(
            text("DELETE FROM conversations WHERE id = CAST(:cid AS UUID) RETURNING id"),
            {"cid": str(conversation_id)},
        )
        deleted = result.fetchone() is not None
        await session.commit()
        return deleted
