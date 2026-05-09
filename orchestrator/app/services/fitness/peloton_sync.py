"""Peloton sync service.

Pulls workout data via Peloton OAuth2 API (password grant via auth.onepeloton.com).
"""

import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session

logger = logging.getLogger("pai.fitness.peloton")

API_BASE = "https://api.onepeloton.com"
TOKEN_URL = "https://auth.onepeloton.com/oauth/token"
CLIENT_ID = "mgsmWCD0A8Qn6uz6mmqI6qeBNHH9IPwS"
REDIRECT_URI = "https://members.onepeloton.com/callback"


async def sync_peloton() -> dict:
    """Sync Peloton workout data. Returns summary."""
    if not settings.peloton_username or not settings.peloton_password:
        return {"status": "skipped", "reason": "no peloton credentials configured"}

    summary = {"workouts": 0, "errors": []}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Authenticate via OAuth2 password grant
            auth_resp = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "password",
                    "client_id": CLIENT_ID,
                    "scope": "offline_access openid",
                    "username": settings.peloton_username,
                    "password": settings.peloton_password,
                },
            )
            if auth_resp.status_code != 200:
                logger.error("peloton_auth_failed", extra={
                    "status": auth_resp.status_code, "body": auth_resp.text[:200]})
                return {"status": "error", "reason": f"auth failed: {auth_resp.status_code}"}

            tokens = auth_resp.json()
            access_token = tokens.get("access_token", "")
            headers = {"Authorization": f"Bearer {access_token}"}

            # Get user ID
            me_resp = await client.get(f"{API_BASE}/api/me", headers=headers)
            if me_resp.status_code != 200:
                return {"status": "error", "reason": f"me endpoint failed: {me_resp.status_code}"}
            user_id = me_resp.json().get("id", "")

            # Fetch workouts
            page = 0
            limit = 20

            while True:
                resp = await client.get(
                    f"{API_BASE}/api/user/{user_id}/workouts",
                    params={"page": page, "limit": limit, "sort_by": "-created"},
                    headers=headers,
                )
                if resp.status_code != 200:
                    summary["errors"].append(f"page {page}: HTTP {resp.status_code}")
                    break

                data = resp.json()
                workouts = data.get("data", [])

                if not workouts:
                    break

                for w in workouts:
                    workout_id = w.get("id", "")

                    # Check if we already have this one
                    if await _workout_exists("peloton", workout_id):
                        # We've caught up to previously synced data
                        summary["workouts"] += 0
                        continue

                    # Fetch detailed metrics
                    metrics_data = {}
                    try:
                        metrics_resp = await client.get(
                            f"{API_BASE}/api/workout/{workout_id}/performance_graph",
                            params={"every_n": 5},
                            headers=headers,
                        )
                        if metrics_resp.status_code == 200:
                            perf = metrics_resp.json()
                            for s in perf.get("summaries", []):
                                metrics_data[s.get("slug", "")] = s.get("value")
                    except Exception:
                        pass  # Metrics are optional

                    overall = w.get("overall_summary") or {}
                    ride = w.get("ride") or {}
                    start_ts = w.get("start_time", 0)
                    end_ts = w.get("end_time", 0)
                    start_time = datetime.fromtimestamp(start_ts, tz=timezone.utc) if start_ts else None
                    end_time = datetime.fromtimestamp(end_ts, tz=timezone.utc) if end_ts else None

                    await _upsert_workout(
                        platform="peloton",
                        external_id=workout_id,
                        workout_type=w.get("fitness_discipline", ""),
                        sport_name=w.get("fitness_discipline", ""),
                        title=ride.get("title", w.get("name", "")),
                        start_time=start_time,
                        end_time=end_time,
                        duration_seconds=end_ts - start_ts if start_ts and end_ts else 0,
                        calories_kj=overall.get("calories", 0) * 4.184 if overall.get("calories") else 0,
                        distance_meters=(overall.get("distance", 0) or 0) * 1609.34,  # miles → meters
                        avg_heart_rate=int(overall.get("avg_heart_rate", 0) or 0),
                        max_heart_rate=int(overall.get("max_heart_rate", 0) or 0),
                        strain=0,
                        metrics={
                            "avg_cadence": overall.get("avg_cadence"),
                            "avg_power": overall.get("avg_power"),
                            "avg_resistance": overall.get("avg_resistance"),
                            "avg_speed": overall.get("avg_speed"),
                            "total_work": overall.get("total_work"),
                            "instructor": w.get("instructor_name"),
                            "difficulty": ride.get("difficulty_estimate"),
                            "leaderboard_rank": w.get("leaderboard_rank"),
                            "leaderboard_total": w.get("total_leaderboard_users"),
                            **metrics_data,
                        },
                    )
                    summary["workouts"] += 1

                if not data.get("show_next", False):
                    break
                page += 1

    except Exception as e:
        logger.error("peloton_sync_failed", extra={"error": str(e)})
        summary["errors"].append(str(e))

    await _update_sync_state("peloton", summary["workouts"])
    logger.info("peloton_sync_complete", extra=summary)
    return summary


# ── DB helpers (shared pattern with whoop_sync) ──


async def _workout_exists(platform: str, external_id: str) -> bool:
    async with async_session() as session:
        result = await session.execute(
            text("SELECT 1 FROM fitness_workouts WHERE platform = :p AND external_id = :eid LIMIT 1"),
            {"p": platform, "eid": external_id},
        )
        return result.scalar_one_or_none() is not None


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
                    metrics = EXCLUDED.metrics
            """),
            {**kw, "metrics": _json(kw.get("metrics"))},
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


def _json(obj) -> str:
    if obj is None:
        return "{}"
    return json.dumps(obj, default=str)
