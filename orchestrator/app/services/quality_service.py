"""Quality metrics service — persist and query agent evaluation scores."""

import logging
from uuid import UUID

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.quality")


async def store_scores(
    request_id: UUID,
    intent: str,
    workflow: str,
    model: str,
    scores: list[dict],
    winner: str | None = None,
) -> None:
    """Persist evaluation scores for all agents in a competition."""
    async with async_session() as session:
        for s in scores:
            await session.execute(
                text("""
                    INSERT INTO quality_metrics
                        (request_id, intent, workflow, agent_name, model,
                         accuracy, relevance, depth, clarity, actionability, consistency,
                         total, was_selected)
                    VALUES
                        (:rid, :intent, :workflow, :agent, :model,
                         :accuracy, :relevance, :depth, :clarity, :actionability, :consistency,
                         :total, :selected)
                """),
                {
                    "rid": str(request_id),
                    "intent": intent,
                    "workflow": workflow,
                    "agent": s["agent_name"],
                    "model": model,
                    "accuracy": s.get("accuracy", 0),
                    "relevance": s.get("relevance", 0),
                    "depth": s.get("depth", 0),
                    "clarity": s.get("clarity", 0),
                    "actionability": s.get("actionability", 0),
                    "consistency": s.get("consistency", 0),
                    "total": s.get("total", 0),
                    "selected": s["agent_name"] == winner,
                },
            )
        await session.commit()
    logger.info("quality_scores_stored", extra={"request_id": str(request_id), "count": len(scores)})


async def get_agent_stats(agent_name: str | None = None) -> list[dict]:
    """Get aggregate quality stats per agent."""
    condition = "WHERE agent_name = :agent" if agent_name else ""
    params = {"agent": agent_name} if agent_name else {}

    async with async_session() as session:
        result = await session.execute(
            text(f"""
                SELECT agent_name,
                       COUNT(*) as total_runs,
                       ROUND(AVG(total)::numeric, 3) as avg_score,
                       ROUND(AVG(accuracy)::numeric, 3) as avg_accuracy,
                       ROUND(AVG(relevance)::numeric, 3) as avg_relevance,
                       SUM(CASE WHEN was_selected THEN 1 ELSE 0 END) as wins
                FROM quality_metrics
                {condition}
                GROUP BY agent_name
                ORDER BY avg_score DESC
            """),
            params,
        )
        return [dict(row._mapping) for row in result.fetchall()]
