"""Procedural memory — learn which workflows and agents work best for each intent."""

import json
import logging

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.procedural")


async def record_outcome(
    intent: str,
    workflow: str,
    agent_names: list[str],
    avg_score: float,
) -> None:
    """Record a task outcome into procedural memory for future routing."""
    workflow_name = f"{intent}:{workflow}"
    agents_str = ",".join(sorted(agent_names))

    async with async_session() as session:
        # Check if this pattern already exists
        existing = await session.execute(
            text("""
                SELECT id, success_rate, usage_count
                FROM procedural_memory
                WHERE workflow_name = :name
                  AND metadata->>'agents' = :agents
            """),
            {"name": workflow_name, "agents": agents_str},
        )
        row = existing.fetchone()

        if row:
            # Update with exponential moving average
            row_map = row._mapping
            old_rate = float(row_map["success_rate"])
            count = int(row_map["usage_count"]) + 1
            # EMA with alpha = 0.3 — recent results weighted more
            new_rate = old_rate * 0.7 + avg_score * 0.3
            await session.execute(
                text("""
                    UPDATE procedural_memory
                    SET success_rate = :rate, usage_count = :count, updated_at = NOW()
                    WHERE id = :id
                """),
                {"rate": new_rate, "count": count, "id": row_map["id"]},
            )
        else:
            await session.execute(
                text("""
                    INSERT INTO procedural_memory (workflow_name, workflow_definition, success_rate, usage_count, metadata)
                    VALUES (:name, CAST(:definition AS jsonb), :rate, 1, CAST(:meta AS jsonb))
                """),
                {
                    "name": workflow_name,
                    "definition": json.dumps({"intent": intent, "workflow": workflow, "agents": list(agent_names)}),
                    "rate": avg_score,
                    "meta": json.dumps({"agents": agents_str}),
                },
            )
        await session.commit()

    logger.info("procedural_recorded", extra={"pattern": workflow_name, "agents": agents_str, "score": avg_score})


async def lookup_proven_workflow(intent: str, min_uses: int = 2, min_score: float = 0.45) -> dict | None:
    """Look up the best proven workflow pattern for a given intent."""
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT workflow_name, workflow_definition, success_rate, usage_count, metadata
                FROM procedural_memory
                WHERE workflow_name LIKE :prefix
                  AND usage_count >= :min_uses
                  AND success_rate >= :min_score
                ORDER BY success_rate DESC
                LIMIT 1
            """),
            {"prefix": f"{intent}:%", "min_uses": min_uses, "min_score": min_score},
        )
        row = result.fetchone()
        if row:
            m = row._mapping
            return {
                "workflow_name": m["workflow_name"],
                "workflow_definition": m["workflow_definition"],
                "success_rate": float(m["success_rate"]),
                "usage_count": int(m["usage_count"]),
                "agents": m["metadata"].get("agents", "").split(",") if m["metadata"].get("agents") else [],
            }
        return None


async def get_patterns(intent: str | None = None) -> list[dict]:
    """List all procedural memory patterns, optionally filtered by intent."""
    condition = "WHERE workflow_name LIKE :prefix" if intent else ""
    params = {"prefix": f"{intent}:%"} if intent else {}

    async with async_session() as session:
        result = await session.execute(
            text(f"""
                SELECT workflow_name, success_rate, usage_count, metadata, created_at, updated_at
                FROM procedural_memory
                {condition}
                ORDER BY success_rate DESC
            """),
            params,
        )
        return [
            {
                "workflow_name": r["workflow_name"],
                "success_rate": float(r["success_rate"]),
                "usage_count": int(r["usage_count"]),
                "agents": r["metadata"].get("agents", ""),
                "created_at": str(r["created_at"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in [row._mapping for row in result.fetchall()]
        ]
