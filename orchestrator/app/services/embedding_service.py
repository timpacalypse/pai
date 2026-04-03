import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.embedding")

EMBED_MODEL = "nomic-embed-text"


async def get_embedding(
    text: str,
    http_client: httpx.AsyncClient | None = None,
) -> list[float]:
    """Generate an embedding vector using Ollama's nomic-embed-text model."""
    client = http_client or httpx.AsyncClient(timeout=60.0)
    own_client = http_client is None

    try:
        resp = await client.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": EMBED_MODEL, "input": text},
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama returns {"embeddings": [[...]]} for /api/embed
        embeddings = data.get("embeddings", [])
        if embeddings:
            return embeddings[0]
        return []
    finally:
        if own_client:
            await client.aclose()
