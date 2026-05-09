"""XP Engine — Experience points, leveling, and power surge management."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.villain_challenge.models import (
    XP_AWARDS, XP_PENALTIES, POWER_SURGES,
)

logger = logging.getLogger("pai.villain.xp")


# ── XP Level Thresholds ──

LEVEL_THRESHOLDS = [
    0, 100, 300, 600, 1000, 1500, 2200, 3000, 4000, 5200,
    6500, 8000, 10000, 12500, 15500, 19000, 23000, 28000, 34000, 41000,
    50000, 60000, 72000, 86000, 102000,
]

LEVEL_TITLES = {
    1: "Recruit",
    5: "Street-Level Operative",
    10: "Enhanced Agent",
    15: "Super Soldier Program Graduate",
    20: "Alpha-Level Operative",
    25: "Omega-Level Hero",
}


def level_from_xp(total_xp: int) -> int:
    """Calculate level from total XP."""
    for i in range(len(LEVEL_THRESHOLDS) - 1, -1, -1):
        if total_xp >= LEVEL_THRESHOLDS[i]:
            return i + 1
    return 1


def title_from_level(level: int) -> str:
    """Get title for a given level."""
    best_title = "Recruit"
    for threshold, title in sorted(LEVEL_TITLES.items()):
        if level >= threshold:
            best_title = title
    return best_title


def xp_to_next_level(total_xp: int) -> int:
    """XP needed for next level."""
    level = level_from_xp(total_xp)
    if level >= len(LEVEL_THRESHOLDS):
        return 0
    return LEVEL_THRESHOLDS[level] - total_xp


# ── XP Awards ──

async def award_xp(
    amount: int,
    reason: str,
    category: str = "general",
    challenge_id: int | None = None,
) -> dict:
    """Award XP and update hero profile. Returns new totals."""
    # Apply active surge multiplier
    multiplier = await get_active_xp_multiplier()
    adjusted = int(amount * multiplier)

    async with async_session() as session:
        # Record in ledger
        await session.execute(text("""
            INSERT INTO xp_ledger (amount, reason, category, challenge_id)
            VALUES (:amount, :reason, :cat, :cid)
        """), {
            "amount": adjusted, "reason": reason,
            "cat": category, "cid": challenge_id,
        })

        # Update hero profile
        await session.execute(text("""
            UPDATE hero_profile
            SET total_xp = total_xp + :xp, updated_at = NOW()
        """), {"xp": adjusted})

        # Fetch new totals
        r = await session.execute(text(
            "SELECT total_xp, level, power_level FROM hero_profile LIMIT 1"
        ))
        row = r.fetchone()
        await session.commit()

    if not row:
        return {"awarded": adjusted, "total_xp": adjusted, "level": 1}

    total_xp = row[0]
    new_level = level_from_xp(total_xp)
    old_level = row[1]
    leveled_up = new_level > old_level

    # Update level if changed
    if leveled_up:
        new_title = title_from_level(new_level)
        async with async_session() as session:
            await session.execute(text("""
                UPDATE hero_profile
                SET level = :level, hero_name = :title, updated_at = NOW()
            """), {"level": new_level, "title": new_title})
            await session.commit()

    result = {
        "awarded": adjusted,
        "multiplier": multiplier,
        "total_xp": total_xp,
        "level": new_level,
        "leveled_up": leveled_up,
        "xp_to_next": xp_to_next_level(total_xp),
    }

    if leveled_up:
        result["new_title"] = title_from_level(new_level)
        logger.info("level_up", extra={"level": new_level, "total_xp": total_xp})

    logger.info("xp_awarded", extra={
        "amount": adjusted, "reason": reason, "total": total_xp,
    })
    return result


async def apply_penalty(penalty_type: str, challenge_id: int | None = None) -> dict:
    """Apply an XP penalty."""
    amount = XP_PENALTIES.get(penalty_type, -10)
    return await award_xp(amount, penalty_type, category="penalty", challenge_id=challenge_id)


async def award_battle_xp(outcome: dict, challenge_id: int | None = None) -> dict:
    """Award XP based on battle outcome."""
    base = XP_AWARDS.get("weekly_challenge_won", 200)
    xp_mult = outcome.get("xp_mult", 1.0)
    amount = int(base * xp_mult)

    if outcome.get("is_victory"):
        amount += XP_AWARDS.get("villain_defeated", 250)

    reason = f"{outcome.get('name', 'Battle')} vs {outcome.get('villain_name', 'Unknown')}"
    return await award_xp(amount, reason, category="battle", challenge_id=challenge_id)


async def award_nemesis_defeat(villain_name: str, challenge_id: int | None = None) -> dict:
    """Bonus XP for defeating a nemesis."""
    amount = XP_AWARDS.get("nemesis_defeated", 500)
    return await award_xp(amount, f"Nemesis Defeated: {villain_name}", category="nemesis", challenge_id=challenge_id)


# ── XP Summary ──

async def get_xp_summary() -> dict:
    """Get current XP state."""
    async with async_session() as session:
        r = await session.execute(text(
            "SELECT total_xp, level, hero_name FROM hero_profile LIMIT 1"
        ))
        row = r.fetchone()
        if not row:
            return {"total_xp": 0, "level": 1, "title": "Recruit", "xp_to_next": 100}

        total_xp, level, title = row[0], row[1], row[2]
        return {
            "total_xp": total_xp,
            "level": level,
            "title": title,
            "xp_to_next": xp_to_next_level(total_xp),
            "multiplier": await get_active_xp_multiplier(),
        }


async def get_xp_history(limit: int = 20) -> list[dict]:
    """Get recent XP transactions."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT amount, reason, category, created_at
            FROM xp_ledger ORDER BY created_at DESC LIMIT :lim
        """), {"lim": limit})
        return [
            {"amount": row[0], "reason": row[1], "category": row[2],
             "created_at": row[3].isoformat() if row[3] else None}
            for row in r.fetchall()
        ]


# ── Power Surge Management ──

async def get_active_xp_multiplier() -> float:
    """Get combined XP multiplier from active surges."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT xp_multiplier FROM power_surges
            WHERE is_active = TRUE AND expires_at > NOW()
        """))
        rows = r.fetchall()

    if not rows:
        return 1.0

    # Stack multiplicatively but cap at 2.0
    mult = 1.0
    for row in rows:
        mult *= row[0]
    return min(2.0, round(mult, 2))


async def get_active_surge_bonus() -> float:
    """Get combined battle bonus from active surges."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT battle_bonus FROM power_surges
            WHERE is_active = TRUE AND expires_at > NOW()
        """))
        rows = r.fetchall()

    return sum(row[0] for row in rows)


async def check_and_activate_surges() -> list[dict]:
    """Check if any power surge triggers have been met and activate them.

    Called periodically (e.g. after daily check-in or workout sync).
    """
    activated = []

    async with async_session() as session:
        # Check 7-day streak
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - INTERVAL '6 days'
        """))
        streak_7 = r.scalar() or 0

        # Check 14-day streak
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - INTERVAL '13 days'
        """))
        streak_14 = r.scalar() or 0

        # Check 30-day streak
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - INTERVAL '29 days'
        """))
        streak_30 = r.scalar() or 0

        # Check elite sleep week (5+ nights above 80% sleep performance)
        r = await session.execute(text("""
            SELECT COUNT(*) FROM whoop_sleep
            WHERE start_time >= NOW() - INTERVAL '7 days'
              AND sleep_performance >= 80
        """))
        elite_sleep = r.scalar() or 0

        # Check perfect nutrition adherence (7/7 days)
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= CURRENT_DATE - INTERVAL '6 days'
              AND nutrition_adherence >= 80
        """))
        perfect_nutrition = r.scalar() or 0

    # Map triggers to surge definitions
    trigger_map = {
        "streak_7": streak_7 >= 7,
        "streak_14": streak_14 >= 14,
        "streak_30": streak_30 >= 30,
        "elite_sleep_week": elite_sleep >= 5,
        "perfect_adherence": perfect_nutrition >= 7,
    }

    for surge_id, surge_def in POWER_SURGES.items():
        trigger = surge_def["trigger"]
        if trigger in trigger_map and trigger_map[trigger]:
            result = await _activate_surge(surge_id, surge_def)
            if result:
                activated.append(result)

    return activated


async def activate_pr_surge() -> dict | None:
    """Activate berserker_rage surge when a PR is achieved. Called externally."""
    surge_def = POWER_SURGES.get("berserker_rage")
    if surge_def:
        return await _activate_surge("berserker_rage", surge_def)
    return None


async def _activate_surge(surge_id: str, surge_def: dict) -> dict | None:
    """Activate a specific power surge if not already active."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=surge_def["duration_days"])

    async with async_session() as session:
        # Check if this surge type is already active
        r = await session.execute(text("""
            SELECT id FROM power_surges
            WHERE surge_type = :stype AND is_active = TRUE AND expires_at > NOW()
        """), {"stype": surge_id})

        if r.fetchone():
            return None  # Already active

        await session.execute(text("""
            INSERT INTO power_surges
                (surge_name, surge_type, xp_multiplier, battle_bonus,
                 activated_at, expires_at, trigger_reason, is_active)
            VALUES (:name, :stype, :xm, :bb, :now, :exp, :reason, TRUE)
        """), {
            "name": surge_def["name"],
            "stype": surge_id,
            "xm": surge_def["xp_multiplier"],
            "bb": surge_def["battle_bonus"],
            "now": now, "exp": expires,
            "reason": surge_def["trigger"],
        })
        await session.commit()

    logger.info("power_surge_activated", extra={
        "surge": surge_def["name"], "expires": expires.isoformat(),
    })

    return {
        "surge_name": surge_def["name"],
        "surge_type": surge_id,
        "xp_multiplier": surge_def["xp_multiplier"],
        "battle_bonus": surge_def["battle_bonus"],
        "duration_days": surge_def["duration_days"],
        "description": surge_def["description"],
    }


async def expire_surges() -> int:
    """Deactivate expired surges. Called periodically."""
    async with async_session() as session:
        r = await session.execute(text("""
            UPDATE power_surges SET is_active = FALSE
            WHERE is_active = TRUE AND expires_at <= NOW()
        """))
        await session.commit()
        count = r.rowcount
    if count:
        logger.info("surges_expired", extra={"count": count})
    return count


async def get_active_surges() -> list[dict]:
    """List all active power surges."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT surge_name, surge_type, xp_multiplier, battle_bonus,
                   activated_at, expires_at, trigger_reason
            FROM power_surges
            WHERE is_active = TRUE AND expires_at > NOW()
            ORDER BY activated_at DESC
        """))
        return [
            {
                "surge_name": row[0], "surge_type": row[1],
                "xp_multiplier": row[2], "battle_bonus": row[3],
                "activated_at": row[4].isoformat() if row[4] else None,
                "expires_at": row[5].isoformat() if row[5] else None,
                "trigger_reason": row[6],
            }
            for row in r.fetchall()
        ]
