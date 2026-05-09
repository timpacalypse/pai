"""Whoop API sync service.

Pulls workouts, recovery, and sleep data via official OAuth2 API.
Docs: https://developer.whoop.com/api
"""

import json as _json_mod
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session

logger = logging.getLogger("pai.fitness.whoop")

API_BASE = "https://api.prod.whoop.com"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"


async def store_whoop_tokens(tokens: dict):
    """Store OAuth tokens in fitness_sync_state for the whoop platform."""
    token_data = _json_mod.dumps({
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_in": tokens.get("expires_in", 3600),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    })
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_sync_state (platform, sync_cursor, status, updated_at)
                VALUES ('whoop', :tokens, 'authenticated', NOW())
                ON CONFLICT (platform) DO UPDATE SET
                    sync_cursor = :tokens, status = 'authenticated', updated_at = NOW()
            """),
            {"tokens": token_data},
        )
        await session.commit()
    logger.info("whoop_tokens_stored")


async def _load_tokens() -> dict | None:
    """Load stored OAuth tokens from DB."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT sync_cursor FROM fitness_sync_state WHERE platform = 'whoop'"),
        )
        row = result.scalar_one_or_none()
        if row:
            try:
                return _json_mod.loads(row)
            except Exception:
                pass
    return None


async def _refresh_access_token(refresh_token: str) -> dict | None:
    """Use refresh token to get new access + refresh tokens."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.whoop_client_id,
            "client_secret": settings.whoop_client_secret,
            "scope": "offline",
        })
    if resp.status_code != 200:
        logger.error("whoop_token_refresh_failed", extra={"status": resp.status_code, "body": resp.text})
        return None
    tokens = resp.json()
    await store_whoop_tokens(tokens)
    logger.info("whoop_tokens_refreshed")
    return tokens


async def _get_access_token() -> str | None:
    """Get a valid access token — from .env, DB, or refresh."""
    # 1. Check .env override
    if settings.whoop_access_token:
        return settings.whoop_access_token

    # 2. Load from DB
    stored = await _load_tokens()
    if not stored:
        return None

    access_token = stored.get("access_token")
    refresh_token = stored.get("refresh_token")

    # 3. Check if token is likely expired (stored_at + expires_in)
    try:
        stored_at = datetime.fromisoformat(stored.get("stored_at", ""))
        expires_in = stored.get("expires_in", 3600)
        if datetime.now(timezone.utc) > stored_at + timedelta(seconds=expires_in - 300):
            # Token expired or expiring soon — refresh
            if refresh_token:
                refreshed = await _refresh_access_token(refresh_token)
                if refreshed:
                    return refreshed.get("access_token")
            return None
    except Exception:
        pass

    return access_token


async def sync_whoop() -> dict:
    """Sync all Whoop data. Returns summary of records synced."""
    access_token = await _get_access_token()
    if not access_token:
        return {"status": "skipped", "reason": "no whoop_access_token configured"}

    summary = {"workouts": 0, "recovery": 0, "sleep": 0, "errors": []}

    async with httpx.AsyncClient(timeout=30.0) as client:
        headers = {"Authorization": f"Bearer {access_token}"}

        # Get last sync time
        last_sync = await _get_last_sync("whoop")
        start_dt = last_sync or (datetime.now(timezone.utc) - timedelta(days=30))
        params = {"start": start_dt.isoformat(), "limit": 25}

        try:
            summary["workouts"] = await _sync_workouts(client, headers, params)
        except Exception as e:
            logger.error("whoop_workout_sync_failed", extra={"error": str(e)})
            summary["errors"].append(f"workouts: {e}")

        try:
            summary["recovery"] = await _sync_recovery(client, headers, params)
        except Exception as e:
            logger.error("whoop_recovery_sync_failed", extra={"error": str(e)})
            summary["errors"].append(f"recovery: {e}")

        try:
            summary["sleep"] = await _sync_sleep(client, headers, params)
        except Exception as e:
            logger.error("whoop_sleep_sync_failed", extra={"error": str(e)})
            summary["errors"].append(f"sleep: {e}")

    await _update_sync_state("whoop", sum(v for k, v in summary.items() if isinstance(v, int)))
    logger.info("whoop_sync_complete", extra=summary)
    return summary


async def _sync_workouts(client: httpx.AsyncClient, headers: dict, params: dict) -> int:
    """Fetch and store Whoop workouts."""
    count = 0
    next_token = None

    while True:
        p = {**params}
        if next_token:
            p["nextToken"] = next_token

        resp = await client.get(f"{API_BASE}/v2/activity/workout", headers=headers, params=p)
        if resp.status_code == 401:
            # Try refresh once
            stored = await _load_tokens()
            rt = stored.get("refresh_token") if stored else None
            if rt:
                refreshed = await _refresh_access_token(rt)
                if refreshed:
                    headers["Authorization"] = f"Bearer {refreshed['access_token']}"
                    resp = await client.get(f"{API_BASE}/v2/activity/workout", headers=headers, params=p)
            if resp.status_code == 401:
                raise PermissionError("Whoop token expired — re-authenticate via /whoop/auth")
        resp.raise_for_status()
        data = resp.json()

        for w in data.get("records", []):
            score = w.get("score") or {}
            await _upsert_workout(
                platform="whoop",
                external_id=str(w.get("id", "")),
                workout_type="whoop_activity",
                sport_name=w.get("sport_name", ""),
                title=w.get("sport_name", ""),
                start_time=_parse_dt(w.get("start")),
                end_time=_parse_dt(w.get("end")),
                duration_seconds=_duration_secs(w.get("start"), w.get("end")),
                calories_kj=score.get("kilojoule", 0),
                distance_meters=score.get("distance_meter", 0),
                avg_heart_rate=score.get("average_heart_rate", 0),
                max_heart_rate=score.get("max_heart_rate", 0),
                strain=score.get("strain", 0),
                metrics={
                    "zone_durations": score.get("zone_durations"),
                    "percent_recorded": score.get("percent_recorded"),
                    "altitude_gain_meter": score.get("altitude_gain_meter"),
                },
            )
            count += 1

        next_token = data.get("next_token")
        if not next_token:
            break

    return count


async def _sync_recovery(client: httpx.AsyncClient, headers: dict, params: dict) -> int:
    """Fetch and store Whoop recovery data."""
    count = 0
    next_token = None

    while True:
        p = {**params}
        if next_token:
            p["nextToken"] = next_token

        resp = await client.get(f"{API_BASE}/v2/recovery", headers=headers, params=p)
        resp.raise_for_status()
        data = resp.json()

        for r in data.get("records", []):
            score = r.get("score") or {}
            await _upsert_recovery(
                platform="whoop",
                external_id=str(r.get("cycle_id", "")),
                record_date=r.get("created_at", "")[:10],
                recovery_score=score.get("recovery_score", 0),
                resting_heart_rate=score.get("resting_heart_rate", 0),
                hrv_rmssd=score.get("hrv_rmssd_milli", 0),
                spo2_percentage=score.get("spo2_percentage", 0),
                skin_temp_celsius=score.get("skin_temp_celsius", 0),
                metrics={"user_calibrating": score.get("user_calibrating")},
            )
            count += 1

        next_token = data.get("next_token")
        if not next_token:
            break

    return count


async def _sync_sleep(client: httpx.AsyncClient, headers: dict, params: dict) -> int:
    """Fetch and store Whoop sleep data."""
    count = 0
    next_token = None

    while True:
        p = {**params}
        if next_token:
            p["nextToken"] = next_token

        resp = await client.get(f"{API_BASE}/v2/activity/sleep", headers=headers, params=p)
        resp.raise_for_status()
        data = resp.json()

        for s in data.get("records", []):
            score = s.get("score") or {}
            await _upsert_sleep(
                platform="whoop",
                external_id=str(s.get("id", "")),
                start_time=_parse_dt(s.get("start")),
                end_time=_parse_dt(s.get("end")),
                total_duration_seconds=_duration_secs(s.get("start"), s.get("end")),
                sleep_performance=score.get("sleep_performance_percentage", 0),
                sleep_efficiency=score.get("sleep_efficiency_percentage", 0),
                respiratory_rate=score.get("respiratory_rate", 0),
                is_nap=s.get("nap", False),
                stage_summary=score.get("stage_summary", {}),
                metrics={
                    "sleep_consistency": score.get("sleep_consistency_percentage"),
                    "sleep_needed": score.get("sleep_needed"),
                },
            )
            count += 1

        next_token = data.get("next_token")
        if not next_token:
            break

    return count


# ── DB helpers ──


async def _upsert_workout(**kw):
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_workouts
                    (platform, external_id, workout_type, sport_name, title,
                     start_time, end_time, duration_seconds, calories_kj,
                     distance_meters, avg_heart_rate, max_heart_rate, strain, metrics)
                VALUES
                    (:platform, :external_id, :workout_type, :sport_name, :title,
                     :start_time, :end_time, :duration_seconds, :calories_kj,
                     :distance_meters, :avg_heart_rate, :max_heart_rate, :strain,
                     CAST(:metrics AS jsonb))
                ON CONFLICT (platform, external_id) DO UPDATE SET
                    duration_seconds = EXCLUDED.duration_seconds,
                    calories_kj = EXCLUDED.calories_kj,
                    avg_heart_rate = EXCLUDED.avg_heart_rate,
                    max_heart_rate = EXCLUDED.max_heart_rate,
                    strain = EXCLUDED.strain,
                    metrics = EXCLUDED.metrics
            """),
            {**kw, "metrics": _json(kw.get("metrics"))},
        )
        await session.commit()


async def _upsert_recovery(**kw):
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_recovery
                    (platform, external_id, record_date, recovery_score,
                     resting_heart_rate, hrv_rmssd, spo2_percentage,
                     skin_temp_celsius, metrics)
                VALUES
                    (:platform, :external_id, :record_date, :recovery_score,
                     :resting_heart_rate, :hrv_rmssd, :spo2_percentage,
                     :skin_temp_celsius, CAST(:metrics AS jsonb))
                ON CONFLICT (platform, external_id) DO UPDATE SET
                    recovery_score = EXCLUDED.recovery_score,
                    resting_heart_rate = EXCLUDED.resting_heart_rate,
                    hrv_rmssd = EXCLUDED.hrv_rmssd,
                    spo2_percentage = EXCLUDED.spo2_percentage,
                    skin_temp_celsius = EXCLUDED.skin_temp_celsius,
                    metrics = EXCLUDED.metrics
            """),
            {**kw, "metrics": _json(kw.get("metrics"))},
        )
        await session.commit()


async def _upsert_sleep(**kw):
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_sleep
                    (platform, external_id, start_time, end_time,
                     total_duration_seconds, sleep_performance, sleep_efficiency,
                     respiratory_rate, is_nap, stage_summary, metrics)
                VALUES
                    (:platform, :external_id, :start_time, :end_time,
                     :total_duration_seconds, :sleep_performance, :sleep_efficiency,
                     :respiratory_rate, :is_nap, CAST(:stage_summary AS jsonb),
                     CAST(:metrics AS jsonb))
                ON CONFLICT (platform, external_id) DO UPDATE SET
                    sleep_performance = EXCLUDED.sleep_performance,
                    sleep_efficiency = EXCLUDED.sleep_efficiency,
                    respiratory_rate = EXCLUDED.respiratory_rate,
                    stage_summary = EXCLUDED.stage_summary,
                    metrics = EXCLUDED.metrics
            """),
            {
                **kw,
                "stage_summary": _json(kw.get("stage_summary")),
                "metrics": _json(kw.get("metrics")),
            },
        )
        await session.commit()


async def _get_last_sync(platform: str) -> datetime | None:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT last_sync_at FROM fitness_sync_state WHERE platform = :p"),
            {"p": platform},
        )
        row = result.scalar_one_or_none()
        return row


async def _update_sync_state(platform: str, records: int):
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_sync_state (platform, last_sync_at, status, records_synced, updated_at)
                VALUES (:platform, NOW(), 'ok', :records, NOW())
                ON CONFLICT (platform) DO UPDATE SET
                    last_sync_at = NOW(), status = 'ok',
                    records_synced = fitness_sync_state.records_synced + :records,
                    updated_at = NOW()
            """),
            {"platform": platform, "records": records},
        )
        await session.commit()


def _parse_dt(val) -> datetime | None:
    """Convert ISO string to datetime object for asyncpg."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_secs(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 0
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, int((e - s).total_seconds()))
    except Exception:
        return 0


def _json(obj) -> str:
    import json
    if obj is None:
        return "{}"
    return json.dumps(obj, default=str)
