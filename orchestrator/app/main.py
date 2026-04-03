from fastapi import FastAPI
from contextlib import asynccontextmanager
import logging
import sys

import httpx
import redis.asyncio as aioredis

from app.core.config import settings
from app.core.middleware import RequestLoggingMiddleware
from app.api.routes import router
from app.services.role_service import load_roles

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
    yield
    # Shutdown
    await app.state.redis.aclose()
    await app.state.http_client.aclose()


app = FastAPI(
    title="PAI Orchestrator",
    description="Personal AI Orchestration Platform — Control Plane",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)
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
