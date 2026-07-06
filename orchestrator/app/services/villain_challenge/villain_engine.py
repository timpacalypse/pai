"""Villain Selection Engine + Weekly Challenge Generator."""

import logging
import random
from datetime import datetime, date, timedelta, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.villain_challenge.models import (
    VILLAIN_CATALOG, ALL_DOMAINS, get_villains_in_hci_range, get_villain,
)

logger = logging.getLogger("pai.villain.engine")


async def select_weekly_villain(hero_data: dict) -> dict:
    """Select this week's villain based on hero state.

    Args:
        hero_data: dict from get_hero_profile() containing hci, domain_scores, etc.

    Returns:
        Selected Villain as dict with difficulty_rating and domain_focus.
    """
    hci = hero_data["hci"]
    domain_scores = hero_data["domain_scores"]
    weakest = hero_data["weakest_domain"]

    # Check for nemesis rematch
    nemesis = await _get_due_nemesis()
    if nemesis:
        villain = get_villain(nemesis["villain_id"])
        if villain:
            logger.info("nemesis_rematch_selected", extra={"villain": villain.name})
            return _villain_to_selection(villain, hci, is_nemesis=True)

    # Determine target HCI range: slightly above user's level
    target_low = max(0, hci - 5)
    target_high = min(100, hci + 15)

    # Adjust based on recent performance
    recent_streak = await _get_recent_win_streak()
    if recent_streak >= 3:
        target_low += 5
        target_high += 10  # Escalate
    elif recent_streak <= -2:
        target_low -= 10
        target_high -= 5  # De-escalate

    candidates = get_villains_in_hci_range(target_low, target_high)

    # Filter out recently fought villains (last 4 weeks)
    recent_ids = await _get_recent_villain_ids(weeks=4)
    candidates = [v for v in candidates if v.id not in recent_ids]

    # If no candidates after filtering, broaden range
    if not candidates:
        candidates = get_villains_in_hci_range(max(0, hci - 15), min(100, hci + 20))
        candidates = [v for v in candidates if v.id not in recent_ids]

    # Still nothing? Use any villain in broader range
    if not candidates:
        candidates = get_villains_in_hci_range(max(0, hci - 20), min(100, hci + 25))

    if not candidates:
        # Fallback to Toad
        candidates = [VILLAIN_CATALOG.get("toad", list(VILLAIN_CATALOG.values())[0])]

    # Prefer villains that target user's weakest domain
    weak_domain = weakest["name"]
    weighted = []
    for v in candidates:
        weight = 1.0
        # Prefer villains whose domain focus includes user's weakness
        if weak_domain in v.domain_weights:
            weight += v.domain_weights[weak_domain] * 3
        # Slight randomness
        weight += random.uniform(-0.2, 0.3)
        weighted.append((v, weight))

    weighted.sort(key=lambda x: x[1], reverse=True)
    selected = weighted[0][0]

    logger.info("villain_selected", extra={
        "villain": selected.name, "hci": hci,
        "range": f"{target_low}-{target_high}"
    })

    return _villain_to_selection(selected, hci)


def _villain_to_selection(villain, hero_hci: float, is_nemesis: bool = False) -> dict:
    """Convert a Villain object into a selection dict with difficulty."""
    # Difficulty = villain base HCI adjusted slightly
    difficulty = villain.base_hci + random.uniform(-3, 3)
    if is_nemesis:
        difficulty += 5  # Nemeses are harder

    # Determine which domains this villain most tests
    sorted_weights = sorted(villain.domain_weights.items(), key=lambda x: x[1], reverse=True)
    domain_focus = [d[0] for d in sorted_weights[:3]]

    return {
        "villain_id": villain.id,
        "villain_name": villain.name,
        "tier": villain.tier,
        "description": villain.description,
        "difficulty_rating": round(difficulty, 1),
        "villain_hci": villain.base_hci,
        "domain_focus": domain_focus,
        "domain_weights": villain.domain_weights,
        "weakness_text": villain.weakness_text,
        "victory_text": villain.victory_text,
        "defeat_text": villain.defeat_text,
        "is_nemesis": is_nemesis,
        "affiliation": villain.affiliation,
    }


async def generate_weekly_objectives(
    villain_selection: dict,
    hero_data: dict,
) -> list[dict]:
    """Generate 4-6 specific, measurable weekly objectives based on villain and hero state."""
    domain_scores = hero_data["domain_scores"]
    domain_weights = villain_selection["domain_weights"]
    hci = hero_data["hci"]

    objectives = []

    # Sort villain domains by weight to prioritize
    sorted_domains = sorted(domain_weights.items(), key=lambda x: x[1], reverse=True)

    for domain, weight in sorted_domains:
        if len(objectives) >= 6:
            break
        if weight < 0.10:
            continue

        score = domain_scores.get(domain, 50)
        # Scale difficulty based on domain score and villain weight
        intensity = "moderate" if score > 50 else "push"

        objs = _objectives_for_domain(domain, score, weight, intensity, hci)
        objectives.extend(objs)

    # Ensure at least 4 objectives
    if len(objectives) < 4:
        # Add generic consistency/check-in objectives
        objectives.append({
            "description": "Complete all 7 daily check-ins this week",
            "objective_type": "checkin_streak",
            "target_value": 7,
            "domain": "consistency",
            "weight": 1.0,
        })
        if len(objectives) < 4:
            objectives.append({
                "description": "Log body weight at least 3 times this week",
                "objective_type": "weight_logs",
                "target_value": 3,
                "domain": "physique",
                "weight": 0.8,
            })

    # Cap at 6
    return objectives[:6]


def _objectives_for_domain(
    domain: str, score: float, weight: float, intensity: str, hci: float
) -> list[dict]:
    """Generate 1-2 objectives for a specific domain."""
    objs = []

    if domain == "strength":
        target = 3 if score > 60 else 2
        objs.append({
            "description": f"Complete {target} Tonal strength workouts",
            "objective_type": "tonal_workouts",
            "target_value": target,
            "domain": "strength",
            "weight": weight,
        })
        if weight >= 0.25:
            objs.append({
                "description": "Set or attempt one strength PR",
                "objective_type": "strength_pr",
                "target_value": 1,
                "domain": "strength",
                "weight": weight * 0.7,
            })

    elif domain == "conditioning":
        target = 4 if score > 60 else 3
        objs.append({
            "description": f"Complete {target} Peloton rides",
            "objective_type": "peloton_rides",
            "target_value": target,
            "domain": "conditioning",
            "weight": weight,
        })
        if weight >= 0.25:
            objs.append({
                "description": "Complete 1 Zone 2 endurance session (30+ min)",
                "objective_type": "zone2_session",
                "target_value": 1,
                "domain": "conditioning",
                "weight": weight * 0.6,
            })

    elif domain == "recovery":
        target_pct = 70 if score > 60 else 60
        target_nights = 5 if score > 50 else 4
        objs.append({
            "description": f"Achieve sleep performance above {target_pct}% for {target_nights} nights",
            "objective_type": "sleep_target",
            "target_value": target_nights,
            "domain": "recovery",
            "weight": weight,
        })
        if weight >= 0.25:
            objs.append({
                "description": f"Maintain recovery above {target_pct}% for 4 days",
                "objective_type": "recovery_target",
                "target_value": 4,
                "domain": "recovery",
                "weight": weight * 0.7,
            })

    elif domain == "consistency":
        objs.append({
            "description": "Complete all 7 daily check-ins this week",
            "objective_type": "checkin_streak",
            "target_value": 7,
            "domain": "consistency",
            "weight": weight,
        })

    elif domain == "nutrition_adherence":
        target_days = 6 if score > 50 else 5
        objs.append({
            "description": f"Hit nutrition/protein target {target_days} days",
            "objective_type": "nutrition_target",
            "target_value": target_days,
            "domain": "nutrition_adherence",
            "weight": weight,
        })

    elif domain == "mobility":
        target = 3 if score > 50 else 2
        objs.append({
            "description": f"Complete {target} mobility sessions",
            "objective_type": "mobility_sessions",
            "target_value": target,
            "domain": "mobility",
            "weight": weight,
        })

    elif domain == "physique":
        objs.append({
            "description": "Log body weight at least 3 times this week",
            "objective_type": "weight_logs",
            "target_value": 3,
            "domain": "physique",
            "weight": weight,
        })

    return objs


async def create_weekly_challenge(
    villain_selection: dict,
    objectives: list[dict],
    tone: str = "shield_tactical",
) -> dict:
    """Persist a weekly challenge to the database."""
    today = date.today()
    # Week starts Monday
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    async with async_session() as session:
        # Check if challenge already exists for this week
        r = await session.execute(text(
            "SELECT id FROM villain_challenges WHERE week_start = :ws"
        ), {"ws": monday})
        existing = r.scalar_one_or_none()
        if existing:
            logger.info("challenge_already_exists", extra={"week": str(monday)})
            return await get_active_challenge()

        # Create challenge
        r = await session.execute(text("""
            INSERT INTO villain_challenges
                (villain_id, villain_name, week_start, week_end,
                 difficulty_rating, villain_hci, status, narrative_tone, domain_focus)
            VALUES (:vid, :vname, :ws, :we, :diff, :vhci, 'active', :tone,
                    CAST(:focus AS text[]))
            RETURNING id
        """), {
            "vid": villain_selection["villain_id"],
            "vname": villain_selection["villain_name"],
            "ws": monday, "we": sunday,
            "diff": villain_selection["difficulty_rating"],
            "vhci": villain_selection["villain_hci"],
            "tone": tone,
            "focus": villain_selection["domain_focus"],
        })
        challenge_id = r.scalar_one()

        # Create objectives
        for obj in objectives:
            await session.execute(text("""
                INSERT INTO challenge_objectives
                    (challenge_id, description, objective_type, target_value, domain, weight)
                VALUES (:cid, :desc, :otype, :target, :domain, :weight)
            """), {
                "cid": challenge_id,
                "desc": obj["description"],
                "otype": obj["objective_type"],
                "target": obj["target_value"],
                "domain": obj.get("domain", ""),
                "weight": obj.get("weight", 1.0),
            })

        await session.commit()
        logger.info("challenge_created", extra={
            "villain": villain_selection["villain_name"],
            "week": str(monday), "objectives": len(objectives),
        })

    return await get_active_challenge()


async def get_active_challenge() -> dict | None:
    """Get the current active challenge with objectives."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT * FROM villain_challenges
            WHERE status = 'active'
            ORDER BY week_start DESC LIMIT 1
        """))
        challenge = r.mappings().fetchone()
        if not challenge:
            return None

        challenge = dict(challenge)

        r = await session.execute(text("""
            SELECT * FROM challenge_objectives
            WHERE challenge_id = :cid
            ORDER BY id
        """), {"cid": challenge["id"]})
        objectives = [dict(row) for row in r.mappings().fetchall()]

        challenge["objectives"] = objectives
        challenge["completion_pct"] = _calc_completion(objectives)

        return challenge


def _calc_completion(objectives: list[dict]) -> float:
    if not objectives:
        return 0
    completed = sum(1 for o in objectives if o.get("completed"))
    return round((completed / len(objectives)) * 100, 1)


async def update_objective_progress(objective_type: str, increment: float = 1.0):
    """Update progress on objectives matching the given type for the active challenge."""
    challenge = await get_active_challenge()
    if not challenge:
        return

    async with async_session() as session:
        for obj in challenge["objectives"]:
            if obj["objective_type"] == objective_type and not obj["completed"]:
                new_val = min(obj["target_value"], obj["current_value"] + increment)
                completed = new_val >= obj["target_value"]
                await session.execute(text("""
                    UPDATE challenge_objectives
                    SET current_value = :val, completed = :done
                    WHERE id = :id
                """), {"val": new_val, "done": completed, "id": obj["id"]})
        await session.commit()


# ── Private Helpers ──

async def _get_due_nemesis() -> dict | None:
    """Check if any nemesis is due for a rematch (lost 2+ times, not fought in 3+ weeks)."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT villain_id, villain_name, losses
            FROM nemesis_tracker
            WHERE is_nemesis = TRUE
              AND (last_encounter IS NULL OR last_encounter < CURRENT_DATE - INTERVAL '21 days')
            ORDER BY losses DESC LIMIT 1
        """))
        row = r.mappings().fetchone()
        return dict(row) if row else None


async def _get_recent_win_streak() -> int:
    """Get recent win/loss streak. Positive = wins, negative = losses."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT outcome FROM battle_log
            ORDER BY battle_date DESC LIMIT 5
        """))
        rows = r.fetchall()
        if not rows:
            return 0

        streak = 0
        direction = None
        for row in rows:
            outcome = row[0]
            is_win = outcome in ("Overwhelming Victory", "Victory", "Narrow Victory")
            if direction is None:
                direction = is_win
            if is_win == direction:
                streak += 1 if is_win else -1
            else:
                break
        return streak


async def _get_recent_villain_ids(weeks: int = 4) -> set[str]:
    """Get villain IDs fought in the last N weeks."""
    async with async_session() as session:
        days = weeks * 7
        r = await session.execute(text("""
            SELECT DISTINCT villain_id FROM villain_challenges
            WHERE week_start > CURRENT_DATE - CAST(:days AS INTEGER) * INTERVAL '1 day'
        """), {"days": days})
        return {row[0] for row in r.fetchall()}


async def pause_challenge() -> dict | None:
    """Pause the current active challenge (e.g. for vacation)."""
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE villain_challenges SET status = 'paused'
            WHERE status = 'active'
            RETURNING id, villain_name
        """))
        await session.commit()
        row = r.mappings().fetchone()
        return dict(row) if row else None


async def resume_challenge() -> dict | None:
    """Resume a paused challenge."""
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE villain_challenges SET status = 'active'
            WHERE status = 'paused'
            RETURNING id, villain_name
        """))
        await session.commit()
        row = r.mappings().fetchone()
        return dict(row) if row else None


async def get_paused_challenge() -> dict | None:
    """Check if there's a paused challenge."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT id, villain_name, week_start, week_end
            FROM villain_challenges WHERE status = 'paused'
            ORDER BY week_start DESC LIMIT 1
        """))
        row = r.mappings().fetchone()
        return dict(row) if row else None


async def schedule_pause(pause_start, pause_end, reason: str = "") -> dict:
    """Schedule a future pause (e.g. for an upcoming vacation)."""
    async with async_session() as session:
        r = await session.execute(text("""
            INSERT INTO challenge_pause_schedule (pause_start, pause_end, reason)
            VALUES (:start, :end, :reason)
            RETURNING id, pause_start, pause_end, reason, status
        """), {"start": pause_start, "end": pause_end, "reason": reason})
        await session.commit()
        return dict(r.mappings().fetchone())


async def get_scheduled_pauses() -> list[dict]:
    """Get all scheduled (future) pauses."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT id, pause_start, pause_end, reason, status
            FROM challenge_pause_schedule
            WHERE status = 'scheduled'
            ORDER BY pause_start ASC
        """))
        return [dict(row) for row in r.mappings().fetchall()]


async def cancel_scheduled_pause(pause_id: int) -> bool:
    """Cancel a scheduled pause."""
    async with async_session() as session:
        r = await session.execute(text("""
            DELETE FROM challenge_pause_schedule WHERE id = :id AND status = 'scheduled'
        """), {"id": pause_id})
        await session.commit()
        return r.rowcount > 0


async def check_scheduled_pauses():
    """Check if any scheduled pauses should activate or deactivate today."""
    from datetime import date as dt_date
    today = dt_date.today()

    async with async_session() as session:
        # Activate pauses that start today
        r = await session.execute(text("""
            SELECT id FROM challenge_pause_schedule
            WHERE status = 'scheduled' AND pause_start <= :today AND pause_end >= :today
        """), {"today": today})
        to_activate = r.fetchall()

        for row in to_activate:
            await session.execute(text("""
                UPDATE challenge_pause_schedule SET status = 'active' WHERE id = :id
            """), {"id": row[0]})

        if to_activate:
            # Pause the active challenge
            await session.execute(text("""
                UPDATE villain_challenges SET status = 'paused' WHERE status = 'active'
            """))
            await session.commit()
            return "paused"

        # Resume pauses that ended
        r = await session.execute(text("""
            SELECT id FROM challenge_pause_schedule
            WHERE status = 'active' AND pause_end < :today
        """), {"today": today})
        to_complete = r.fetchall()

        for row in to_complete:
            await session.execute(text("""
                UPDATE challenge_pause_schedule SET status = 'completed' WHERE id = :id
            """), {"id": row[0]})

        if to_complete:
            # Resume the paused challenge
            await session.execute(text("""
                UPDATE villain_challenges SET status = 'active' WHERE status = 'paused'
            """))
            await session.commit()
            return "resumed"

        await session.commit()
        return None
