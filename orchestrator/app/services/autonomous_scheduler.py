"""Autonomous workflow scheduler — triggers background tasks based on calendar, goals, and schedules."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.core.config import settings
from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.autonomous_scheduler")


async def autonomous_scheduler_loop():
    """Background loop that runs autonomous workflows on a schedule."""
    interval_hours = settings.autonomous_schedule_hours
    if not interval_hours or interval_hours <= 0:
        logger.info("autonomous_scheduler_disabled")
        return

    logger.info("autonomous_scheduler_started", extra={"interval_hours": interval_hours})

    while True:
        await asyncio.sleep(interval_hours * 3600)

        logger.info("autonomous_scheduler_trigger")
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Run all autonomous tasks
                await _daily_security_scan(client)
                await _check_upcoming_meetings(client)
                await _weekly_goal_check(client)
                await _learning_loop(client)
        except Exception:
            logger.exception("autonomous_scheduler_error")


async def _daily_security_scan(http_client: httpx.AsyncClient):
    """Trigger a scheduled research run for AI security topics."""
    try:
        from app.services.scheduler import run_scheduled_research
        result = await run_scheduled_research(
            max_results_per_topic=5,
            time_filter="d",
            send_email=False,  # Don't spam — briefing handles email
        )
        logger.info("auto_security_scan_complete", extra={
            "new_articles": result.get("new_articles", 0),
            "total_found": result.get("total_found", 0),
        })
    except Exception as e:
        logger.error("auto_security_scan_failed", extra={"error": str(e)})


async def _check_upcoming_meetings(http_client: httpx.AsyncClient):
    """Check calendar for meetings in the next 24h and run pre-meeting research if needed."""
    try:
        from app.services.calendar_service import get_events
        events = await get_events(upcoming_days=1)

        research_categories = {"appointment", "deadline", "activity"}
        actionable = [e for e in events if e.get("category") in research_categories]

        if not actionable:
            return

        from app.services.web_search_service import search_and_extract
        from app.memory.semantic import store_semantic

        for event in actionable[:3]:  # Cap at 3 to avoid overload
            title = event.get("title", "")
            if not title:
                continue

            # Search for context about the meeting topic
            results = await search_and_extract(
                query=title,
                max_results=3,
                time_filter="m",
                http_client=http_client,
                extract_bodies=True,
                max_extract=2,
            )

            # Store any found context in semantic memory
            for r in results:
                if r.body and len(r.body) > 100:
                    await store_semantic(
                        content=f"Pre-meeting context for '{title}': {r.body[:500]}",
                        source=r.url,
                        metadata={"type": "pre_meeting_research", "event": title},
                        http_client=http_client,
                    )

            logger.info("pre_meeting_research", extra={
                "event": title,
                "articles_found": len(results),
            })

    except Exception as e:
        logger.error("pre_meeting_check_failed", extra={"error": str(e)})


async def _weekly_goal_check(http_client: httpx.AsyncClient):
    """Weekly: compare recent activity against identity_memory goals and generate insights."""
    # Only run on Sundays
    if datetime.now(timezone.utc).weekday() != 6:
        return

    try:
        from sqlalchemy import text

        # Get role goals from identity_memory
        async with async_session() as session:
            result = await session.execute(
                text("SELECT role, domain, goals FROM identity_memory ORDER BY domain")
            )
            roles = [dict(r) for r in result.mappings()]

        # Get recent activity summary from episodic_memory
        async with async_session() as session:
            result = await session.execute(
                text(
                    "SELECT role, request_type, COUNT(*) as count "
                    "FROM episodic_memory "
                    "WHERE created_at > NOW() - INTERVAL '7 days' "
                    "GROUP BY role, request_type ORDER BY count DESC"
                )
            )
            activity = [dict(r) for r in result.mappings()]

        if not activity:
            logger.info("weekly_goal_check_skip_no_activity")
            return

        # Use LLM to assess goal alignment
        goals_text = json.dumps(roles, indent=2, default=str)
        activity_text = json.dumps(activity, indent=2, default=str)

        system_prompt = (
            "You analyze a user's weekly activity against their stated life goals and roles. "
            "Identify: areas of strong alignment, neglected goals, and specific recommendations "
            "for the coming week. Be constructive and actionable.\n\n"
            "Respond ONLY with valid JSON:\n"
            '{"aligned_goals": ["..."], "neglected_goals": ["..."], '
            '"recommendations": ["..."], "summary": "brief overall assessment"}'
        )
        user_prompt = (
            f"Weekly Goal Alignment Check\n\n"
            f"My roles and goals:\n{goals_text}\n\n"
            f"This week's activity:\n{activity_text}\n\n"
            "How well did my activity align with my goals?"
        )

        raw = await generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            http_client=http_client,
        )

        # Store the analysis in semantic memory
        from app.memory.semantic import store_semantic
        await store_semantic(
            content=f"Weekly goal alignment analysis: {raw}",
            source="autonomous_scheduler",
            metadata={"type": "weekly_goal_check", "date": datetime.now(timezone.utc).isoformat()},
            http_client=http_client,
        )

        logger.info("weekly_goal_check_complete")

    except Exception as e:
        logger.error("weekly_goal_check_failed", extra={"error": str(e)})


async def _learning_loop(http_client: httpx.AsyncClient):
    """
    Automated learning loop:
    1. Evaluate any promoted experiments that have enough data
    2. If no active experiments, generate a new improvement hypothesis
    3. Auto-promote new experiments so the override takes effect immediately
    
    The evaluation step will auto-rollback experiments that hurt performance.
    """
    try:
        from app.services.learning_service import (
            get_experiments, evaluate_experiment, generate_improvement,
            promote_experiment, get_active_overrides,
        )

        # Step 1: Evaluate any promoted experiments that have been running
        promoted = await get_experiments(status="promoted", limit=5)
        for exp in promoted:
            eid = exp.get("experiment_id", "")
            if not eid:
                continue
            result = await evaluate_experiment(eid, min_samples=10, http_client=http_client)
            status = result.get("verdict") or result.get("status", "")
            logger.info("learning_loop_evaluated", extra={
                "experiment_id": eid,
                "verdict": status,
                "delta": result.get("delta"),
                "sample_count": result.get("sample_count"),
            })

        # Step 2: If no active overrides remain, generate and promote a new experiment
        active = await get_active_overrides()
        if not active:
            gen_result = await generate_improvement(http_client=http_client)
            if gen_result.get("status") == "created":
                eid = gen_result["experiment_id"]
                # Auto-promote so the override takes effect for data collection
                promote_result = await promote_experiment(eid)
                logger.info("learning_loop_new_experiment", extra={
                    "experiment_id": eid,
                    "improvement": gen_result.get("improvement", {}).get("description", ""),
                    "promote_status": promote_result.get("status"),
                })
            else:
                logger.info("learning_loop_skip", extra={
                    "reason": gen_result.get("reason", gen_result.get("status", "unknown")),
                })

    except Exception as e:
        logger.error("learning_loop_failed", extra={"error": str(e)})
