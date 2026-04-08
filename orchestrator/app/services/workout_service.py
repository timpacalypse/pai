"""Workout program tracking — schedule recurring workouts and log activities."""

import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.workout")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_ABBREV = {"m": 0, "mon": 0, "monday": 0,
              "t": 1, "tu": 1, "tue": 1, "tues": 1, "tuesday": 1,
              "w": 2, "wed": 2, "wednesday": 2,
              "th": 3, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
              "f": 4, "fri": 4, "friday": 4,
              "sa": 5, "sat": 5, "saturday": 5,
              "su": 6, "sun": 6, "sunday": 6}

WORKOUT_INTAKE_PROMPT = """\
You are a workout program parser for a personal fitness tracker.
The user will describe workout schedules or log completed activities.
Today's date is {{today}} ({{day_name}}).

Determine if the user is:
1. **defining a program** (recurring schedule, e.g. "peloton M-W-F 30 minutes")
2. **logging an activity** (something they did today/recently, e.g. "sauna 20 minutes")
3. **both** (e.g. "I ride peloton M-W-F 30 min and I also did cold plunge 5 min today")

Respond ONLY with valid JSON matching this schema (no other text):

{{
  "programs": [
    {{
      "name": "short descriptive name for the program entry",
      "activity": "the exercise/activity type (e.g. peloton, chest workout, plyometrics, yoga, running, sauna, cold plunge)",
      "days": ["mon", "wed", "fri"],
      "duration_minutes": 30,
      "notes": "any extra details"
    }}
  ],
  "logs": [
    {{
      "activity": "activity name",
      "duration_minutes": 20,
      "notes": "any details",
      "metrics": {{}}
    }}
  ]
}}

Rules:
- "programs" is for recurring schedules. "logs" is for one-time activities done today.
- Parse day abbreviations: M=Monday, T=Tuesday, W=Wednesday, Th=Thursday, F=Friday, Sa=Saturday, Su=Sunday
- "m-w-f" means Monday, Wednesday, Friday. "t-th" means Tuesday, Thursday.
- If user says "I do X on Y days" that's a program.
- If user says "I did X" or "X 20 minutes" without specifying days, that's a log.
- If user says "sauna 20 minutes cold plunge 5" those are two separate logs.
- Either "programs" or "logs" can be empty arrays.
- Duration defaults to 30 if not specified for programs, but must be explicit for logs.
"""


def _parse_json(raw: str) -> dict:
    """Extract JSON from LLM response."""
    text_clean = raw.strip()
    if "```json" in text_clean:
        text_clean = text_clean.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text_clean:
        text_clean = text_clean.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        start = text_clean.find("{")
        end = text_clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text_clean[start:end])
            except json.JSONDecodeError:
                pass
    return {"parse_error": True}


def _resolve_days(day_list: list[str]) -> list[int]:
    """Convert day name strings to integer day-of-week (0=Monday)."""
    result = []
    for d in day_list:
        key = d.strip().lower().rstrip(".")
        if key in DAY_ABBREV:
            val = DAY_ABBREV[key]
            if val not in result:
                result.append(val)
    result.sort()
    return result


# ── Core Operations ──────────────────────────────────────────


async def add_program(name: str, activity: str, days_of_week: list[int],
                      duration_minutes: int = 30, notes: str = "") -> dict:
    """Add a recurring workout program."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO workout_programs (name, activity, days_of_week, duration_minutes, notes) "
                "VALUES (:name, :activity, :days, :dur, :notes) "
                "RETURNING id, name, activity, days_of_week, duration_minutes, notes, is_active"
            ),
            {"name": name.strip(), "activity": activity.strip(),
             "days": days_of_week, "dur": duration_minutes, "notes": notes.strip()},
        )
        row = result.mappings().fetchone()
        await session.commit()
        rec = dict(row)
        rec["days_display"] = ", ".join(DAY_NAMES[d] for d in days_of_week if 0 <= d <= 6)
        return rec


async def log_activity(activity: str, duration_minutes: int, notes: str = "",
                       metrics: dict | None = None, log_date: date | None = None,
                       program_id: int | None = None) -> dict:
    """Log a completed workout or activity."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO workout_logs (workout_program_id, activity, duration_minutes, "
                "log_date, notes, metrics) "
                "VALUES (:pid, :activity, :dur, :ldate, :notes, CAST(:metrics AS jsonb)) "
                "RETURNING id, workout_program_id, activity, duration_minutes, log_date, notes, metrics"
            ),
            {"pid": program_id, "activity": activity.strip(),
             "dur": duration_minutes, "ldate": log_date or date.today(),
             "notes": notes.strip(), "metrics": json.dumps(metrics or {})},
        )
        row = result.mappings().fetchone()
        await session.commit()
        rec = dict(row)
        rec["log_date"] = str(rec["log_date"])
        return rec


async def get_programs(active_only: bool = True) -> list[dict]:
    """Get workout programs."""
    async with async_session() as session:
        clause = "WHERE is_active = TRUE" if active_only else ""
        result = await session.execute(
            text(f"SELECT * FROM workout_programs {clause} ORDER BY created_at DESC"),
        )
        rows = [dict(r) for r in result.mappings().all()]
        for r in rows:
            days = r.get("days_of_week", [])
            r["days_display"] = ", ".join(DAY_NAMES[d] for d in days if 0 <= d <= 6)
        return rows


async def get_logs(limit: int = 30, days_back: int | None = None) -> list[dict]:
    """Get recent workout logs."""
    async with async_session() as session:
        where = ""
        params: dict = {"limit": limit}
        if days_back:
            where = "WHERE log_date >= CURRENT_DATE - :days"
            params["days"] = days_back
        result = await session.execute(
            text(f"SELECT * FROM workout_logs {where} ORDER BY log_date DESC, created_at DESC LIMIT :limit"),
            params,
        )
        rows = [dict(r) for r in result.mappings().all()]
        for r in rows:
            r["log_date"] = str(r["log_date"])
        return rows


async def get_todays_workout() -> dict:
    """Get today's scheduled workout(s) from active programs + any logged activities."""
    today_dow = datetime.now(timezone.utc).weekday()  # 0=Monday
    async with async_session() as session:
        # Scheduled programs for today
        progs = await session.execute(
            text(
                "SELECT id, name, activity, duration_minutes, notes "
                "FROM workout_programs "
                "WHERE is_active = TRUE AND :dow = ANY(days_of_week) "
                "ORDER BY created_at"
            ),
            {"dow": today_dow},
        )
        scheduled = [dict(r) for r in progs.mappings().all()]

        # Already logged today
        logs = await session.execute(
            text(
                "SELECT id, activity, duration_minutes, notes, metrics "
                "FROM workout_logs WHERE log_date = CURRENT_DATE "
                "ORDER BY created_at"
            ),
        )
        completed = [dict(r) for r in logs.mappings().all()]

    return {
        "day": DAY_NAMES[today_dow],
        "scheduled": scheduled,
        "completed": completed,
    }


async def deactivate_program(program_id: int) -> bool:
    """Deactivate a workout program."""
    async with async_session() as session:
        result = await session.execute(
            text("UPDATE workout_programs SET is_active = FALSE, updated_at = NOW() WHERE id = :id RETURNING id"),
            {"id": program_id},
        )
        row = result.fetchone()
        await session.commit()
        return row is not None


# ── NL Processing ────────────────────────────────────────────


async def process_workout_input(user_text: str, http_client=None) -> dict:
    """Parse natural language workout input and store programs/logs."""
    today = datetime.now(timezone.utc)
    prompt_text = WORKOUT_INTAKE_PROMPT.format(
        today=today.strftime("%Y-%m-%d"),
        day_name=DAY_NAMES[today.weekday()],
    )

    raw = await generate(
        prompt=user_text,
        system_prompt=prompt_text,
        http_client=http_client,
    )
    parsed = _parse_json(raw)
    if parsed.get("parse_error"):
        return {"error": "Could not parse workout input. Try rephrasing.", "raw": raw}

    actions = []
    programs_added = []
    logs_added = []

    # Process programs
    for prog in parsed.get("programs", []):
        days_raw = prog.get("days", [])
        days_int = _resolve_days(days_raw)
        if not days_int:
            continue
        name = prog.get("name", prog.get("activity", "Workout"))
        activity = prog.get("activity", name)
        duration = int(prog.get("duration_minutes", 30))
        notes = prog.get("notes", "")

        record = await add_program(
            name=name, activity=activity,
            days_of_week=days_int, duration_minutes=duration, notes=notes,
        )
        programs_added.append(record)
        actions.append(
            f"Program added: {activity} — {record['days_display']} ({duration} min)"
        )

    # Process logs
    for log in parsed.get("logs", []):
        activity = log.get("activity", "")
        if not activity:
            continue
        duration = int(log.get("duration_minutes", 0))
        notes = log.get("notes", "")
        metrics = log.get("metrics", {})

        record = await log_activity(
            activity=activity, duration_minutes=duration,
            notes=notes, metrics=metrics,
        )
        logs_added.append(record)
        dur_str = f" ({duration} min)" if duration else ""
        actions.append(f"Logged: {activity}{dur_str}")

    if not actions:
        return {"error": "No workouts or activities found in your message. Try something like: 'peloton M-W-F 30 minutes' or 'sauna 20 minutes'"}

    return {
        "intent": "workout",
        "programs": programs_added,
        "logs": logs_added,
        "actions": actions,
    }


# ── Context Builder (for chat / briefing) ────────────────────


async def build_workout_context() -> str:
    """Build a text summary of workout programs and recent activity."""
    programs = await get_programs()
    today = await get_todays_workout()
    recent = await get_logs(limit=10, days_back=7)

    lines = []

    # Today's workout
    if today["scheduled"]:
        lines.append(f"TODAY ({today['day']}) — Scheduled:")
        for s in today["scheduled"]:
            lines.append(f"  • {s['activity']} — {s['duration_minutes']} min"
                         + (f" ({s['notes']})" if s.get("notes") else ""))
    else:
        lines.append(f"TODAY ({today['day']}) — Rest day (no scheduled workouts)")

    if today["completed"]:
        lines.append("COMPLETED TODAY:")
        for c in today["completed"]:
            lines.append(f"  ✓ {c['activity']} — {c['duration_minutes']} min")

    # Active programs
    if programs:
        lines.append("\nACTIVE PROGRAMS:")
        for p in programs:
            lines.append(f"  {p['activity']} — {p['days_display']} ({p['duration_minutes']} min)")

    # Recent activity log
    if recent:
        lines.append("\nLAST 7 DAYS:")
        for r in recent:
            lines.append(f"  {r['log_date']}: {r['activity']} — {r['duration_minutes']} min")

    return "\n".join(lines) if lines else "No workout programs or activity logged yet."
