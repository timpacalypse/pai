"""Memory consolidation — merge similar semantic chunks and prune low-value entries."""

import logging
from sqlalchemy import text
from app.core.database import async_session
from app.services.embedding_service import get_embedding

logger = logging.getLogger("pai.services.consolidation")


async def consolidate_memory(
    similarity_threshold: float = 0.92,
    min_age_hours: int = 24,
    batch_size: int = 50,
    http_client=None,
) -> dict:
    """Find and merge highly similar semantic memory entries.
    
    Strategy:
    1. Find pairs with cosine similarity >= threshold
    2. Keep the longer/richer entry, delete the shorter duplicate
    3. Only consider entries older than min_age_hours
    """
    merged = 0
    pruned = 0

    async with async_session() as session:
        # Find duplicate pairs (high similarity, same source prefix)
        result = await session.execute(
            text(
                "SELECT a.id AS id_a, b.id AS id_b, "
                "  LENGTH(a.content) AS len_a, LENGTH(b.content) AS len_b, "
                "  a.source AS source_a, b.source AS source_b, "
                "  1 - (a.embedding <=> b.embedding) AS similarity "
                "FROM semantic_memory a "
                "JOIN semantic_memory b ON a.id < b.id "
                "WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL "
                "  AND a.created_at < NOW() - INTERVAL ':hours hours' "
                "  AND b.created_at < NOW() - INTERVAL ':hours hours' "
                "  AND 1 - (a.embedding <=> b.embedding) >= :threshold "
                "ORDER BY similarity DESC "
                "LIMIT :batch"
            ).bindparams(
                hours=min_age_hours,
                threshold=similarity_threshold,
                batch=batch_size,
            ),
        )
        pairs = result.mappings().all()

        if not pairs:
            return {"merged": 0, "pruned": 0, "message": "No duplicates found"}

        ids_to_delete = set()
        for pair in pairs:
            id_a, id_b = pair["id_a"], pair["id_b"]
            # Skip if we already marked one for deletion
            if id_a in ids_to_delete or id_b in ids_to_delete:
                continue
            # Keep the longer entry
            if pair["len_a"] >= pair["len_b"]:
                ids_to_delete.add(id_b)
            else:
                ids_to_delete.add(id_a)
            merged += 1

        if ids_to_delete:
            # Delete in batches
            for del_id in ids_to_delete:
                await session.execute(
                    text("DELETE FROM semantic_memory WHERE id = :id"),
                    {"id": del_id},
                )
                pruned += 1
            await session.commit()

    logger.info("memory_consolidated merged=%d pruned=%d", merged, pruned)
    return {"merged": merged, "pruned": pruned}


async def prune_low_quality(
    min_content_length: int = 20,
    min_age_hours: int = 48,
    http_client=None,
) -> dict:
    """Remove semantic memory entries that are too short or likely noise."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "DELETE FROM semantic_memory "
                "WHERE LENGTH(content) < :min_len "
                "  AND created_at < NOW() - MAKE_INTERVAL(hours => :hours) "
                "RETURNING id"
            ),
            {"min_len": min_content_length, "hours": min_age_hours},
        )
        deleted = result.fetchall()
        await session.commit()

    count = len(deleted)
    if count:
        logger.info("memory_pruned_low_quality count=%d", count)
    return {"pruned": count}


async def get_memory_stats() -> dict:
    """Get semantic memory statistics."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT "
                "  COUNT(*) AS total_entries, "
                "  COUNT(CASE WHEN embedding IS NOT NULL THEN 1 END) AS with_embeddings, "
                "  COALESCE(SUM(LENGTH(content)), 0) AS total_chars, "
                "  COALESCE(AVG(LENGTH(content)), 0) AS avg_chars, "
                "  COUNT(DISTINCT source) AS unique_sources "
                "FROM semantic_memory"
            )
        )
        row = dict(result.mappings().fetchone())
        row["avg_chars"] = round(float(row["avg_chars"]), 1)
        row["total_chars"] = int(row["total_chars"])

        # Source breakdown
        sources = await session.execute(
            text(
                "SELECT "
                "  CASE "
                "    WHEN source LIKE 'file:%' THEN 'files' "
                "    WHEN source LIKE 'http%' THEN 'web' "
                "    WHEN source = 'chat' THEN 'chat' "
                "    ELSE 'other' "
                "  END AS source_type, "
                "  COUNT(*) AS count "
                "FROM semantic_memory "
                "GROUP BY source_type ORDER BY count DESC"
            )
        )
        row["by_source"] = {r["source_type"]: r["count"] for r in sources.mappings()}
        return row
