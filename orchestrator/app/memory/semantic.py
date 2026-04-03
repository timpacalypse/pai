import json
import logging

from sqlalchemy import text

from app.core.database import async_session
from app.services.embedding_service import get_embedding

logger = logging.getLogger("pai.memory.semantic")


async def store_semantic(
    content: str,
    source: str = "",
    metadata: dict | None = None,
    http_client=None,
) -> int:
    """Embed content and store in semantic_memory. Returns the row id."""
    embedding = await get_embedding(content, http_client=http_client)
    if not embedding:
        logger.warning("empty_embedding", extra={"source": source})
        return -1

    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO semantic_memory (content, embedding, source, metadata) "
                "VALUES (:content, CAST(:embedding AS vector), :source, :meta) RETURNING id"
            ),
            {
                "content": content,
                "embedding": str(embedding),
                "source": source,
                "meta": json.dumps(metadata or {}),
            },
        )
        row_id = result.scalar_one()
        await session.commit()
        return row_id


async def search_semantic(
    query: str,
    limit: int = 5,
    http_client=None,
) -> list[dict]:
    """Search semantic_memory by vector similarity. Returns matching content."""
    embedding = await get_embedding(query, http_client=http_client)
    if not embedding:
        return []

    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, content, source, metadata, "
                "1 - (embedding <=> CAST(:embedding AS vector)) AS similarity "
                "FROM semantic_memory "
                "WHERE embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:embedding AS vector) "
                "LIMIT :limit"
            ),
            {"embedding": str(embedding), "limit": limit},
        )
        rows = []
        for row in result.mappings():
            rows.append({
                "id": row["id"],
                "content": row["content"],
                "source": row["source"],
                "similarity": round(float(row["similarity"]), 4),
            })
        return rows
