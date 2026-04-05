"""Calendar / event service — family events, appointments, and agenda scheduling."""

import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.calendar")


CALENDAR_INTAKE_PROMPT = """\
You are a calendar event data extractor for a family scheduling system.
The user will tell you about an event, appointment, or date to remember.
Extract structured data from their statement.

Respond ONLY with valid JSON matching this schema (no other text):

{
  "title": "short event title",
  "event_date": "YYYY-MM-DD (required — infer from context like 'next Tuesday', 'June 15', etc.)",
  "event_time": "HH:MM in 24h format if mentioned, else empty string",
  "end_time": "HH:MM if mentioned, else empty string",
  "category": "birthday" | "appointment" | "school" | "activity" | "holiday" | "travel" | "deadline" | "reminder" | "other",
  "family_member_name": "who this is for (or 'family' if it's for everyone)",
  "location": "where, if mentioned",
  "recurrence": "yearly" | "monthly" | "weekly" | "none",
  "notes": "any additional details"
}

Rules:
- Today's date is provided in the user message for relative date calculation
- "next Tuesday" means the upcoming Tuesday from today
- Birthdays are always yearly recurrence
- If no specific person is mentioned, use "family"
- event_date is REQUIRED — try to infer it from any date reference
"""


async def process_calendar_input(user_text: str, http_client=None) -> dict:
    """Parse natural language event info and store it."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"Today is {today}.\n\n{user_text}"

    raw = await generate(
        prompt=prompt,
        system_prompt=CALENDAR_INTAKE_PROMPT,
        http_client=http_client,
    )

    parsed = _parse_json(raw)
    if parsed.get("parse_error"):
        return {"error": "Could not parse event info. Try rephrasing.", "raw": raw}

    title = parsed.get("title", "").strip()
    event_date = parsed.get("event_date", "").strip()
    if not title or not event_date:
        return {"error": "Could not determine event title or date."}

    # Resolve family member if specified
    member_name = parsed.get("family_member_name", "family").strip()
    member_id = None
    if member_name.lower() != "family":
        member_id = await _resolve_member(member_name)

    event = await add_event(
        title=title,
        event_date=event_date,
        event_time=parsed.get("event_time", ""),
        end_time=parsed.get("end_time", ""),
        category=parsed.get("category", "other"),
        family_member_id=member_id,
        family_member_name=member_name,
        location=parsed.get("location", ""),
        recurrence=parsed.get("recurrence", "none"),
        notes=parsed.get("notes", ""),
    )

    actions = [f"Added event: {title} on {event_date}"]
    if parsed.get("recurrence", "none") != "none":
        actions.append(f"Recurs: {parsed['recurrence']}")

    return {
        "intent": "calendar",
        "event": event,
        "actions": actions,
    }


async def _resolve_member(name: str) -> int | None:
    """Look up a family member by name."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id FROM family_members WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
            {"name": name.strip()},
        )
        return result.scalar()


async def add_event(
    title: str,
    event_date: str,
    event_time: str = "",
    end_time: str = "",
    category: str = "other",
    family_member_id: int | None = None,
    family_member_name: str = "family",
    location: str = "",
    recurrence: str = "none",
    notes: str = "",
) -> dict:
    """Insert a calendar event."""
    try:
        parsed_date = datetime.strptime(event_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        parsed_date = datetime.now(timezone.utc).date()

    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO family_events "
                "(title, event_date, event_time, end_time, category, "
                " family_member_id, family_member_name, location, recurrence, notes) "
                "VALUES (:title, :edate, :etime, :end_time, :cat, "
                " :member_id, :member_name, :location, :recurrence, :notes) "
                "RETURNING id, title, event_date, event_time, end_time, category, "
                "  family_member_id, family_member_name, location, recurrence, notes, created_at"
            ),
            {
                "title": title.strip(),
                "edate": parsed_date,
                "etime": event_time,
                "end_time": end_time,
                "cat": category,
                "member_id": family_member_id,
                "member_name": family_member_name,
                "location": location,
                "recurrence": recurrence,
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        logger.info("event_added", extra={"title": title, "date": str(parsed_date)})
        return dict(row)


async def get_events(
    upcoming_days: int | None = None,
    family_member_id: int | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get calendar events, optionally filtered."""
    conditions = []
    params: dict = {"limit": limit}

    if upcoming_days is not None:
        conditions.append(
            f"e.event_date BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '{int(upcoming_days)} days'"
        )
    if family_member_id:
        conditions.append("e.family_member_id = :member_id")
        params["member_id"] = family_member_id
    if category:
        conditions.append("e.category = :cat")
        params["cat"] = category

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT e.id, e.title, e.event_date, e.event_time, e.end_time, "
                f"  e.category, e.family_member_id, e.family_member_name, "
                f"  e.location, e.recurrence, e.notes, e.created_at "
                f"FROM family_events e "
                f"{where} "
                f"ORDER BY e.event_date ASC, e.event_time ASC "
                f"LIMIT :limit"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def get_event(event_id: int) -> dict | None:
    """Get a single event by ID."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, title, event_date, event_time, end_time, category, "
                "  family_member_id, family_member_name, location, recurrence, notes, created_at "
                "FROM family_events WHERE id = :id"
            ),
            {"id": event_id},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def delete_event(event_id: int) -> bool:
    """Delete a calendar event."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM family_events WHERE id = :id RETURNING id"),
            {"id": event_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


async def get_agenda(days: int = 7) -> dict:
    """Get upcoming events for the next N days, grouped by date."""
    events = await get_events(upcoming_days=days)
    by_date: dict[str, list[dict]] = {}
    for e in events:
        date_key = str(e["event_date"])
        if date_key not in by_date:
            by_date[date_key] = []
        by_date[date_key].append(e)

    today_events = by_date.get(str(datetime.now(timezone.utc).date()), [])

    return {
        "period_days": days,
        "total_events": len(events),
        "today_count": len(today_events),
        "agenda": by_date,
    }


async def build_calendar_context(days: int = 14) -> str:
    """Build plain-text agenda for chat context."""
    events = await get_events(upcoming_days=days)
    if not events:
        return ""
    lines = [f"Upcoming events (next {days} days):"]
    for e in events:
        date_str = str(e["event_date"])
        time_str = f" at {e['event_time']}" if e.get("event_time") else ""
        who = f" ({e['family_member_name']})" if e.get("family_member_name", "family") != "family" else ""
        lines.append(f"  {date_str}{time_str}: {e['title']}{who}")
        if e.get("location"):
            lines.append(f"    Location: {e['location']}")
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    text_clean = raw.strip()
    if text_clean.startswith("```"):
        lines = text_clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text_clean = "\n".join(lines)
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
        return {"parse_error": True, "raw": raw}
