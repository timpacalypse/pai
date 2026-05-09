"""Unified fitness data query service.

Queries across all fitness platforms (Whoop, Peloton, Tonal) and provides
data summaries for LLM-powered analysis and recommendations.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.fitness.query")


async def get_fitness_summary(days: int = 7) -> str:
    """Get a comprehensive fitness summary for the last N days.

    Returns formatted text ready for LLM context injection.
    """
    parts = []

    workouts = await _get_recent_workouts(days)
    if workouts:
        parts.append(_format_workouts(workouts, days))

    recovery = await _get_recent_recovery(days)
    if recovery:
        parts.append(_format_recovery(recovery, days))

    sleep = await _get_recent_sleep(days)
    if sleep:
        parts.append(_format_sleep(sleep, days))

    strength = await _get_recent_strength(days)
    if strength:
        parts.append(_format_strength(strength, days))

    sync_status = await _get_sync_status()
    if sync_status:
        parts.append(_format_sync_status(sync_status))

    if not parts:
        return "No fitness data available. Configure your Whoop, Peloton, or Tonal credentials to start syncing."

    return "\n\n".join(parts)


async def get_workout_details(days: int = 7, platform: str = "") -> str:
    """Get detailed workout data."""
    workouts = await _get_recent_workouts(days, platform)
    if not workouts:
        return f"No workouts found in the last {days} days."
    return _format_workouts(workouts, days, detailed=True)


async def get_recovery_trends(days: int = 14) -> str:
    """Get recovery and HRV trend data."""
    recovery = await _get_recent_recovery(days)
    if not recovery:
        return f"No recovery data found in the last {days} days."
    return _format_recovery(recovery, days, detailed=True)


async def get_sleep_analysis(days: int = 14) -> str:
    """Get sleep trend data."""
    sleep = await _get_recent_sleep(days)
    if not sleep:
        return f"No sleep data found in the last {days} days."
    return _format_sleep(sleep, days, detailed=True)


async def get_strength_progress(days: int = 30) -> str:
    """Get Tonal strength score trends."""
    strength = await _get_recent_strength(days)
    if not strength:
        return f"No strength data found in the last {days} days."
    return _format_strength(strength, days, detailed=True)


async def trigger_sync() -> str:
    """Manually trigger a sync of all configured platforms."""
    results = []

    from app.services.fitness.whoop_sync import sync_whoop
    from app.services.fitness.peloton_sync import sync_peloton
    from app.services.fitness.tonal_sync import sync_tonal

    for name, fn in [("Whoop", sync_whoop), ("Peloton", sync_peloton), ("Tonal", sync_tonal)]:
        try:
            result = await fn()
            status = result.get("status", "ok")
            if status == "skipped":
                results.append(f"{name}: skipped ({result.get('reason', '')})")
            else:
                synced = sum(v for k, v in result.items() if isinstance(v, int))
                results.append(f"{name}: synced {synced} records")
        except Exception as e:
            results.append(f"{name}: error — {e}")

    return "Fitness sync results:\n" + "\n".join(f"  • {r}" for r in results)


# ── DB query functions ──


async def _get_recent_workouts(days: int, platform: str = "") -> list[dict]:
    async with async_session() as session:
        where = "WHERE start_time > NOW() - INTERVAL ':days days'"
        params: dict = {}
        if platform:
            where += " AND platform = :platform"
            params["platform"] = platform

        # SQLAlchemy text() doesn't support :days in INTERVAL, use explicit
        result = await session.execute(
            text(f"""
                SELECT platform, sport_name, title, start_time, duration_seconds,
                       calories_kj, distance_meters, avg_heart_rate, max_heart_rate,
                       strain, metrics
                FROM fitness_workouts
                WHERE start_time > NOW() - make_interval(days => CAST(:days AS INTEGER))
                {"AND platform = :platform" if platform else ""}
                ORDER BY start_time DESC
                LIMIT 50
            """),
            {"days": days, **params},
        )
        return [dict(r) for r in result.mappings()]


async def _get_recent_recovery(days: int) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT platform, record_date, recovery_score, resting_heart_rate,
                       hrv_rmssd, spo2_percentage, skin_temp_celsius
                FROM fitness_recovery
                WHERE record_date > CURRENT_DATE - make_interval(days => CAST(:days AS INTEGER))
                ORDER BY record_date DESC
                LIMIT 50
            """),
            {"days": days},
        )
        return [dict(r) for r in result.mappings()]


async def _get_recent_sleep(days: int) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT platform, start_time, total_duration_seconds,
                       sleep_performance, sleep_efficiency, respiratory_rate,
                       is_nap, stage_summary
                FROM fitness_sleep
                WHERE start_time > NOW() - make_interval(days => CAST(:days AS INTEGER))
                AND is_nap = FALSE
                ORDER BY start_time DESC
                LIMIT 30
            """),
            {"days": days},
        )
        return [dict(r) for r in result.mappings()]


async def _get_recent_strength(days: int) -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            text("""
                SELECT platform, workout_title, workout_type, start_time,
                       total_volume, total_reps, duration_seconds,
                       strength_scores, sets
                FROM fitness_strength
                WHERE start_time > NOW() - make_interval(days => CAST(:days AS INTEGER))
                ORDER BY start_time DESC
                LIMIT 50
            """),
            {"days": days},
        )
        return [dict(r) for r in result.mappings()]


async def _get_sync_status() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT platform, last_sync_at, status, records_synced FROM fitness_sync_state ORDER BY platform")
        )
        return [dict(r) for r in result.mappings()]


# ── Formatting functions ──


def _format_workouts(workouts: list[dict], days: int, detailed: bool = False) -> str:
    lines = [f"WORKOUTS (last {days} days): {len(workouts)} sessions"]

    # Group by platform
    by_platform: dict[str, list] = {}
    for w in workouts:
        by_platform.setdefault(w["platform"], []).append(w)

    for platform, ws in by_platform.items():
        total_duration = sum(w.get("duration_seconds", 0) for w in ws)
        avg_hr = [w["avg_heart_rate"] for w in ws if w.get("avg_heart_rate")]
        total_cal = sum(w.get("calories_kj", 0) for w in ws)

        lines.append(f"\n  {platform.upper()} ({len(ws)} workouts):")
        lines.append(f"    Total duration: {total_duration // 3600}h {(total_duration % 3600) // 60}m")
        if total_cal:
            lines.append(f"    Total energy: {total_cal:.0f} kJ")
        if avg_hr:
            lines.append(f"    Avg HR range: {min(avg_hr)}-{max(avg_hr)} bpm")

        if detailed:
            for w in ws[:10]:
                dur = w.get("duration_seconds", 0)
                st = w.get('start_time', '')
                st_str = st.strftime('%Y-%m-%d') if hasattr(st, 'strftime') else str(st)[:10]
                lines.append(
                    f"    • {st_str} — {w.get('title') or w.get('sport_name', '?')} "
                    f"({dur // 60}m, HR {w.get('avg_heart_rate', '?')}/{w.get('max_heart_rate', '?')}"
                    f"{', strain ' + str(round(w.get('strain', 0), 1)) if w.get('strain') else ''})"
                )

    return "\n".join(lines)


def _format_recovery(recovery: list[dict], days: int, detailed: bool = False) -> str:
    lines = [f"RECOVERY (last {days} days): {len(recovery)} entries"]

    scores = [r["recovery_score"] for r in recovery if r.get("recovery_score")]
    hrvs = [r["hrv_rmssd"] for r in recovery if r.get("hrv_rmssd")]
    rhrs = [r["resting_heart_rate"] for r in recovery if r.get("resting_heart_rate")]

    if scores:
        lines.append(f"  Recovery score: avg {sum(scores)/len(scores):.0f}% (range {min(scores):.0f}%-{max(scores):.0f}%)")
    if hrvs:
        lines.append(f"  HRV (RMSSD): avg {sum(hrvs)/len(hrvs):.1f}ms (range {min(hrvs):.1f}-{max(hrvs):.1f}ms)")
    if rhrs:
        lines.append(f"  Resting HR: avg {sum(rhrs)/len(rhrs):.0f} bpm (range {min(rhrs):.0f}-{max(rhrs):.0f})")

    if detailed:
        for r in recovery[:10]:
            lines.append(
                f"  • {r.get('record_date', '?')} — Recovery {r.get('recovery_score', '?')}%, "
                f"HRV {r.get('hrv_rmssd', '?')}ms, RHR {r.get('resting_heart_rate', '?')} bpm"
                f"{', SpO2 ' + str(r.get('spo2_percentage', '')) + '%' if r.get('spo2_percentage') else ''}"
            )

    return "\n".join(lines)


def _format_sleep(sleep: list[dict], days: int, detailed: bool = False) -> str:
    lines = [f"SLEEP (last {days} days): {len(sleep)} nights"]

    durations = [s["total_duration_seconds"] for s in sleep if s.get("total_duration_seconds")]
    perfs = [s["sleep_performance"] for s in sleep if s.get("sleep_performance")]
    effs = [s["sleep_efficiency"] for s in sleep if s.get("sleep_efficiency")]

    if durations:
        avg_hrs = (sum(durations) / len(durations)) / 3600
        lines.append(f"  Avg sleep duration: {avg_hrs:.1f} hours")
    if perfs:
        lines.append(f"  Sleep performance: avg {sum(perfs)/len(perfs):.0f}%")
    if effs:
        lines.append(f"  Sleep efficiency: avg {sum(effs)/len(effs):.0f}%")

    if detailed:
        for s in sleep[:10]:
            dur_h = (s.get("total_duration_seconds", 0) or 0) / 3600
            lines.append(
                f"  • {str(s.get('start_time', '?'))[:10]} — {dur_h:.1f}h, "
                f"perf {s.get('sleep_performance', '?')}%, eff {s.get('sleep_efficiency', '?')}%"
            )

    return "\n".join(lines)


def _format_strength(strength: list[dict], days: int, detailed: bool = False) -> str:
    # Split actual workouts from score history
    workouts = [s for s in strength if s.get("workout_type") not in ("ASSESSMENT", "SCORE_HISTORY")]
    scores = [s for s in strength if s.get("workout_type") in ("ASSESSMENT", "SCORE_HISTORY")]

    lines = [f"STRENGTH/TONAL (last {days} days): {len(workouts)} workouts"]

    if workouts:
        total_vol = sum(w.get("total_volume", 0) for w in workouts)
        total_reps = sum(w.get("total_reps", 0) for w in workouts)
        lines.append(f"  Total volume: {total_vol:,.0f} lbs")
        lines.append(f"  Total reps: {total_reps:,}")

    # Latest strength score
    latest_score = next(
        (s for s in scores if s.get("strength_scores", {}).get("regions")),
        None,
    )
    if latest_score:
        regions = latest_score["strength_scores"].get("regions", {})
        lines.append(f"  Strength scores: Overall {regions.get('Overall', '?')}, "
                     f"Upper {regions.get('Upper', '?')}, Lower {regions.get('Lower', '?')}, "
                     f"Core {regions.get('Core', '?')}")

    # Score trend
    score_history = [s for s in scores if s.get("workout_type") == "SCORE_HISTORY" and s.get("strength_scores", {}).get("overall")]
    if len(score_history) >= 2:
        latest = score_history[0]["strength_scores"]["overall"]
        oldest = score_history[-1]["strength_scores"]["overall"]
        delta = latest - oldest
        lines.append(f"  Score trend: {'+' if delta >= 0 else ''}{delta} over {len(score_history)} entries")

    if detailed:
        for w in workouts[:10]:
            dur = w.get("duration_seconds", 0)
            lines.append(
                f"  • {str(w.get('start_time', '?'))[:10]} — {w.get('workout_title', '?')} "
                f"({dur // 60}m, {w.get('total_volume', 0):,.0f} lbs, {w.get('total_reps', 0)} reps)"
            )

        if latest_score and latest_score["strength_scores"].get("muscles"):
            lines.append("\n  Muscle breakdown:")
            for muscle, info in latest_score["strength_scores"]["muscles"].items():
                lines.append(f"    {muscle}: {info.get('score', '?')} ({info.get('region', '')})")

    return "\n".join(lines)


def _format_sync_status(status: list[dict]) -> str:
    lines = ["SYNC STATUS:"]
    for s in status:
        last = s.get("last_sync_at")
        last_str = str(last)[:19] if last else "never"
        lines.append(f"  {s['platform']}: {s.get('status', '?')} — last sync {last_str}, {s.get('records_synced', 0)} total records")
    return "\n".join(lines)
