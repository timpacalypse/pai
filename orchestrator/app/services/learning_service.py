"""Learning loop service — auto-generate, test, and promote prompt/workflow improvements."""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate
from app.services.quality_service import get_agent_stats

logger = logging.getLogger("pai.services.learning")


IMPROVEMENT_SYSTEM_PROMPT = """\
You are a prompt engineering optimizer for a multi-agent AI system.
You are given performance data about agents (research, analysis, planning, critic, synthesizer).

Analyze the data and suggest ONE specific, targeted improvement to either:
1. An agent's system prompt (to improve quality)
2. A workflow routing rule (to pick better agents for certain intents)

Respond ONLY with valid JSON:
{
  "target": "agent_prompt" | "workflow_rule",
  "agent_name": "which agent this targets (if agent_prompt)",
  "description": "what you're changing and why",
  "hypothesis": "expected improvement",
  "change": "the specific text/rule change to apply",
  "metrics_to_watch": ["which score dimensions should improve"]
}
"""


async def generate_improvement(http_client=None) -> dict:
    """Analyze quality data and generate a candidate improvement."""
    stats = await get_agent_stats()
    if not stats:
        return {"status": "skip", "reason": "No quality data available yet"}

    stats_text = json.dumps(stats, indent=2, default=str)
    prompt = (
        f"Here are the current agent performance stats:\n\n{stats_text}\n\n"
        "Suggest one targeted improvement to boost the weakest dimension."
    )

    raw = await generate(
        prompt=prompt,
        system_prompt=IMPROVEMENT_SYSTEM_PROMPT,
        http_client=http_client,
    )

    parsed = _parse_json(raw)
    if parsed.get("parse_error"):
        return {"status": "error", "reason": "Could not parse improvement suggestion", "raw": raw}

    # Store the experiment
    experiment_id = str(uuid4())
    await _store_experiment(
        experiment_id=experiment_id,
        improvement=parsed,
        baseline_stats=stats,
    )

    return {
        "status": "created",
        "experiment_id": experiment_id,
        "improvement": parsed,
    }


async def get_experiments(status: str | None = None, limit: int = 20) -> list[dict]:
    """List learning experiments."""
    condition = "WHERE status = :status" if status else ""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT id, experiment_id, improvement, baseline_stats, result_stats, "
                f"  status, verdict, created_at, evaluated_at "
                f"FROM learning_experiments "
                f"{condition} "
                f"ORDER BY created_at DESC LIMIT :limit"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def evaluate_experiment(experiment_id: str, http_client=None) -> dict:
    """
    Compare current quality stats against the experiment's baseline.
    If improved, promote. If worse, reject.
    """
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, improvement, baseline_stats, status "
                "FROM learning_experiments WHERE experiment_id = :eid"
            ),
            {"eid": experiment_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return {"error": "Experiment not found"}
        if row["status"] != "pending":
            return {"error": f"Experiment already {row['status']}"}

    # Get current stats
    current_stats = await get_agent_stats()
    baseline = row["baseline_stats"] if isinstance(row["baseline_stats"], list) else []

    # Compare average scores
    baseline_avg = _avg_score(baseline)
    current_avg = _avg_score(current_stats)
    delta = current_avg - baseline_avg

    if delta > 0.01:
        verdict = "promoted"
    elif delta < -0.01:
        verdict = "rejected"
    else:
        verdict = "inconclusive"

    # Update experiment
    async with async_session() as session:
        await session.execute(
            text(
                "UPDATE learning_experiments "
                "SET result_stats = CAST(:results AS jsonb), "
                "    status = :status, verdict = :verdict, evaluated_at = NOW() "
                "WHERE experiment_id = :eid"
            ),
            {
                "results": json.dumps(current_stats, default=str),
                "status": verdict,
                "verdict": f"delta={delta:.4f}",
                "eid": experiment_id,
            },
        )
        await session.commit()

    logger.info("experiment_evaluated", extra={
        "experiment_id": experiment_id,
        "baseline_avg": baseline_avg,
        "current_avg": current_avg,
        "delta": delta,
        "verdict": verdict,
    })

    return {
        "experiment_id": experiment_id,
        "baseline_avg": round(baseline_avg, 4),
        "current_avg": round(current_avg, 4),
        "delta": round(delta, 4),
        "verdict": verdict,
    }


async def promote_experiment(experiment_id: str) -> dict:
    """Apply a promoted experiment's improvement as an active prompt override."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT experiment_id, improvement, status "
                "FROM learning_experiments WHERE experiment_id = :eid"
            ),
            {"eid": experiment_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return {"error": "Experiment not found"}
        if row["status"] not in ("pending", "promoted"):
            return {"error": f"Cannot promote — experiment status is {row['status']}"}

    improvement = row["improvement"] if isinstance(row["improvement"], dict) else {}
    target = improvement.get("target", "agent_prompt")
    agent_name = improvement.get("agent_name", "")
    change = improvement.get("change", "")

    if not change:
        return {"error": "No change text in improvement"}

    # Get current prompt as original_value (for rollback)
    original = ""
    if target == "agent_prompt" and agent_name:
        from app.services.prompt_service import get_agent_system_prompt
        original = get_agent_system_prompt(agent_name)

    async with async_session() as session:
        # Deactivate any existing override for this agent
        if agent_name:
            await session.execute(
                text(
                    "UPDATE prompt_overrides SET active = FALSE "
                    "WHERE agent_name = :agent AND active = TRUE"
                ),
                {"agent": agent_name},
            )

        await session.execute(
            text(
                "INSERT INTO prompt_overrides "
                "(target, agent_name, original_value, override_value, experiment_id) "
                "VALUES (:target, :agent, :original, :override, :eid) "
                "ON CONFLICT (experiment_id) DO UPDATE SET "
                "  active = TRUE, override_value = EXCLUDED.override_value"
            ),
            {
                "target": target,
                "agent": agent_name,
                "original": original,
                "override": change,
                "eid": experiment_id,
            },
        )

        # Update experiment status
        await session.execute(
            text(
                "UPDATE learning_experiments SET status = 'promoted', "
                "evaluated_at = NOW() WHERE experiment_id = :eid"
            ),
            {"eid": experiment_id},
        )
        await session.commit()

    logger.info("experiment_promoted", extra={"experiment_id": experiment_id, "agent": agent_name})
    return {"status": "promoted", "experiment_id": experiment_id, "agent_name": agent_name}


async def rollback_experiment(experiment_id: str) -> dict:
    """Rollback a promoted experiment — deactivate its override."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, active FROM prompt_overrides WHERE experiment_id = :eid"
            ),
            {"eid": experiment_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return {"error": "No override found for this experiment"}
        if not row["active"]:
            return {"error": "Override already inactive"}

        await session.execute(
            text("UPDATE prompt_overrides SET active = FALSE WHERE experiment_id = :eid"),
            {"eid": experiment_id},
        )
        await session.execute(
            text(
                "UPDATE learning_experiments SET status = 'rolled_back', "
                "evaluated_at = NOW() WHERE experiment_id = :eid"
            ),
            {"eid": experiment_id},
        )
        await session.commit()

    logger.info("experiment_rolled_back", extra={"experiment_id": experiment_id})
    return {"status": "rolled_back", "experiment_id": experiment_id}


async def get_active_overrides() -> list[dict]:
    """Get all active prompt overrides."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT target, agent_name, override_value, experiment_id, created_at "
                "FROM prompt_overrides WHERE active = TRUE ORDER BY created_at DESC"
            )
        )
        return [dict(r) for r in result.mappings()]


async def _store_experiment(experiment_id: str, improvement: dict, baseline_stats: list) -> None:
    """Persist a learning experiment."""
    async with async_session() as session:
        await session.execute(
            text(
                "INSERT INTO learning_experiments "
                "(experiment_id, improvement, baseline_stats, status) "
                "VALUES (:eid, CAST(:improvement AS jsonb), CAST(:baseline AS jsonb), 'pending')"
            ),
            {
                "eid": experiment_id,
                "improvement": json.dumps(improvement),
                "baseline": json.dumps(baseline_stats, default=str),
            },
        )
        await session.commit()


def _avg_score(stats: list) -> float:
    """Compute average total score across all agents."""
    if not stats:
        return 0.0
    scores = []
    for s in stats:
        avg = s.get("avg_score")
        if avg is not None:
            scores.append(float(avg))
    return sum(scores) / len(scores) if scores else 0.0


def _parse_json(raw: str) -> dict:
    text_clean = raw.strip()
    if text_clean.startswith("```"):
        lines = text_clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text_clean = "\n".join(lines)
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        start = text_clean.find("{")
        end = text_clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text_clean[start:end])
            except json.JSONDecodeError:
                pass
        return {"parse_error": True, "raw": raw}
