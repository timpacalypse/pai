"""Claude service — routes complex tasks to Claude via CLI or API.

Supports two backends:
  1. CLI mode (default): shells out to `claude -p` — uses your Claude Pro/Max
     subscription, no API billing. Set CLAUDE_CLI_ENABLED=true.
  2. API mode: direct HTTP to Anthropic API. Set ANTHROPIC_API_KEY.

Both share: Redis response caching, daily cost tracking, context compression,
tiered model routing, and graceful fallback to local Ollama.
"""

import asyncio
import hashlib
import json
import logging
import time
from datetime import date
from typing import AsyncGenerator

import httpx
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger("pai.claude")

# ── Model tiers ───────────────────────────────────────────────────────────────
MODELS = {
    "haiku": "claude-haiku-4-20250414",
    "sonnet": "claude-sonnet-4-20250514",
    "opus": "claude-opus-4-20250514",
}

# CLI uses short model names
CLI_MODELS = {
    "haiku": "haiku",
    "sonnet": "sonnet",
    "opus": "opus",
}

# Cost per million tokens (USD)
COSTS = {
    "claude-haiku-4-20250414": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
}

_CACHE_TTL = 86400  # 24h for Claude responses (expensive to regenerate)
_redis_client: aioredis.Redis | None = None


# ── Redis helpers ─────────────────────────────────────────────────────────────

async def _get_redis() -> aioredis.Redis | None:
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        except Exception:
            return None
    return _redis_client


async def _cache_get(key: str) -> str | None:
    try:
        r = await _get_redis()
        if r:
            return await r.get(key)
    except Exception:
        pass
    return None


async def _cache_set(key: str, value: str, ttl: int = _CACHE_TTL) -> None:
    try:
        r = await _get_redis()
        if r:
            await r.set(key, value, ex=ttl)
    except Exception:
        pass


def _cache_key(model: str, system_prompt: str, prompt: str) -> str:
    raw = f"claude|{model}|{system_prompt}|{prompt}"
    return f"pai:claude_cache:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


# ── Budget tracking ───────────────────────────────────────────────────────────

async def _get_daily_spend() -> float:
    """Get today's Claude API spend in USD."""
    r = await _get_redis()
    if not r:
        return 0.0
    key = f"pai:claude:spend:{date.today().isoformat()}"
    val = await r.get(key)
    return float(val) if val else 0.0


async def _record_usage(model: str, input_tokens: int, output_tokens: int) -> float:
    """Record token usage and return updated daily spend."""
    costs = COSTS.get(model, {"input": 3.0, "output": 15.0})
    cost = (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000

    r = await _get_redis()
    if r:
        key = f"pai:claude:spend:{date.today().isoformat()}"
        current = float(await r.get(key) or 0)
        new_total = current + cost
        await r.set(key, str(new_total), ex=86400)

        # Also track raw token counts
        tk = f"pai:claude:tokens:{date.today().isoformat()}"
        tokens_data = await r.get(tk)
        if tokens_data:
            tokens = json.loads(tokens_data)
        else:
            tokens = {"input": 0, "output": 0, "calls": 0}
        tokens["input"] += input_tokens
        tokens["output"] += output_tokens
        tokens["calls"] += 1
        await r.set(tk, json.dumps(tokens), ex=86400)

        return new_total
    return cost


async def get_usage_stats() -> dict:
    """Get today's Claude usage stats."""
    r = await _get_redis()
    if not r:
        return {"spend": 0, "tokens": {"input": 0, "output": 0, "calls": 0}, "budget": get_daily_budget()}

    spend = float(await r.get(f"pai:claude:spend:{date.today().isoformat()}") or 0)
    tokens_data = await r.get(f"pai:claude:tokens:{date.today().isoformat()}")
    tokens = json.loads(tokens_data) if tokens_data else {"input": 0, "output": 0, "calls": 0}

    return {
        "spend_usd": round(spend, 4),
        "budget_usd": get_daily_budget(),
        "budget_remaining_usd": round(get_daily_budget() - spend, 4),
        "tokens": tokens,
    }


def get_daily_budget() -> float:
    return getattr(settings, "claude_daily_budget_usd", 1.0)


async def is_budget_available() -> bool:
    """Check if there's budget remaining for today."""
    spend = await _get_daily_spend()
    return spend < get_daily_budget()


# ── Core generation ───────────────────────────────────────────────────────────

def _is_cli_mode() -> bool:
    return getattr(settings, "claude_cli_enabled", False)


def _is_api_mode() -> bool:
    return bool(getattr(settings, "anthropic_api_key", ""))


def _is_enabled() -> bool:
    return _is_cli_mode() or _is_api_mode()


async def generate(
    prompt: str,
    system_prompt: str = "",
    model_tier: str = "sonnet",
    max_tokens: int = 1024,
    temperature: float = 0.7,
    http_client: httpx.AsyncClient | None = None,
    use_cache: bool = True,
    cache_system_prompt: bool = True,
) -> str:
    """Generate a response from Claude (CLI or API).

    Returns response text, or falls back to local LLM on failure.
    """
    if not _is_enabled():
        logger.debug("claude_not_configured — falling back to local")
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt, http_client=http_client)

    # Budget check (skip for CLI mode — subscription is flat-rate)
    if _is_api_mode() and not _is_cli_mode():
        if not await is_budget_available():
            logger.info("claude_budget_exhausted — falling back to local")
            from app.services.ollama_service import generate as local_generate
            return await local_generate(prompt, system_prompt, http_client=http_client)

    model = MODELS.get(model_tier, MODELS["sonnet"])

    # Check response cache
    if use_cache:
        ck = _cache_key(model, system_prompt, prompt)
        cached = await _cache_get(ck)
        if cached is not None:
            logger.debug("claude_cache_hit", extra={"model": model_tier})
            return cached

    # Route to appropriate backend
    if _is_cli_mode():
        cli_model = CLI_MODELS.get(model_tier, "sonnet")
        result = await _generate_cli(prompt, system_prompt, cli_model, max_tokens)
    else:
        result = await _generate_api(
            prompt, system_prompt, model, model_tier,
            max_tokens, temperature, http_client, cache_system_prompt,
        )

    # Cache the result
    if use_cache and result:
        await _cache_set(_cache_key(model, system_prompt, prompt), result)

    return result


# ── CLI backend (Claude Code — uses Pro/Max subscription) ─────────────────────

async def _generate_cli(
    prompt: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
) -> str:
    """Call `claude -p` subprocess. Uses Pro/Max subscription, no API billing."""
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--model", model,
        "--max-turns", "1",
    ]
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=120,
        )

        if proc.returncode != 0:
            err = stderr.decode()[:300]
            logger.error("claude_cli_failed", extra={"rc": proc.returncode, "stderr": err})
            from app.services.ollama_service import generate as local_generate
            return await local_generate(prompt, system_prompt)

        raw = stdout.decode()

        # claude -p --output-format json returns a JSON object with "result", "cost_usd", etc.
        try:
            data = json.loads(raw)
            result = data.get("result", raw).strip()
            cost = data.get("cost_usd", 0)
            duration = data.get("duration_ms", 0)
            # Track usage for stats (even though it's subscription-based)
            await _record_cli_usage(model, cost, duration)
            logger.info("claude_cli_response", extra={
                "model": model,
                "cost_usd": cost,
                "duration_ms": duration,
            })
        except json.JSONDecodeError:
            # Fallback: plain text output
            result = raw.strip()
            logger.info("claude_cli_response_text", extra={"model": model, "len": len(result)})

        return result

    except asyncio.TimeoutError:
        logger.error("claude_cli_timeout")
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt)
    except FileNotFoundError:
        logger.error("claude_cli_not_found — is claude CLI installed?")
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt)
    except Exception as e:
        logger.error("claude_cli_error", extra={"error": str(e)})
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt)


async def _record_cli_usage(model: str, cost_usd: float, duration_ms: int) -> None:
    """Track CLI usage in Redis for stats endpoint."""
    r = await _get_redis()
    if not r:
        return
    today = date.today().isoformat()
    # Accumulate spend
    key = f"pai:claude:spend:{today}"
    current = float(await r.get(key) or 0)
    await r.set(key, str(current + cost_usd), ex=86400)
    # Accumulate call count
    tk = f"pai:claude:tokens:{today}"
    tokens_data = await r.get(tk)
    tokens = json.loads(tokens_data) if tokens_data else {"input": 0, "output": 0, "calls": 0}
    tokens["calls"] += 1
    tokens["duration_ms"] = tokens.get("duration_ms", 0) + duration_ms
    await r.set(tk, json.dumps(tokens), ex=86400)


# ── API backend (direct Anthropic HTTP — uses API key billing) ────────────────

async def _generate_api(
    prompt: str,
    system_prompt: str,
    model: str,
    model_tier: str,
    max_tokens: int,
    temperature: float,
    http_client: httpx.AsyncClient | None,
    cache_system_prompt: bool,
) -> str:
    """Call Anthropic Messages API directly. Requires ANTHROPIC_API_KEY."""
    api_key = settings.anthropic_api_key

    # Build request
    messages = [{"role": "user", "content": prompt}]

    system_block = None
    if system_prompt:
        if cache_system_prompt:
            system_block = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_block = system_prompt

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system_block:
        payload["system"] = system_block

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if cache_system_prompt:
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"

    client = http_client or httpx.AsyncClient(timeout=60.0)
    own_client = http_client is None

    try:
        resp = await client.post(
            f"{settings.anthropic_base_url}/v1/messages",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content", [])
        result = "".join(
            block.get("text", "") for block in content_blocks if block.get("type") == "text"
        ).strip()

        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        total_spend = await _record_usage(model, input_tokens, output_tokens)

        logger.info("claude_api_response", extra={
            "model": model_tier,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "daily_spend_usd": round(total_spend, 4),
        })

        return result

    except httpx.HTTPStatusError as e:
        logger.error("claude_api_error", extra={
            "status": e.response.status_code,
            "body": e.response.text[:200],
        })
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt, http_client=http_client)
    except Exception as e:
        logger.error("claude_api_request_failed", extra={"error": str(e)})
        from app.services.ollama_service import generate as local_generate
        return await local_generate(prompt, system_prompt, http_client=http_client)
    finally:
        if own_client:
            await client.aclose()


async def generate_stream(
    prompt: str,
    system_prompt: str = "",
    model_tier: str = "sonnet",
    max_tokens: int = 1024,
    temperature: float = 0.7,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncGenerator[str, None]:
    """Stream tokens from Claude. Falls back to local on failure."""
    if not _is_enabled():
        from app.services.ollama_service import generate_stream as local_stream
        async for token in local_stream(prompt, system_prompt, http_client=http_client):
            yield token
        return

    if _is_cli_mode():
        # CLI mode: stream-json output
        cli_model = CLI_MODELS.get(model_tier, "sonnet")
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json",
            "--model", cli_model,
            "--max-turns", "1",
        ]
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            proc.stdin.write(prompt.encode())
            proc.stdin.write_eof()
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                    if event.get("type") == "assistant" and "message" in event:
                        yield event["message"]
                    elif event.get("type") == "result":
                        break
                except json.JSONDecodeError:
                    text = line.decode().strip()
                    if text:
                        yield text
            await proc.wait()
        except Exception as e:
            logger.error("claude_cli_stream_failed", extra={"error": str(e)})
            from app.services.ollama_service import generate_stream as local_stream
            async for token in local_stream(prompt, system_prompt, http_client=http_client):
                yield token
        return

    # API mode
    if not await is_budget_available():
        from app.services.ollama_service import generate_stream as local_stream
        async for token in local_stream(prompt, system_prompt, http_client=http_client):
            yield token
        return

    model = MODELS.get(model_tier, MODELS["sonnet"])

    # API mode streaming
    messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
        "stream": True,
    }
    if system_prompt:
        payload["system"] = system_prompt

    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    client = http_client or httpx.AsyncClient(timeout=60.0)
    own_client = http_client is None

    try:
        async with client.stream(
            "POST",
            f"{settings.anthropic_base_url}/v1/messages",
            json=payload,
            headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        text = delta.get("text", "")
                        if text:
                            yield text
                    elif event.get("type") == "message_delta":
                        # Record usage from final event
                        usage = event.get("usage", {})
                        if usage:
                            await _record_usage(
                                model,
                                usage.get("input_tokens", 0),
                                usage.get("output_tokens", 0),
                            )
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("claude_stream_failed", extra={"error": str(e)})
        # Fallback to local
        from app.services.ollama_service import generate_stream as local_stream
        async for token in local_stream(prompt, system_prompt, http_client=http_client):
            yield token
    finally:
        if own_client:
            await client.aclose()


# ── Model router ──────────────────────────────────────────────────────────────

# Skills/tasks that benefit from Claude's reasoning
_CLOUD_PREFERRED_SKILLS = {
    "linkedin",         # content drafting
    "weekly_digest",    # multi-article synthesis
    "article_curation", # analysis + scoring
    "briefing",         # complex aggregation
}

# Complexity scoring thresholds
_COMPLEXITY_SONNET_THRESHOLD = 7   # score >= 7 → route to sonnet
_COMPLEXITY_HAIKU_THRESHOLD = 5    # score >= 5 → route to haiku

_COMPLEXITY_PROMPT = 'Score complexity 1-10:\n1-3=simple  4-6=moderate  7-10=complex\n\n"{message}"'


async def _score_complexity(message: str, http_client: httpx.AsyncClient | None = None) -> int:
    """Ask the local LLM to score query complexity (1-10)."""
    client = http_client or httpx.AsyncClient(timeout=30.0)
    own_client = http_client is None
    try:
        resp = await client.post(
            f"{settings.ollama_url}/api/chat",
            json={
                "model": "qwen3.5:9b",
                "messages": [
                    {"role": "system", "content": "Output ONLY a single integer 1-10. No words."},
                    {"role": "user", "content": _COMPLEXITY_PROMPT.format(message=message[:1000])},
                ],
                "stream": False,
                "think": False,
                "options": {"temperature": 0.0, "num_predict": 4},
            },
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "").strip()
        # Extract first integer from response
        for token in raw.split():
            token = token.strip(".,;:!?()[]")
            if token.isdigit():
                score = int(token)
                if 1 <= score <= 10:
                    logger.info(f"complexity_score: {score} for: {message[:80]}")
                    return score
        logger.warning("complexity_score_parse_fail", extra={"raw": raw})
        return 3  # default to local on parse failure
    except Exception as e:
        logger.warning(f"complexity_score_error: {type(e).__name__}: {e}")
        return 3  # default to local on error
    finally:
        if own_client:
            await client.aclose()


async def should_use_claude(skill_id: str | None, message: str, action: str | None = None,
                            http_client: httpx.AsyncClient | None = None) -> tuple[str | None, int]:
    """Decide whether to route to Claude using LLM-based complexity scoring.

    Returns (tier, score) where tier is "sonnet", "haiku", or None,
    and score is the complexity rating 1-10.

    Logic:
    - If neither CLI nor API configured → (None, 0)
    - If skill is in cloud-preferred list or has model_preference="cloud" → ("sonnet", 10)
    - If LLM complexity score >= 7 → ("sonnet", score)
    - If LLM complexity score >= 5 → ("haiku", score)
    - Otherwise → (None, score)
    """
    if not _is_enabled():
        return None, 0

    # Skill-based routing (check registry model_preference first, then hardcoded set)
    if skill_id:
        if skill_id in _CLOUD_PREFERRED_SKILLS:
            return "sonnet", 10
        try:
            from app.services.skill_registry import get_skill
            skill = get_skill(skill_id)
            if skill and getattr(skill, "model_preference", "local") == "cloud":
                return "sonnet", 10
        except Exception:
            pass

    # LLM-based complexity scoring
    score = await _score_complexity(message, http_client=http_client)

    if score >= _COMPLEXITY_SONNET_THRESHOLD:
        logger.info("routing_to_claude", extra={"tier": "sonnet", "score": score})
        return "sonnet", score
    if score >= _COMPLEXITY_HAIKU_THRESHOLD:
        logger.info("routing_to_claude", extra={"tier": "haiku", "score": score})
        return "haiku", score

    return None, score


# ── Context compression ───────────────────────────────────────────────────────

async def compress_context(
    chunks: list[str],
    max_tokens: int = 1500,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Use local LLM to compress RAG chunks before sending to Claude.

    This saves Claude tokens by sending a distilled summary instead of raw documents.
    """
    if not chunks:
        return ""

    combined = "\n---\n".join(chunks)

    # If already short enough, skip compression
    # Rough estimate: 1 token ≈ 4 chars
    if len(combined) < max_tokens * 4:
        return combined

    from app.services.ollama_service import generate as local_generate
    compressed = await local_generate(
        prompt=(
            f"Compress the following context into a dense summary preserving all "
            f"key facts, numbers, names, and dates. Remove filler and redundancy. "
            f"Keep it under {max_tokens // 4} words.\n\n{combined}"
        ),
        system_prompt="You are a compression assistant. Output only the compressed summary, nothing else.",
        model="qwen3:4b",
        http_client=http_client,
    )
    return compressed
