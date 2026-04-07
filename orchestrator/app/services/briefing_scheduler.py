"""Daily briefing scheduler — sends morning briefing email at a fixed local time."""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone, tzinfo

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.services.briefing_scheduler")

# US Eastern: UTC-5 (EST) / UTC-4 (EDT)
# Simple DST rule: 2nd Sunday of March to 1st Sunday of November
class _Eastern(tzinfo):
    _ZERO = timedelta(0)
    _EDT = timedelta(hours=-4)
    _EST = timedelta(hours=-5)

    def utcoffset(self, dt):
        return self._EDT if self._is_dst(dt) else self._EST

    def tzname(self, dt):
        return "EDT" if self._is_dst(dt) else "EST"

    def dst(self, dt):
        return timedelta(hours=1) if self._is_dst(dt) else self._ZERO

    @staticmethod
    def _is_dst(dt):
        if dt is None:
            return False
        # 2nd Sunday of March
        mar1 = datetime(dt.year, 3, 1)
        spring = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
        # 1st Sunday of November
        nov1 = datetime(dt.year, 11, 1)
        fall = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        return spring <= dt.replace(tzinfo=None) < fall

ET = _Eastern()
BRIEFING_TIME = time(5, 30)  # 5:30 AM Eastern


def _seconds_until_target() -> float:
    """Return seconds until the next 5:30 AM Eastern."""
    now_et = datetime.now(ET)
    target_today = datetime.combine(now_et.date(), BRIEFING_TIME, tzinfo=ET)
    if now_et >= target_today:
        target = target_today + timedelta(days=1)
    else:
        target = target_today
    # Recalculate with correct DST for the target date
    target = datetime.combine(target.date(), BRIEFING_TIME, tzinfo=ET)
    return (target - now_et).total_seconds()


async def briefing_scheduler_loop():
    """Background loop that sends a daily briefing email at 5:30 AM Eastern."""
    interval_hours = settings.briefing_schedule_hours
    if not interval_hours or interval_hours <= 0:
        logger.info("briefing_scheduler_disabled")
        return

    logger.info("briefing_scheduler_started", extra={
        "target_time": "05:30 ET",
        "next_in_seconds": int(_seconds_until_target()),
    })

    while True:
        wait = _seconds_until_target()
        logger.info("briefing_scheduler_sleeping", extra={
            "next_send_in_hours": round(wait / 3600, 1),
        })
        await asyncio.sleep(wait)

        if not settings.gmail_address:
            continue

        logger.info("briefing_scheduler_trigger")
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                from app.services.briefing_service import send_daily_briefing
                sent = await send_daily_briefing(http_client=client)
                if sent:
                    logger.info("briefing_scheduler_sent")
                else:
                    logger.warning("briefing_scheduler_not_sent")
        except Exception:
            logger.exception("briefing_scheduler_error")

        # Sleep past the target minute to avoid double-fire
        await asyncio.sleep(120)
