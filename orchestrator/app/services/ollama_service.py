import json
import logging
import re

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.ollama")

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# Fallback: strip everything before </think> if opening tag is missing
_THINK_FALLBACK_RE = re.compile(r"^.*?</think>\s*", re.DOTALL)


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
                "options": {"temperature": 0.7, "num_ctx": 8192},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("response", "")
        # Strip <think>...</think> blocks from reasoning models (e.g. qwen3)
        cleaned = _THINK_RE.sub("", raw)
        # Fallback: if </think> present without opening tag, strip everything before it
        if "</think>" in cleaned:
            cleaned = _THINK_FALLBACK_RE.sub("", cleaned)
        return cleaned.strip()
    finally:
        if own_client:
            await client.aclose()


def select_model(task_input: str) -> str:
    """
    Select a model based on task complexity.

    Routing rules:
      - Simple/short queries → qwen3:4b (fast, lightweight)
      - Standard + complex tasks → llama3.1:8b (balanced default)
    """
    lower = task_input.lower()
    word_count = len(lower.split())

    # Simple signals → light model
    if word_count < 20 and lower.rstrip().endswith("?"):
        return "qwen3:4b"

    simple_keywords = [
        "what is", "who is", "define", "explain briefly", "quick", "short",
        "how do i", "tell me", "help me",
    ]
    if word_count < 40 and any(kw in lower for kw in simple_keywords):
        return "qwen3:4b"

    # Everything else → balanced model
    return settings.ollama_default_model
