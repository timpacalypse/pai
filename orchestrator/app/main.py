from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import sys

import asyncio

import httpx
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.middleware import RequestLoggingMiddleware
from app.api.routes import router
from app.services.role_service import load_roles
from app.services.scheduler import scheduler_loop
from app.services.home_alert_scheduler import home_alert_scheduler_loop
from app.services.briefing_scheduler import briefing_scheduler_loop
from app.services.autonomous_scheduler import autonomous_scheduler_loop
from app.services.fitness.fitness_scheduler import fitness_sync_loop
from app.services.villain_challenge.scheduler import villain_challenge_loop

# ── Structured logging ──
logging.basicConfig(
    level=getattr(logging, settings.orchestrator_log_level.upper(), logging.INFO),
    format='{"time":"%(asctime)s","name":"%(name)s","level":"%(levelname)s","msg":"%(message)s"}',
    stream=sys.stdout,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.http_client = httpx.AsyncClient(timeout=120.0)
    await load_roles()

    # Seed process engine definitions
    from app.services.process_engine import seed_process_definitions
    try:
        await seed_process_definitions()
    except Exception as e:
        logger.warning(f"Process seed failed (table may not exist yet): {e}")

    # Register skill registry (built-in + process definitions)
    from app.services.skill_registry import register_all_skills
    register_all_skills()
    try:
        from app.services.skill_registry import register_process_skills
        await register_process_skills()
    except Exception as e:
        logger.warning(f"Process skill registration failed: {e}")

    # Start background schedulers
    scheduler_task = asyncio.create_task(scheduler_loop())
    app.state.scheduler_task = scheduler_task

    home_alert_task = asyncio.create_task(home_alert_scheduler_loop())
    app.state.home_alert_task = home_alert_task

    briefing_task = asyncio.create_task(briefing_scheduler_loop())
    app.state.briefing_task = briefing_task

    autonomous_task = asyncio.create_task(autonomous_scheduler_loop())
    app.state.autonomous_task = autonomous_task

    fitness_task = asyncio.create_task(fitness_sync_loop())
    app.state.fitness_task = fitness_task

    villain_task = asyncio.create_task(villain_challenge_loop())
    app.state.villain_task = villain_task

    yield

    # Shutdown
    for task in (scheduler_task, home_alert_task, briefing_task, autonomous_task, fitness_task, villain_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await app.state.redis.aclose()
    await app.state.http_client.aclose()


app = FastAPI(
    title="PAI Orchestrator",
    description="Personal AI Orchestration Platform — Control Plane",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://pai-frontend:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/health")
async def health():
    """Health check that verifies connectivity to all backing services."""
    checks = {}

    # Redis
    try:
        pong = await app.state.redis.ping()
        checks["redis"] = "ok" if pong else "error"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # Ollama
    try:
        resp = await app.state.http_client.get(f"{settings.ollama_url}/api/tags")
        checks["ollama"] = "ok" if resp.status_code == 200 else f"error: {resp.status_code}"
    except Exception as e:
        checks["ollama"] = f"error: {e}"

    status = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
