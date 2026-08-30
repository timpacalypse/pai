import hashlib
import json
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.embedding")

EMBED_MODEL = "qwen3-embedding:0.6b"
EMBED_MODEL_FALLBACK = "nomic-embed-text"
_EMBED_CACHE_TTL = 300  # 5 minutes
_redis_client = None


async def _redis():
    """Get or create a shared Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        import aioredis
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def get_embedding(
    text: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[float]:
    """Generate an embedding vector using Ollama's qwen3-embedding model, with Redis cache."""
    cache_key = f"pai:embed:{hashlib.sha256(text.encode()).hexdigest()[:24]}"

    # Check cache
    try:
        redis = await _redis()
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    client = http_client or httpx.AsyncClient(timeout=60.0)
    own_client = http_client is None

    try:
        for model in (EMBED_MODEL, EMBED_MODEL_FALLBACK):
            try:
                resp = await client.post(
                    f"{settings.ollama_url}/api/embed",
                    json={"model": model, "input": text},
                )
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings", [])
                if embeddings:
                    embedding = embeddings[0]
                    try:
                        redis = await _redis()
                        await redis.set(cache_key, json.dumps(embedding), ex=_EMBED_CACHE_TTL)
                    except Exception:
                        pass
                    return embedding
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404 and model != EMBED_MODEL_FALLBACK:
                    logger.warning("Embed model %s unavailable, trying fallback", model)
                    continue
                raise
        return []
    finally:
        if own_client:
            await client.aclose()
