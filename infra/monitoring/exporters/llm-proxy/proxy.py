"""
PAI LLM Gateway Proxy

A transparent proxy for LLM APIs that tracks token usage, caches responses,
and logs all requests locally. No data leaves your network.

Supported backends:
  - Anthropic (Claude): /anthropic/v1/messages
  - OpenAI:             /openai/v1/chat/completions
  - Ollama (local):     /ollama/api/...

Metrics: http://localhost:9619/metrics
"""
import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import sqlite3

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger("llm-proxy")

# ── Config ────────────────────────────────────────────────────────────────────

ANTHROPIC_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://192.168.0.58:11434")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))  # 1 hour default
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.97"))
AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit.db")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8082"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9619"))

# ── Prometheus Metrics ────────────────────────────────────────────────────────

REQUEST_COUNT = Counter(
    'llm_proxy_requests_total', 'Total LLM requests',
    ['provider', 'model', 'cache_hit']
)
TOKEN_INPUT = Counter(
    'llm_proxy_input_tokens_total', 'Total input tokens',
    ['provider', 'model']
)
TOKEN_OUTPUT = Counter(
    'llm_proxy_output_tokens_total', 'Total output tokens',
    ['provider', 'model']
)
COST_USD = Counter(
    'llm_proxy_cost_usd_total', 'Estimated cost in USD',
    ['provider', 'model']
)
LATENCY = Histogram(
    'llm_proxy_latency_seconds', 'Request latency',
    ['provider', 'model']
)
CACHE_HITS = Counter(
    'llm_proxy_cache_hits_total', 'Cache hits',
    ['provider', 'type']  # type: exact, semantic
)
REQUESTS_TODAY = Gauge(
    'llm_proxy_requests_today', 'Requests made today',
    ['provider']
)

# ── Pricing (per million tokens) ─────────────────────────────────────────────

PRICING = {
    # Anthropic (API may return short or long model names)
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-haiku-4-20250414": {"input": 0.80, "output": 4.0},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
    # OpenAI
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.0, "output": 30.0},
    "o1": {"input": 15.0, "output": 60.0},
    # Ollama (free/local)
    "_local": {"input": 0.0, "output": 0.0},
}


def get_pricing(model: str) -> dict:
    return PRICING.get(model, PRICING.get("_local", {"input": 0.0, "output": 0.0}))


# ── Globals ───────────────────────────────────────────────────────────────────

redis_pool: redis.Redis | None = None
http_client: httpx.AsyncClient | None = None
audit_db: sqlite3.Connection | None = None


# ── Audit DB ──────────────────────────────────────────────────────────────────

def init_audit_db():
    global audit_db
    os.makedirs(os.path.dirname(AUDIT_DB), exist_ok=True)
    audit_db = sqlite3.connect(AUDIT_DB, check_same_thread=False)
    audit_db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            latency_ms INTEGER DEFAULT 0,
            cache_hit TEXT DEFAULT 'none',
            request_hash TEXT,
            request_body TEXT,
            response_body TEXT,
            status_code INTEGER
        )
    """)
    audit_db.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_timestamp ON requests(timestamp)
    """)
    audit_db.execute("""
        CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider, model)
    """)
    audit_db.commit()


def log_audit(provider: str, model: str, input_tokens: int, output_tokens: int,
              cost: float, latency_ms: int, cache_hit: str, request_hash: str,
              request_body: str, response_body: str, status_code: int):
    if audit_db:
        try:
            audit_db.execute(
                """INSERT INTO requests
                   (timestamp, provider, model, input_tokens, output_tokens, cost_usd,
                    latency_ms, cache_hit, request_hash, request_body, response_body, status_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), provider, model,
                 input_tokens, output_tokens, cost, latency_ms, cache_hit,
                 request_hash, request_body, response_body, status_code)
            )
            audit_db.commit()
        except Exception as e:
            logger.error(f"Audit log failed: {e}")


# ── Caching ───────────────────────────────────────────────────────────────────

def compute_request_hash(provider: str, body: dict) -> str:
    """Deterministic hash of the request for exact-match caching."""
    # Strip non-deterministic fields
    cache_body = body.copy()
    cache_body.pop("stream", None)
    cache_body.pop("metadata", None)
    raw = f"{provider}:{json.dumps(cache_body, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_cached_response(request_hash: str) -> dict | None:
    """Check Redis for exact-match cached response."""
    if not redis_pool:
        return None
    cached = await redis_pool.get(f"llm_cache:{request_hash}")
    if cached:
        return json.loads(cached)
    return None


async def set_cached_response(request_hash: str, response: dict):
    """Store response in Redis cache."""
    if not redis_pool:
        return
    await redis_pool.set(
        f"llm_cache:{request_hash}",
        json.dumps(response),
        ex=CACHE_TTL
    )


async def get_embedding(text: str) -> list[float] | None:
    """Get embedding from local Ollama for semantic dedup."""
    try:
        resp = await http_client.post(
            f"{OLLAMA_BASE}/api/embed",
            json={"model": EMBEDDING_MODEL, "input": text},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("embeddings", [[]])[0]
    except Exception as e:
        logger.debug(f"Embedding failed: {e}")
    return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def check_semantic_cache(provider: str, body: dict) -> dict | None:
    """Check for semantically similar cached requests."""
    if not redis_pool:
        return None

    # Extract the user message content for embedding
    messages = body.get("messages", [])
    if not messages:
        return None
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if not last_user:
        return None

    content = last_user.get("content", "")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")

    if len(content) < 20:  # Too short for meaningful semantic match
        return None

    query_embedding = await get_embedding(content)
    if not query_embedding:
        return None

    # Check recent semantic cache entries
    keys = await redis_pool.keys("llm_semantic:*")
    for key in keys[:100]:  # Limit search scope
        cached_data = await redis_pool.get(key)
        if not cached_data:
            continue
        entry = json.loads(cached_data)
        if entry.get("provider") != provider:
            continue
        if entry.get("model") != body.get("model"):
            continue

        sim = cosine_similarity(query_embedding, entry.get("embedding", []))
        if sim >= SIMILARITY_THRESHOLD:
            logger.info(f"Semantic cache hit (similarity={sim:.4f})")
            return entry.get("response")

    return None


async def store_semantic_cache(provider: str, body: dict, response: dict):
    """Store response with embedding for semantic dedup."""
    if not redis_pool:
        return

    messages = body.get("messages", [])
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if not last_user:
        return

    content = last_user.get("content", "")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")

    if len(content) < 20:
        return

    embedding = await get_embedding(content)
    if not embedding:
        return

    cache_key = f"llm_semantic:{compute_request_hash(provider, body)}"
    await redis_pool.set(cache_key, json.dumps({
        "provider": provider,
        "model": body.get("model"),
        "embedding": embedding,
        "response": response
    }), ex=CACHE_TTL)


# ── Provider Handlers ─────────────────────────────────────────────────────────

async def proxy_anthropic(request: Request, body: dict) -> tuple[Response, dict]:
    """Proxy request to Anthropic API."""
    headers = dict(request.headers)
    # Forward auth + required headers
    forward_headers = {
        "content-type": "application/json",
        "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
    }
    if "x-api-key" in headers:
        forward_headers["x-api-key"] = headers["x-api-key"]
    if "anthropic-beta" in headers:
        forward_headers["anthropic-beta"] = headers["anthropic-beta"]

    path = request.url.path.replace("/anthropic", "", 1)
    url = f"{ANTHROPIC_BASE}{path}"

    # Streaming: buffer full response and parse SSE for usage
    if body.get("stream"):
        resp = await http_client.post(url, json=body, headers=forward_headers, timeout=120)
        meta = {"model": body.get("model", "unknown")}
        if resp.status_code == 200:
            # Parse SSE events for usage data in message_start and message_delta
            for line in resp.text.split("\n"):
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                    etype = event.get("type", "")
                    if etype == "message_start":
                        msg = event.get("message", {})
                        meta["model"] = msg.get("model", meta["model"])
                        usage = msg.get("usage", {})
                        meta["input_tokens"] = usage.get("input_tokens", 0)
                    elif etype == "message_delta":
                        usage = event.get("usage", {})
                        meta["output_tokens"] = usage.get("output_tokens", 0)
                except (json.JSONDecodeError, KeyError):
                    continue
        return Response(content=resp.content, status_code=resp.status_code,
                       headers=dict(resp.headers)), meta

    resp = await http_client.post(url, json=body, headers=forward_headers, timeout=120)
    resp_data = resp.json() if resp.status_code == 200 else {}

    usage = resp_data.get("usage", {})
    meta = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "model": resp_data.get("model", body.get("model", "unknown")),
    }

    return Response(content=resp.content, status_code=resp.status_code,
                   media_type="application/json"), meta


async def proxy_openai(request: Request, body: dict) -> tuple[Response, dict]:
    """Proxy request to OpenAI API."""
    headers = dict(request.headers)
    forward_headers = {"content-type": "application/json"}
    if "authorization" in headers:
        forward_headers["authorization"] = headers["authorization"]

    path = request.url.path.replace("/openai", "", 1)
    url = f"{OPENAI_BASE}{path}"

    if body.get("stream"):
        resp = await http_client.post(url, json=body, headers=forward_headers, timeout=120)
        return Response(content=resp.content, status_code=resp.status_code,
                       headers=dict(resp.headers)), {}

    resp = await http_client.post(url, json=body, headers=forward_headers, timeout=120)
    resp_data = resp.json() if resp.status_code == 200 else {}

    usage = resp_data.get("usage", {})
    meta = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "model": resp_data.get("model", body.get("model", "unknown")),
    }

    return Response(content=resp.content, status_code=resp.status_code,
                   media_type="application/json"), meta


async def proxy_ollama(request: Request, body: dict) -> tuple[Response, dict]:
    """Proxy request to local Ollama."""
    path = request.url.path.replace("/ollama", "", 1)
    url = f"{OLLAMA_BASE}{path}"

    # Pass through GET requests (e.g. /api/tags) without modification
    if request.method == "GET":
        resp = await http_client.get(url, timeout=30)
        return Response(content=resp.content, status_code=resp.status_code,
                       media_type="application/json"), {}

    # Disable streaming for token counting
    body_copy = body.copy()
    is_stream = body_copy.pop("stream", False)

    if is_stream:
        resp = await http_client.post(url, json=body, timeout=300)
        return Response(content=resp.content, status_code=resp.status_code,
                       headers=dict(resp.headers)), {}

    body_copy["stream"] = False
    resp = await http_client.post(url, json=body_copy, timeout=300)
    resp_data = resp.json() if resp.status_code == 200 else {}

    # Ollama uses different response formats
    meta = {
        "input_tokens": resp_data.get("prompt_eval_count", 0),
        "output_tokens": resp_data.get("eval_count", 0),
        "model": resp_data.get("model", body.get("model", "unknown")),
    }

    return Response(content=resp.content, status_code=resp.status_code,
                   media_type="application/json"), meta


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_pool, http_client
    redis_pool = redis.from_url(REDIS_URL, decode_responses=True)
    http_client = httpx.AsyncClient(follow_redirects=True)
    init_audit_db()
    logger.info(f"LLM Proxy started — Anthropic: {ANTHROPIC_BASE}, OpenAI: {OPENAI_BASE}, Ollama: {OLLAMA_BASE}")
    yield
    await http_client.aclose()
    await redis_pool.aclose()


app = FastAPI(title="PAI LLM Gateway", lifespan=lifespan)


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    return {"status": "ok", "providers": ["anthropic", "openai", "ollama"]}


@app.api_route("/anthropic/{path:path}", methods=["GET", "POST"])
async def handle_anthropic(request: Request, path: str):
    return await _handle_request(request, "anthropic", proxy_anthropic)


@app.api_route("/openai/{path:path}", methods=["GET", "POST"])
async def handle_openai(request: Request, path: str):
    return await _handle_request(request, "openai", proxy_openai)


@app.api_route("/ollama/{path:path}", methods=["GET", "POST"])
async def handle_ollama(request: Request, path: str):
    return await _handle_request(request, "ollama", proxy_ollama)


async def _handle_request(request: Request, provider: str, proxy_fn):
    """Common request handler with caching, metrics, and audit."""
    start = time.time()

    # Parse body
    raw_body = await request.body()
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body = {}

    model = body.get("model", "unknown")
    is_stream = body.get("stream", False)

    # Skip caching for streaming requests
    if not is_stream and request.method == "POST":
        # Check exact cache
        req_hash = compute_request_hash(provider, body)
        cached = await get_cached_response(req_hash)
        if cached:
            latency_ms = int((time.time() - start) * 1000)
            CACHE_HITS.labels(provider=provider, type="exact").inc()
            REQUEST_COUNT.labels(provider=provider, model=model, cache_hit="exact").inc()
            log_audit(provider, model, 0, 0, 0, latency_ms, "exact", req_hash,
                     raw_body.decode()[:2000], json.dumps(cached)[:2000], 200)
            return JSONResponse(content=cached)

        # Check semantic cache
        semantic_cached = await check_semantic_cache(provider, body)
        if semantic_cached:
            latency_ms = int((time.time() - start) * 1000)
            CACHE_HITS.labels(provider=provider, type="semantic").inc()
            REQUEST_COUNT.labels(provider=provider, model=model, cache_hit="semantic").inc()
            log_audit(provider, model, 0, 0, 0, latency_ms, "semantic", req_hash,
                     raw_body.decode()[:2000], json.dumps(semantic_cached)[:2000], 200)
            return JSONResponse(content=semantic_cached)

    # Forward to provider
    response, meta = await proxy_fn(request, body)
    latency_ms = int((time.time() - start) * 1000)

    input_tokens = meta.get("input_tokens", 0)
    output_tokens = meta.get("output_tokens", 0)
    actual_model = meta.get("model", model)

    # Calculate cost
    pricing = get_pricing(actual_model)
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    # Update metrics
    REQUEST_COUNT.labels(provider=provider, model=actual_model, cache_hit="none").inc()
    TOKEN_INPUT.labels(provider=provider, model=actual_model).inc(input_tokens)
    TOKEN_OUTPUT.labels(provider=provider, model=actual_model).inc(output_tokens)
    COST_USD.labels(provider=provider, model=actual_model).inc(cost)
    LATENCY.labels(provider=provider, model=actual_model).observe(latency_ms / 1000)

    # Cache successful non-streaming responses
    if not is_stream and response.status_code == 200 and meta:
        try:
            resp_data = json.loads(response.body)
            req_hash = compute_request_hash(provider, body)
            await set_cached_response(req_hash, resp_data)
            await store_semantic_cache(provider, body, resp_data)
        except Exception:
            pass

    # Audit log
    resp_body = response.body.decode()[:2000] if hasattr(response, 'body') else ""
    log_audit(provider, actual_model, input_tokens, output_tokens, cost,
             latency_ms, "none", compute_request_hash(provider, body),
             raw_body.decode()[:2000], resp_body, response.status_code)

    logger.info(f"{provider}/{actual_model} | {input_tokens}in/{output_tokens}out | ${cost:.4f} | {latency_ms}ms")

    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT)
