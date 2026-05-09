"""Fitness platform sync scheduler.

Periodically syncs data from Whoop, Peloton, and Tonal.
"""

import asyncio
import logging

from app.core.config import settings

logger = logging.getLogger("pai.fitness.scheduler")


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

    logger.info("fitness_sync_cycle_complete", extra={"results": str(results)})
    return results
