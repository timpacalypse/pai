"""Villain Challenge Scheduler — Monday briefings, daily updates, Sunday resolution."""

import asyncio
import logging
from datetime import date, datetime, timezone

logger = logging.getLogger("pai.villain.scheduler")

# Check every 2 hours
CHECK_INTERVAL_SECONDS = 7200


async def villain_challenge_loop():
    """Background loop that runs villain challenge lifecycle events."""
    logger.info("villain_challenge_scheduler_started")

    # Wait for startup to complete
    await asyncio.sleep(30)

    while True:
        try:
            await run_villain_cycle()
        except Exception as e:
            logger.error("villain_cycle_failed", extra={"error": str(e)})

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def run_villain_cycle():
    """Run a single villain challenge lifecycle check."""
    today = date.today()
    weekday = today.weekday()  # 0=Monday, 6=Sunday
    now = datetime.now(timezone.utc)
    hour = now.hour

    from app.services.villain_challenge.xp_engine import (
        expire_surges, check_and_activate_surges,
    )

    # Always: expire old surges
    await expire_surges()

    # Always: check for new surges
    surges = await check_and_activate_surges()
    if surges:
        logger.info("surges_activated", extra={"count": len(surges)})

    # Monday (any hour): Create new weekly challenge if needed
    if weekday == 0:
        await _maybe_create_weekly_challenge()

    # Sunday evening (18-22 UTC): Resolve weekly battle
    elif weekday == 6 and 18 <= hour <= 22:
        await _maybe_resolve_weekly_battle()

    # Daily: update objective progress from fitness data
    await _sync_objective_progress()


async def _maybe_create_weekly_challenge():
    """Create a new weekly challenge if one doesn't exist for this week."""
    from app.services.villain_challenge.villain_engine import (
        get_active_challenge, select_weekly_villain,
        generate_weekly_objectives, create_weekly_challenge,
    )
    from app.services.villain_challenge.hero_engine import get_hero_profile

    existing = await get_active_challenge()
    if existing:
        return  # Already have a challenge this week

    hero_data = await get_hero_profile()
    villain_selection = await select_weekly_villain(hero_data)
    objectives = await generate_weekly_objectives(villain_selection, hero_data)
    challenge = await create_weekly_challenge(villain_selection, objectives)

    logger.info("weekly_challenge_created", extra={
        "villain": villain_selection["villain_name"],
        "objectives": len(objectives),
    })


async def _maybe_resolve_weekly_battle():
    """Resolve the weekly battle if it hasn't been resolved yet."""
    from app.services.villain_challenge.villain_engine import get_active_challenge
    from app.services.villain_challenge.hero_engine import get_hero_profile
    from app.services.villain_challenge.battle_system import resolve_weekly_battle
    from app.services.villain_challenge.xp_engine import award_battle_xp

    challenge = await get_active_challenge()
    if not challenge or challenge.get("status") != "active":
        return

    hero_data = await get_hero_profile()
    outcome = await resolve_weekly_battle(challenge, hero_data)
    xp_result = await award_battle_xp(outcome, challenge_id=challenge.get("id"))

    logger.info("weekly_battle_resolved", extra={
        "villain": outcome.get("villain_name"),
        "outcome": outcome.get("name"),
        "xp": xp_result.get("awarded"),
    })


async def _sync_objective_progress():
    """Auto-update objective progress from fitness platform data."""
    from app.services.villain_challenge.villain_engine import get_active_challenge
    from app.core.database import async_session
    from sqlalchemy import text

    challenge = await get_active_challenge()
    if not challenge or challenge.get("status") != "active":
        return

    week_start = challenge.get("week_start")
    if isinstance(week_start, str):
        week_start = date.fromisoformat(week_start)

    ws_ts = datetime.combine(week_start, datetime.min.time())

    async def _safe_count(session, sql, params):
        """Query that returns 0 if the table doesn't exist yet."""
        try:
            r = await session.execute(text(sql), params)
            return r.scalar() or 0
        except Exception:
            await session.rollback()
            return 0

    async with async_session() as session:
        tonal_count = await _safe_count(session, """
            SELECT COUNT(*) FROM tonal_workouts
            WHERE start_time >= :ws::timestamp
        """, {"ws": ws_ts})

        peloton_count = await _safe_count(session, """
            SELECT COUNT(*) FROM peloton_workouts
            WHERE start_time >= :ws::timestamp
        """, {"ws": ws_ts})

        # Count daily check-ins this week
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= :ws
        """), {"ws": week_start})
        checkin_count = r.scalar() or 0

        sleep_target_count = await _safe_count(session, """
            SELECT COUNT(*) FROM whoop_sleep
            WHERE start_time >= :ws::timestamp
              AND sleep_performance >= 60
        """, {"ws": ws_ts})

        recovery_target_count = await _safe_count(session, """
            SELECT COUNT(*) FROM whoop_recovery
            WHERE created_at >= :ws::timestamp
              AND recovery_score >= 60
        """, {"ws": ws_ts})

        # Count nutrition target days
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= :ws
              AND nutrition_adherence >= 70
        """), {"ws": week_start})
        nutrition_days = r.scalar() or 0

        # Count mobility sessions
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= :ws
              AND mobility_done = TRUE
        """), {"ws": week_start})
        mobility_count = r.scalar() or 0

        # Count body weight logs
        r = await session.execute(text("""
            SELECT COUNT(*) FROM daily_checkins
            WHERE checkin_date >= :ws
              AND body_weight IS NOT NULL
        """), {"ws": week_start})
        weight_log_count = r.scalar() or 0

        # Map objective types to absolute current values
        progress_map = {
            "tonal_workouts": tonal_count,
            "peloton_rides": peloton_count,
            "checkin_streak": checkin_count,
            "sleep_target": sleep_target_count,
            "recovery_target": recovery_target_count,
            "nutrition_target": nutrition_days,
            "mobility_sessions": mobility_count,
            "weight_logs": weight_log_count,
        }

        # Set absolute values (not increments) for each objective
        for obj in challenge.get("objectives", []):
            obj_type = obj.get("objective_type")
            if obj_type in progress_map and not obj.get("completed"):
                new_val = min(obj["target_value"], progress_map[obj_type])
                completed = new_val >= obj["target_value"]
                if new_val != obj.get("current_value", 0):
                    await session.execute(text("""
                        UPDATE challenge_objectives
                        SET current_value = :val, completed = :done
                        WHERE id = :id
                    """), {"val": new_val, "done": completed, "id": obj["id"]})

        await session.commit()
