"""Pomodoro command parsing + shared dashboard timer state.

State is persisted in Redis so voice commands, chat, and dashboard can share one timer.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from app.core.config import settings

POMODORO_REDIS_KEY = "pai:dashboard:pomodoro"
DEFAULT_MINUTES = 25


async def _get_redis(redis_client=None):
    if redis_client is not None:
        return redis_client, False
    import redis.asyncio as aioredis
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    return r, True


def _default_state() -> dict[str, Any]:
    duration = DEFAULT_MINUTES * 60
    return {
        "visible": False,
        "running": False,
        "duration_seconds": duration,
        "remaining_seconds": duration,
        "ends_at": None,
        "updated_at": int(time.time()),
    }


def _normalized(state: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    out = {**_default_state(), **(state or {})}

    try:
        out["duration_seconds"] = max(60, int(out.get("duration_seconds", DEFAULT_MINUTES * 60)))
    except Exception:
        out["duration_seconds"] = DEFAULT_MINUTES * 60

    try:
        out["remaining_seconds"] = max(0, int(out.get("remaining_seconds", out["duration_seconds"])))
    except Exception:
        out["remaining_seconds"] = out["duration_seconds"]

    out["visible"] = bool(out.get("visible", False))
    out["running"] = bool(out.get("running", False))

    ends_at = out.get("ends_at")
    if out["running"] and ends_at is not None:
        try:
            remaining = max(0, int(ends_at) - now)
        except Exception:
            remaining = 0
        out["remaining_seconds"] = remaining
        if remaining <= 0:
            out["running"] = False
            out["ends_at"] = None

    out["updated_at"] = now
    return out


async def get_pomodoro_state(redis_client=None) -> dict[str, Any]:
    redis, should_close = await _get_redis(redis_client)
    try:
        raw = await redis.get(POMODORO_REDIS_KEY)
        if not raw:
            state = _default_state()
            await redis.set(POMODORO_REDIS_KEY, json.dumps(state))
            return state
        state = _normalized(json.loads(raw))
        await redis.set(POMODORO_REDIS_KEY, json.dumps(state))
        return state
    finally:
        if should_close:
            await redis.aclose()


async def _save_state(state: dict[str, Any], redis_client=None) -> dict[str, Any]:
    redis, should_close = await _get_redis(redis_client)
    try:
        normalized = _normalized(state)
        await redis.set(POMODORO_REDIS_KEY, json.dumps(normalized))
        return normalized
    finally:
        if should_close:
            await redis.aclose()


async def _set_minutes(minutes: int, redis_client=None) -> dict[str, Any]:
    state = await get_pomodoro_state(redis_client)
    sec = max(1, min(180, int(minutes))) * 60
    state["duration_seconds"] = sec
    state["remaining_seconds"] = sec
    state["running"] = False
    state["ends_at"] = None
    state["visible"] = True
    return await _save_state(state, redis_client)


async def _start(minutes: int | None, redis_client=None) -> dict[str, Any]:
    state = await get_pomodoro_state(redis_client)
    if minutes is not None:
        state = await _set_minutes(minutes, redis_client)

    now = int(time.time())
    remaining = max(1, int(state.get("remaining_seconds") or 0))
    if remaining <= 0:
        remaining = int(state.get("duration_seconds") or DEFAULT_MINUTES * 60)
    state["visible"] = True
    state["running"] = True
    state["remaining_seconds"] = remaining
    state["ends_at"] = now + remaining
    return await _save_state(state, redis_client)


async def _pause(redis_client=None) -> dict[str, Any]:
    state = await get_pomodoro_state(redis_client)
    if state.get("running") and state.get("ends_at"):
        now = int(time.time())
        state["remaining_seconds"] = max(0, int(state["ends_at"]) - now)
    state["running"] = False
    state["ends_at"] = None
    state["visible"] = True
    return await _save_state(state, redis_client)


async def _reset(redis_client=None) -> dict[str, Any]:
    state = await get_pomodoro_state(redis_client)
    state["running"] = False
    state["ends_at"] = None
    state["remaining_seconds"] = int(state.get("duration_seconds") or DEFAULT_MINUTES * 60)
    state["visible"] = True
    return await _save_state(state, redis_client)


async def _hide(redis_client=None) -> dict[str, Any]:
    state = await _pause(redis_client)
    state["visible"] = False
    return await _save_state(state, redis_client)


async def _show(redis_client=None) -> dict[str, Any]:
    state = await get_pomodoro_state(redis_client)
    state["visible"] = True
    return await _save_state(state, redis_client)


def _status_text(state: dict[str, Any]) -> str:
    total = max(0, int(state.get("remaining_seconds") or 0))
    mm = total // 60
    ss = total % 60
    timer = f"{mm:02d}:{ss:02d}"
    if state.get("running"):
        return f"Pomodoro running: {timer} remaining."
    if total == 0:
        return "Pomodoro complete. Time for a break."
    return f"Pomodoro ready: {timer}."


async def handle_pomodoro_command(message: str, redis_client=None) -> dict[str, Any] | None:
    """Return command result dict when message is a pomodoro command, else None."""
    lower = (message or "").strip().lower()
    if not lower:
        return None

    if not re.search(r"\b(pomodoro|focus\s*timer|focus\s*mode|timer)\b", lower):
        return None

    mins_match = re.search(r"\b(\d{1,3})\s*(?:min|mins|minute|minutes)\b", lower)
    num_match = re.search(r"\b(?:pomodoro|focus\s*mode)\s*(\d{1,3})\b", lower)
    mins = None
    if mins_match:
        mins = int(mins_match.group(1))
    elif num_match:
        mins = int(num_match.group(1))

    if re.search(r"\b(start|begin|activate|run)\b", lower):
        state = await _start(mins, redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    if re.search(r"\b(resume|continue)\b", lower):
        state = await _start(None, redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    if re.search(r"\b(pause|hold)\b", lower):
        state = await _pause(redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    if re.search(r"\b(reset|restart)\b", lower):
        state = await _reset(redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    if re.search(r"\b(hide|dismiss|close)\b", lower):
        state = await _hide(redis_client)
        return {"handled": True, "content": "Pomodoro hidden.", "state": state}

    if re.search(r"\b(show|display|open)\b", lower):
        state = await _show(redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    if mins is not None and re.search(r"\b(set|configure|change)\b", lower):
        state = await _set_minutes(mins, redis_client)
        return {"handled": True, "content": f"Pomodoro set to {mins} minutes.", "state": state}

    if re.search(r"\b(status|remaining|left|time)\b", lower):
        state = await get_pomodoro_state(redis_client)
        state["visible"] = True
        state = await _save_state(state, redis_client)
        return {"handled": True, "content": _status_text(state), "state": state}

    # Generic timer mention: return current status without forcing state changes.
    state = await get_pomodoro_state(redis_client)
    return {"handled": True, "content": _status_text(state), "state": state}
