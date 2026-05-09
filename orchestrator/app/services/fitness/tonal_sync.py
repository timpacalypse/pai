"""Tonal sync service.

Pulls workout + strength data via Auth0 password grant (same as mobile app).
Based on ToneGet (https://github.com/curlrequests/toneget).
"""

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session

logger = logging.getLogger("pai.fitness.tonal")

AUTH0_DOMAIN = "tonal.auth0.com"
CLIENT_ID = "ERCyexW-xoVG_Yy3RDe-eV4xsOnRHP6L"
API_BASE = "https://api.tonal.com"


async def sync_tonal() -> dict:
    """Sync Tonal workout and strength data. Returns summary."""
    if not settings.tonal_email or not settings.tonal_password:
        return {"status": "skipped", "reason": "no tonal credentials configured"}

    summary = {"workouts": 0, "strength_scores": 0, "errors": []}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Authenticate via Auth0
            auth_resp = await client.post(
                f"https://{AUTH0_DOMAIN}/oauth/token",
                json={
                    "grant_type": "password",
                    "client_id": CLIENT_ID,
                    "username": settings.tonal_email,
                    "password": settings.tonal_password,
                    "scope": "openid profile email offline_access",
                },
            )
            if auth_resp.status_code != 200:
                return {"status": "error", "reason": f"auth failed: {auth_resp.status_code}"}

            tokens = auth_resp.json()
            id_token = tokens.get("id_token", "")
            headers = {"Authorization": f"Bearer {id_token}"}

            # Get user ID
            user_resp = await client.get(f"{API_BASE}/v6/users/userinfo", headers=headers)
            if user_resp.status_code != 200:
                return {"status": "error", "reason": f"userinfo failed: {user_resp.status_code}"}

            user_id = user_resp.json().get("id", "")

            # Sync workouts
            try:
                summary["workouts"] = await _sync_workouts(client, headers, user_id)
            except Exception as e:
                logger.error("tonal_workout_sync_failed", extra={"error": str(e)})
                summary["errors"].append(f"workouts: {e}")

            # Sync strength scores
            try:
                summary["strength_scores"] = await _sync_strength_scores(client, headers, user_id)
            except Exception as e:
                logger.error("tonal_strength_sync_failed", extra={"error": str(e)})
                summary["errors"].append(f"strength: {e}")

    except Exception as e:
        logger.error("tonal_sync_failed", extra={"error": str(e)})
        summary["errors"].append(str(e))

    await _update_sync_state("tonal", summary["workouts"] + summary["strength_scores"])
    logger.info("tonal_sync_complete", extra=summary)
    return summary


async def _sync_workouts(client: httpx.AsyncClient, headers: dict, user_id: str) -> int:
    """Fetch and store Tonal workout activities."""
    count = 0
    offset = 0
    limit = 100

    while True:
        h = {**headers, "pg-offset": str(offset), "pg-limit": str(limit)}
        resp = await client.get(f"{API_BASE}/v6/users/{user_id}/workout-activities", headers=h)

        if resp.status_code != 200:
            logger.warning("tonal_workout_page_failed", extra={"status": resp.status_code, "offset": offset})
            break

        total = int(resp.headers.get("pg-total", 0))
        workouts = resp.json()

        if not workouts:
            break

        for w in workouts:
            external_id = w.get("id", w.get("workoutActivityID", ""))
            if not external_id:
                continue

            # Check if already synced
            if await _exists("fitness_strength", "tonal", str(external_id)):
                continue

            sets_data = w.get("workoutSetActivity", [])
            sets_clean = []
            for s in sets_data:
                sets_clean.append({
                    "weight": s.get("weight"),
                    "reps": s.get("repCount"),
                    "one_rep_max": s.get("oneRepMax"),
                    "rom": s.get("rangeOfMotion"),
                    "movement_id": s.get("movementId"),
                })

            start_time = w.get("beginTime")
            end_time = w.get("endTime")
            duration = _duration_secs(start_time, end_time)

            await _upsert_strength(
                platform="tonal",
                external_id=str(external_id),
                workout_title=w.get("workoutTitle", ""),
                workout_type=w.get("workoutType", ""),
                start_time=_parse_dt(start_time),
                total_volume=w.get("totalVolume", 0),
                total_reps=w.get("totalReps", 0),
                duration_seconds=duration,
                sets=sets_clean,
                strength_scores={},
                metrics={
                    "workout_id": w.get("workoutId"),
                },
            )
            count += 1

        offset += limit
        if offset >= total:
            break

    return count


async def _sync_strength_scores(client: httpx.AsyncClient, headers: dict, user_id: str) -> int:
    """Fetch strength score history and current breakdown."""
    count = 0

    # Current breakdown
    resp = await client.get(f"{API_BASE}/v6/users/{user_id}/strength-scores/current", headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        if isinstance(data, list) and data:
            parsed = {"regions": {}, "muscles": {}}
            for region in data:
                region_name = region.get("strengthBodyRegion", "Unknown")
                parsed["regions"][region_name] = region.get("score", 0)
                for muscle in region.get("familyActivity", []):
                    parsed["muscles"][muscle.get("strengthFamily", "")] = {
                        "score": round(muscle.get("score", 0)),
                        "region": region_name,
                    }

            # Store as a special strength record
            await _upsert_strength(
                platform="tonal",
                external_id=f"strength_snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                workout_title="Strength Score Snapshot",
                workout_type="ASSESSMENT",
                start_time=datetime.now(timezone.utc),
                total_volume=0,
                total_reps=0,
                duration_seconds=0,
                sets=[],
                strength_scores=parsed,
                metrics={},
            )
            count += 1

    # History
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = await client.get(
        f"{API_BASE}/v6/users/{user_id}/strength-scores/history",
        headers=headers,
        params={"limit": 90, "endDate": today},
    )
    if resp.status_code == 200:
        history = resp.json()
        for entry in history[:30]:  # Last 30 entries
            score_date = entry.get("date", today)
            await _upsert_strength(
                platform="tonal",
                external_id=f"strength_history_{score_date}",
                workout_title="Strength Score",
                workout_type="SCORE_HISTORY",
                start_time=_parse_dt(f"{score_date}T00:00:00Z"),
                total_volume=0,
                total_reps=0,
                duration_seconds=0,
                sets=[],
                strength_scores={
                    "overall": entry.get("overall"),
                    "upper": entry.get("upper"),
                    "lower": entry.get("lower"),
                    "core": entry.get("core"),
                },
                metrics={},
            )
            count += 1

    return count


# ── DB helpers ──


async def _exists(table: str, platform: str, external_id: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            text(f"SELECT 1 FROM {table} WHERE platform = :p AND external_id = :eid LIMIT 1"),
            {"p": platform, "eid": external_id},
        )
        return result.scalar_one_or_none() is not None


async def _upsert_strength(**kw):
    async with async_session() as session:
        await session.execute(
            text("""
                INSERT INTO fitness_strength
                    (platform, external_id, workout_title, workout_type,
                     start_time, total_volume, total_reps, duration_seconds,
                     sets, strength_scores, metrics)
                VALUES
                    (:platform, :external_id, :workout_title, :workout_type,
                     :start_time, :total_volume, :total_reps, :duration_seconds,
                     CAST(:sets AS jsonb), CAST(:strength_scores AS jsonb),
                     CAST(:metrics AS jsonb))
                ON CONFLICT (platform, external_id) DO UPDATE SET
                    total_volume = EXCLUDED.total_volume,
                    total_reps = EXCLUDED.total_reps,
                    sets = EXCLUDED.sets,
                    strength_scores = EXCLUDED.strength_scores,
                    metrics = EXCLUDED.metrics
            """),
            {
                **kw,
                "sets": _json(kw.get("sets")),
                "strength_scores": _json(kw.get("strength_scores")),
                "metrics": _json(kw.get("metrics")),
            },
        )
        await session.commit()


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
    """Convert ISO string or datetime to a proper datetime object."""
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
    if obj is None:
        return "{}"
    return json.dumps(obj, default=str)
