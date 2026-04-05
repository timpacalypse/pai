"""Daily briefing scheduler — sends morning briefing email on a configurable schedule."""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.services.briefing_scheduler")


async def briefing_scheduler_loop():
    """Background loop that sends a daily briefing email."""
    interval_hours = settings.briefing_schedule_hours
    if not interval_hours or interval_hours <= 0:
        logger.info("briefing_scheduler_disabled")
        return

    logger.info("briefing_scheduler_started", extra={"interval_hours": interval_hours})

    while True:
        await asyncio.sleep(interval_hours * 3600)

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
