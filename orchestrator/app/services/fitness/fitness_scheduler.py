"""Fitness platform sync scheduler.

Periodically syncs data from Whoop, Peloton, and Tonal.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.core.config import settings

logger = logging.getLogger("pai.fitness.scheduler")

_WHOOP_ALERT_COOLDOWN_HOURS = 12
_whoop_alert_active = False
_whoop_last_alert_at: datetime | None = None


async def fitness_sync_loop():
    """Background loop that syncs fitness data at configured intervals."""
    interval_hours = settings.fitness_sync_hours
    if interval_hours <= 0:
        logger.info("fitness_sync_disabled", extra={"interval_hours": interval_hours})
        return

    logger.info("fitness_sync_scheduler_started", extra={"interval_hours": interval_hours})

    while True:
        try:
            await run_fitness_sync()
        except Exception as e:
            logger.error("fitness_sync_run_failed", extra={"error": str(e)})

        await asyncio.sleep(interval_hours * 3600)


async def run_fitness_sync() -> dict:
    """Run a single sync cycle across all configured platforms."""
    from app.services.fitness.whoop_sync import sync_whoop
    from app.services.fitness.peloton_sync import sync_peloton
    from app.services.fitness.tonal_sync import sync_tonal

    results = {}

    for name, fn in [("whoop", sync_whoop), ("peloton", sync_peloton), ("tonal", sync_tonal)]:
        try:
            results[name] = await fn()
        except Exception as e:
            logger.error(f"fitness_sync_{name}_failed", extra={"error": str(e)})
            results[name] = {"status": "error", "error": str(e)}

    await _whoop_health_check_and_notify(results)

    logger.info("fitness_sync_cycle_complete", extra={"results": str(results)})
    return results


async def _whoop_health_check_and_notify(results: dict) -> None:
    """Send an email alert when WHOOP integration is unhealthy."""
    global _whoop_alert_active, _whoop_last_alert_at

    from app.services.fitness.fitness_query import _get_sync_status
    from app.services.gmail_service import send_system_alert

    whoop_result = results.get("whoop") or {}
    issues: list[str] = []

    status = str(whoop_result.get("status", "")).lower()
    if status in {"error", "failed"}:
        issues.append(f"sync status={status}")

    if status == "skipped":
        issues.append(f"sync skipped: {whoop_result.get('reason', 'unknown reason')}")

    errors = whoop_result.get("errors")
    if isinstance(errors, list) and errors:
        issues.extend([f"sync error: {e}" for e in errors])

    sync_rows = await _get_sync_status()
    whoop_status = next((r for r in sync_rows if r.get("platform") == "whoop"), None)
    if whoop_status:
        last_sync_at = whoop_status.get("last_sync_at")
        stale_after_hours = max(int(settings.fitness_sync_hours * 2), 8)
        if isinstance(last_sync_at, datetime):
            now = datetime.now(timezone.utc)
            last_sync_utc = last_sync_at if last_sync_at.tzinfo else last_sync_at.replace(tzinfo=timezone.utc)
            if now - last_sync_utc > timedelta(hours=stale_after_hours):
                issues.append(
                    f"last successful WHOOP sync is stale ({last_sync_utc.isoformat()} > {stale_after_hours}h old)"
                )

    if not issues:
        if _whoop_alert_active:
            await send_system_alert(
                subject="PAI Health Recovery: WHOOP integration restored",
                body=(
                    "WHOOP integration health check is back to normal.\n\n"
                    f"Checked at: {datetime.now(timezone.utc).isoformat()}\n"
                    "Manual verify: GET /fitness/sync/status"
                ),
            )
        _whoop_alert_active = False
        return

    now = datetime.now(timezone.utc)
    if _whoop_last_alert_at and now - _whoop_last_alert_at < timedelta(hours=_WHOOP_ALERT_COOLDOWN_HOURS):
        logger.warning("whoop_health_alert_suppressed", extra={"issues": issues})
        _whoop_alert_active = True
        return

    sent = await send_system_alert(
        subject="PAI Health Alert: WHOOP integration unhealthy",
        body=(
            "WHOOP integration health check detected issues.\n\n"
            f"Checked at: {now.isoformat()}\n"
            "Issues:\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\n\nRecommended action:\n"
            "1) Reconnect WHOOP via GET /whoop/auth\n"
            "2) Trigger POST /fitness/sync\n"
            "3) Verify GET /fitness/sync/status"
        ),
    )
    if sent:
        _whoop_last_alert_at = now
        _whoop_alert_active = True
