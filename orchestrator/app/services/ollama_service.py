import json
import logging
import re
from typing import AsyncGenerator

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.ollama")

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
# Fallback: strip everything before </think> if opening tag is missing
_THINK_FALLBACK_RE = re.compile(r"^.*?</think>\s*", re.DOTALL)


def _clean_response(raw: str) -> str:
    """Strip <think>...</think> blocks from reasoning models (e.g. qwen3)."""
    cleaned = _THINK_RE.sub("", raw)
    if "</think>" in cleaned:
        cleaned = _THINK_FALLBACK_RE.sub("", cleaned)
    return cleaned.strip()


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
        return _clean_response(raw)
    finally:
        if own_client:
            await client.aclose()


async def generate_stream(
    prompt: str,
    system_prompt: str,
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncGenerator[str, None]:
    """Stream tokens from Ollama generate endpoint, yielding cleaned chunks."""
    model = model or settings.ollama_default_model
    client = http_client or httpx.AsyncClient(timeout=120.0)
    own_client = http_client is None

    in_think = False

    try:
        async with client.stream(
            "POST",
            f"{settings.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "system": system_prompt,
                "stream": True,
                "think": False,
                "options": {"temperature": 0.7, "num_ctx": 8192},
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                token = data.get("response", "")
                if not token:
                    if data.get("done"):
                        break
                    continue

                # Filter <think>...</think> blocks in real-time
                if "<think>" in token:
                    in_think = True
                    # Yield anything before the tag
                    before = token.split("<think>")[0]
                    if before:
                        yield before
                    continue
                if in_think:
                    if "</think>" in token:
                        in_think = False
                        after = token.split("</think>")[-1]
                        if after:
                            yield after
                    continue

                yield token
    finally:
        if own_client:
            await client.aclose()


async def generate_tool_call(
    prompt: str,
    system_prompt: str,
    tools: list[dict],
    model: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict | None:
    """Call Ollama with tool definitions and return the structured tool call result.

    Uses Ollama's native /api/chat with tools support.
    Returns the tool call arguments dict, or None if no tool was called.
    """
    model = model or settings.ollama_default_model
    client = http_client or httpx.AsyncClient(timeout=120.0)
    own_client = http_client is None

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        resp = await client.post(
            f"{settings.ollama_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 2048},
                "think": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract tool calls from response
        message = data.get("message", {})
        tool_calls = message.get("tool_calls", [])

        if tool_calls:
            # Return first tool call's arguments
            call = tool_calls[0]
            return call.get("function", {}).get("arguments", {})

        # Fallback: if the model responded with text instead of tool call,
        # try to parse it as JSON (some models do this)
        content = message.get("content", "")
        if content:
            cleaned = _clean_response(content)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                start = cleaned.find("{")
                end = cleaned.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        return json.loads(cleaned[start:end])
                    except json.JSONDecodeError:
                        pass

        return None
    finally:
        if own_client:
            await client.aclose()


def select_model(task_input: str) -> str:
    """
    Select a model based on task complexity.

    Routing rules:
      - Simple/short queries → qwen3:4b (fast, lightweight)
      - Standard + complex tasks → qwen3:8b (balanced default)
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
