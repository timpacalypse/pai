import json
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.ollama")


async def generate(
    prompt: str,
    system_prompt: str,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Call Ollama generate endpoint and return the full response text."""
    model = model or settings.ollama_default_model
    client = http_client or httpx.AsyncClient(timeout=120.0)
    own_client = http_client is None

    try:
        resp = await client.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "system": system_prompt,
                "stream": False,
                "options": {"temperature": 0.7},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")
    finally:
        if own_client:
            await client.aclose()


def select_model(task_input: str) -> str:
    """Select a model based on task characteristics. Placeholder for future routing logic."""
    return settings.ollama_default_model
