"""Battle System — Daily probability, battle scoring, and weekly resolution."""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.villain_challenge.models import (
    BATTLE_OUTCOMES, BATTLE_STATUSES, ALL_DOMAINS,
)

logger = logging.getLogger("pai.villain.battle")


async def calculate_daily_battle_probability(challenge: dict, hero_data: dict) -> dict:
    """Calculate current battle probability and status.

    Returns dict with: probability, status, advantage_text, recommended_actions
    """
    if not challenge:
        return {"probability": 0, "status": "No Active Challenge"}

    objectives = challenge.get("objectives", [])
    total_objs = len(objectives)
    completed_objs = sum(1 for o in objectives if o.get("completed"))
    partial_progress = sum(
        min(1.0, o.get("current_value", 0) / o["target_value"])
        for o in objectives if o["target_value"] > 0
    )

    # Days in challenge
    week_start = challenge["week_start"]
    week_end = challenge["week_end"]
    if isinstance(week_start, str):
        week_start = date.fromisoformat(week_start)
    if isinstance(week_end, str):
        week_end = date.fromisoformat(week_end)

    today = date.today()
    total_days = max(1, (week_end - week_start).days + 1)
    days_elapsed = min(total_days, max(1, (today - week_start).days + 1))
    days_remaining = max(0, (week_end - today).days)

    # Base probability from objective completion
    if total_objs > 0:
        completion_ratio = partial_progress / total_objs
    else:
        completion_ratio = 0.5

    # Expected pace
    expected_completion = days_elapsed / total_days
    pace_modifier = 0.0
    if expected_completion > 0:
        pace_ratio = completion_ratio / expected_completion
        pace_modifier = (pace_ratio - 1.0) * 15  # +/- based on pace

    # Domain modifier from villain weights
    domain_scores = hero_data.get("domain_scores", {})
    villain_hci = challenge.get("villain_hci", 50)
    hero_hci = hero_data.get("hci", 50)

    hci_diff = hero_hci - villain_hci
    hci_modifier = hci_diff * 0.5  # Each HCI point = 0.5% probability

    # Recovery modifier
    recovery = domain_scores.get("recovery", 50)
    recovery_modifier = (recovery - 50) * 0.2

    # Soreness/injury check
    injury_modifier = await _get_injury_modifier()

    # Power surge bonus
    surge_bonus = await _get_active_surge_bonus()

    # Calculate probability
    base_prob = 50.0  # Start at coin flip
    base_prob += completion_ratio * 30  # Up to +30 for completion
    base_prob += pace_modifier
    base_prob += hci_modifier
    base_prob += recovery_modifier
    base_prob += injury_modifier
    base_prob += surge_bonus

    probability = max(5, min(95, round(base_prob, 1)))

    # Determine battle status
    status = "Critical"
    for bs in BATTLE_STATUSES:
        if probability >= bs["min_prob"]:
            status = bs["name"]
            break

    # Find weakest domain that villain exploits
    villain_domain_focus = challenge.get("domain_focus", [])
    exploitable = []
    for d in villain_domain_focus:
        if isinstance(d, str) and d in domain_scores and domain_scores[d] < 50:
            exploitable.append((d, domain_scores[d]))
    exploitable.sort(key=lambda x: x[1])

    advantage_text = ""
    if exploitable:
        weak = exploitable[0]
        advantage_text = f"{challenge['villain_name']} is exploiting your {weak[0]} gap (score: {weak[1]:.0f})."
    elif probability >= 65:
        advantage_text = f"You have the advantage over {challenge['villain_name']}."
    else:
        advantage_text = f"{challenge['villain_name']} is holding ground."

    # Recommended actions
    actions = _get_recommended_actions(objectives, days_remaining, domain_scores, villain_domain_focus)

    return {
        "probability": probability,
        "status": status,
        "completed_objectives": completed_objs,
        "total_objectives": total_objs,
        "days_remaining": days_remaining,
        "advantage_text": advantage_text,
        "recommended_actions": actions,
        "hci_diff": round(hci_diff, 1),
        "surge_active": surge_bonus > 0,
    }


async def calculate_battle_score(challenge: dict, hero_data: dict) -> float:
    """Calculate final battle score for weekly resolution (0-100)."""
    objectives = challenge.get("objectives", [])
    domain_scores = hero_data.get("domain_scores", {})
    villain_hci = challenge.get("villain_hci", 50)
    hero_hci = hero_data.get("hci", 50)

    # 1. Objective completion (50% of score)
    if objectives:
        obj_completion = sum(
            min(1.0, o.get("current_value", 0) / o["target_value"]) * o.get("weight", 1.0)
            for o in objectives if o["target_value"] > 0
        )
        total_weight = sum(o.get("weight", 1.0) for o in objectives)
        obj_score = (obj_completion / total_weight * 100) if total_weight > 0 else 0
    else:
        obj_score = 50

    # 2. Domain matchup (30% of score)
    # How well user's domains counter villain's weighted domains
    domain_match = 0
    villain_weights = {}
    # Reconstruct villain weights from domain_focus or catalog
    from app.services.villain_challenge.models import get_villain
    villain = get_villain(challenge.get("villain_id", ""))
    if villain:
        villain_weights = villain.domain_weights

    if villain_weights:
        for domain, weight in villain_weights.items():
            user_score = domain_scores.get(domain, 50)
            domain_match += (user_score / 100) * weight
        domain_match = domain_match * 100
    else:
        domain_match = hero_hci

    # 3. HCI advantage (15% of score)
    hci_advantage = 50 + (hero_hci - villain_hci) * 0.8
    hci_advantage = max(0, min(100, hci_advantage))

    # 4. Power surge bonus (5% of score)
    surge_bonus = await _get_active_surge_bonus()
    surge_score = min(100, 50 + surge_bonus * 5)

    # Weighted composite
    battle_score = (
        obj_score * 0.50 +
        domain_match * 0.30 +
        hci_advantage * 0.15 +
        surge_score * 0.05
    )

    return round(max(0, min(100, battle_score)), 1)


def resolve_outcome(battle_score: float) -> dict:
    """Determine battle outcome from score."""
    for outcome in BATTLE_OUTCOMES:
        if battle_score >= outcome["min_score"]:
            return {
                "name": outcome["name"],
                "xp_mult": outcome["xp_mult"],
                "is_victory": outcome["min_score"] >= 65,
                "battle_score": battle_score,
            }
    return {"name": "Severe Defeat", "xp_mult": 0.0, "is_victory": False, "battle_score": battle_score}


async def resolve_weekly_battle(challenge: dict, hero_data: dict) -> dict:
    """Resolve the weekly battle — end-of-week scoring and outcome."""
    battle_score = await calculate_battle_score(challenge, hero_data)
    outcome = resolve_outcome(battle_score)

    async with async_session() as session:
        # Update challenge status
        status = "victory" if outcome["is_victory"] else (
            "stalemate" if outcome["name"] == "Stalemate" else "defeat"
        )
        await session.execute(text("""
            UPDATE villain_challenges
            SET status = :status, battle_score = :score, outcome = :outcome
            WHERE id = :id
        """), {
            "status": status, "score": battle_score,
            "outcome": outcome["name"], "id": challenge["id"],
        })

        # Log battle
        await session.execute(text("""
            INSERT INTO battle_log
                (villain_id, villain_name, challenge_id, battle_date,
                 battle_score, outcome, hero_hci, villain_hci)
            VALUES (:vid, :vname, :cid, CURRENT_DATE, :score, :outcome, :hhci, :vhci)
        """), {
            "vid": challenge["villain_id"],
            "vname": challenge["villain_name"],
            "cid": challenge["id"],
            "score": battle_score,
            "outcome": outcome["name"],
            "hhci": hero_data.get("hci", 0),
            "vhci": challenge.get("villain_hci", 0),
        })

        # Update nemesis tracker
        await _update_nemesis(
            challenge["villain_id"],
            challenge["villain_name"],
            outcome["is_victory"],
            session,
        )

        await session.commit()

    outcome["villain_name"] = challenge["villain_name"]
    outcome["villain_id"] = challenge["villain_id"]
    return outcome


# ── Private Helpers ──

def _get_recommended_actions(
    objectives: list[dict],
    days_remaining: int,
    domain_scores: dict,
    villain_focus: list[str],
) -> list[str]:
    """Generate 1-3 recommended actions based on current state."""
    actions = []

    # Incomplete objectives sorted by urgency
    incomplete = [
        o for o in objectives
        if not o.get("completed") and o["target_value"] > 0
    ]
    incomplete.sort(key=lambda o: o.get("current_value", 0) / o["target_value"])

    for o in incomplete[:2]:
        remaining = o["target_value"] - o.get("current_value", 0)
        if remaining > 0:
            actions.append(f"{o['description']} ({remaining:.0f} remaining)")

    # Domain-based suggestions
    for d in villain_focus:
        if isinstance(d, str) and d in domain_scores and domain_scores[d] < 45:
            if d == "recovery":
                actions.append("Prioritize sleep quality tonight")
            elif d == "conditioning":
                actions.append("Complete a Zone 2 cardio session")
            elif d == "mobility":
                actions.append("Complete a mobility session")
            elif d == "nutrition_adherence":
                actions.append("Hit your protein target today")
            break

    return actions[:3]


async def _get_injury_modifier() -> float:
    """Check recent check-ins for injury/soreness that would impact battle."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT soreness_level, injury_notes
            FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - INTERVAL '2 days'
            ORDER BY checkin_date DESC LIMIT 1
        """))
        row = r.fetchone()
        if not row:
            return 0

        soreness = row[0] or 0
        has_injury = bool(row[1] and row[1].strip())

        modifier = 0
        if soreness >= 7:
            modifier -= 10
        elif soreness >= 5:
            modifier -= 5
        if has_injury:
            modifier -= 8
        return modifier


async def _get_active_surge_bonus() -> float:
    """Get battle bonus from active power surges."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT COALESCE(SUM(battle_bonus), 0)
            FROM power_surges
            WHERE is_active = TRUE AND expires_at > NOW()
        """))
        return r.scalar_one_or_none() or 0


async def _update_nemesis(
    villain_id: str, villain_name: str, is_victory: bool, session
):
    """Update nemesis tracker after a battle."""
    r = await session.execute(text(
        "SELECT * FROM nemesis_tracker WHERE villain_id = :vid"
    ), {"vid": villain_id})
    existing = r.mappings().fetchone()

    if existing:
        if is_victory:
            new_wins = existing["wins"] + 1
            await session.execute(text("""
                UPDATE nemesis_tracker SET
                    wins = :w, last_encounter = CURRENT_DATE,
                    is_nemesis = CASE WHEN is_nemesis AND :w > losses THEN FALSE ELSE is_nemesis END,
                    debuff_active = FALSE, updated_at = NOW()
                WHERE villain_id = :vid
            """), {"w": new_wins, "vid": villain_id})
        else:
            new_losses = existing["losses"] + 1
            is_nemesis = new_losses >= 2
            debuff_active = new_losses >= 3

            await session.execute(text("""
                UPDATE nemesis_tracker SET
                    losses = :l, last_encounter = CURRENT_DATE,
                    is_nemesis = :nem,
                    nemesis_since = CASE WHEN :nem AND NOT is_nemesis THEN NOW() ELSE nemesis_since END,
                    debuff_active = :debuff,
                    debuff_expires = CASE WHEN :debuff THEN NOW() + INTERVAL '7 days' ELSE debuff_expires END,
                    updated_at = NOW()
                WHERE villain_id = :vid
            """), {"l": new_losses, "vid": villain_id, "nem": is_nemesis, "debuff": debuff_active})
    else:
        await session.execute(text("""
            INSERT INTO nemesis_tracker (villain_id, villain_name, losses, wins, last_encounter)
            VALUES (:vid, :vname, :l, :w, CURRENT_DATE)
        """), {
            "vid": villain_id, "vname": villain_name,
            "l": 0 if is_victory else 1, "w": 1 if is_victory else 0,
        })


async def get_battle_history(limit: int = 10) -> list[dict]:
    """Get recent battle history."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT * FROM battle_log ORDER BY battle_date DESC LIMIT :lim
        """), {"lim": limit})
        return [dict(row) for row in r.mappings().fetchall()]


async def get_nemesis_list() -> list[dict]:
    """Get all current nemeses."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT * FROM nemesis_tracker WHERE is_nemesis = TRUE ORDER BY losses DESC
        """))
        return [dict(row) for row in r.mappings().fetchall()]
