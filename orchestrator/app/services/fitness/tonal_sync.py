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

    summary = {"workouts": 0, "strength_scores": 0, "new_prs": 0, "errors": []}

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
                workout_result = await _sync_workouts(client, headers, user_id)
                summary["workouts"] = workout_result["count"]
                summary["new_prs"] = workout_result["new_prs"]
            except Exception as e:
                logger.error("tonal_workout_sync_failed", extra={"error": str(e)})
                summary["errors"].append(f"workouts: {e}")

            # Sync strength scores
            try:
                summary["strength_scores"] = await _sync_strength_scores(client, headers, user_id)
            except Exception as e:
                logger.error("tonal_strength_sync_failed", extra={"error": str(e)})
                summary["errors"].append(f"strength: {e}")

            # Sync movement names into PR records
            try:
                await _sync_movement_names(client, headers)
            except Exception as e:
                logger.warning("tonal_movement_names_failed", extra={"error": str(e)})

    except Exception as e:
        logger.error("tonal_sync_failed", extra={"error": str(e)})
        summary["errors"].append(str(e))

    await _update_sync_state("tonal", summary["workouts"] + summary["strength_scores"])
    logger.info("tonal_sync_complete", extra=summary)
    return summary


async def _sync_workouts(client: httpx.AsyncClient, headers: dict, user_id: str) -> dict:
    """Fetch and store Tonal workout activities. Returns count and new PRs."""
    count = 0
    new_prs = 0
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
                    "movement_name": s.get("movementName", s.get("name", "")),
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

            # Check for PRs in this workout's sets
            workout_time = _parse_dt(start_time) or datetime.now(timezone.utc)
            prs = await _check_prs(sets_clean, workout_time, str(external_id))
            new_prs += prs

        offset += limit
        if offset >= total:
            break

    return {"count": count, "new_prs": new_prs}


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


# ── Movement names ──


async def _sync_movement_names(client: httpx.AsyncClient, headers: dict):
    """Fetch movement catalog and update PR records with exercise names."""
    resp = await client.get(f"{API_BASE}/v6/movements", headers=headers)
    if resp.status_code != 200:
        return

    movements = resp.json()
    if not isinstance(movements, list):
        return

    # Build lookup: id -> name
    name_map = {m["id"]: m.get("name", "") for m in movements if m.get("id") and m.get("name")}

    if not name_map:
        return

    # Update any PR records that are missing names
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT id, movement_id FROM exercise_prs
            WHERE (movement_name IS NULL OR movement_name = '')
                AND platform = 'tonal'
        """))
        unnamed = r.mappings().fetchall()

        updated = 0
        for row in unnamed:
            name = name_map.get(row["movement_id"])
            if name:
                await session.execute(text("""
                    UPDATE exercise_prs SET movement_name = :name WHERE id = :id
                """), {"name": name, "id": row["id"]})
                updated += 1

        if updated:
            await session.commit()
            logger.info("movement_names_synced", extra={"updated": updated, "total_unnamed": len(unnamed)})


# ── PR tracking ──


async def _check_prs(sets: list[dict], workout_time: datetime, workout_id: str) -> int:
    """Check sets for new personal records. Returns count of new PRs."""
    new_prs = 0
    # Group by movement_id and find best 1RM per movement in this workout
    movement_bests: dict[str, dict] = {}
    for s in sets:
        mid = s.get("movement_id")
        orm = s.get("one_rep_max")
        if not mid or not orm:
            continue
        orm = float(orm)
        if mid not in movement_bests or orm > movement_bests[mid]["value"]:
            movement_bests[mid] = {
                "value": orm,
                "reps": s.get("reps", 1),
                "name": s.get("movement_name", ""),
            }

    if not movement_bests:
        return 0

    async with async_session() as session:
        for mid, best in movement_bests.items():
            # Check current PR
            r = await session.execute(text("""
                SELECT value, movement_name FROM exercise_prs
                WHERE movement_id = :mid AND platform = 'tonal' AND pr_type = 'one_rep_max'
            """), {"mid": mid})
            existing = r.mappings().fetchone()

            if existing is None:
                # First time — insert as new PR
                await session.execute(text("""
                    INSERT INTO exercise_prs
                        (movement_id, movement_name, platform, pr_type, value, reps, workout_id, achieved_at)
                    VALUES (:mid, :name, 'tonal', 'one_rep_max', :value, :reps, :wid, :achieved)
                """), {
                    "mid": mid,
                    "name": best["name"],
                    "value": best["value"],
                    "reps": best["reps"],
                    "wid": workout_id,
                    "achieved": workout_time,
                })
                new_prs += 1
            elif best["value"] > float(existing["value"]):
                # New PR! Update
                prev_val = float(existing["value"])
                # Keep movement name if we have a better one
                name = best["name"] or existing["movement_name"] or ""
                await session.execute(text("""
                    UPDATE exercise_prs
                    SET value = :value, previous_value = :prev, reps = :reps,
                        movement_name = :name, workout_id = :wid, achieved_at = :achieved
                    WHERE movement_id = :mid AND platform = 'tonal' AND pr_type = 'one_rep_max'
                """), {
                    "value": best["value"],
                    "prev": prev_val,
                    "reps": best["reps"],
                    "name": name,
                    "wid": workout_id,
                    "achieved": workout_time,
                    "mid": mid,
                })
                new_prs += 1
                logger.info("new_pr_detected", extra={
                    "movement_id": mid,
                    "movement_name": name,
                    "new_value": best["value"],
                    "previous_value": prev_val,
                })
            else:
                # Update movement name if we now have one
                if best["name"] and not existing["movement_name"]:
                    await session.execute(text("""
                        UPDATE exercise_prs SET movement_name = :name
                        WHERE movement_id = :mid AND platform = 'tonal' AND pr_type = 'one_rep_max'
                    """), {"name": best["name"], "mid": mid})

        await session.commit()

    return new_prs


async def backfill_prs() -> dict:
    """Scan all existing Tonal workout data and establish PR baselines."""
    async with async_session() as session:
        # Get all workouts ordered by date (oldest first so PRs accumulate correctly)
        r = await session.execute(text("""
            SELECT external_id, sets, start_time
            FROM fitness_strength
            WHERE platform = 'tonal'
                AND workout_type NOT IN ('ASSESSMENT', 'SCORE_HISTORY')
                AND jsonb_array_length(sets) > 0
            ORDER BY start_time ASC
        """))
        workouts = r.mappings().fetchall()

    total_prs = 0
    for w in workouts:
        sets = json.loads(w["sets"]) if isinstance(w["sets"], str) else w["sets"]
        workout_time = w["start_time"]
        prs = await _check_prs(sets, workout_time, str(w["external_id"]))
        total_prs += prs

    logger.info("pr_backfill_complete", extra={"workouts_scanned": len(workouts), "prs_established": total_prs})
    return {"workouts_scanned": len(workouts), "prs_established": total_prs}


async def get_all_prs() -> list[dict]:
    """Get all current personal records."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT movement_id, movement_name, value, previous_value, reps, achieved_at
            FROM exercise_prs
            WHERE platform = 'tonal' AND pr_type = 'one_rep_max'
            ORDER BY value DESC
        """))
        return [dict(row) for row in r.mappings().fetchall()]


async def get_recent_prs(days: int = 7) -> list[dict]:
    """Get PRs achieved in the last N days."""
    async with async_session() as session:
        r = await session.execute(text("""
            SELECT movement_id, movement_name, value, previous_value, reps, achieved_at
            FROM exercise_prs
            WHERE platform = 'tonal' AND pr_type = 'one_rep_max'
                AND achieved_at >= NOW() - CAST(:days || ' days' AS interval)
            ORDER BY achieved_at DESC
        """), {"days": str(days)})
        return [dict(row) for row in r.mappings().fetchall()]


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
