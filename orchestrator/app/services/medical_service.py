"""Medical history tracker — CRUD for medical records with NLP intake."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.medical")


MEDICAL_INTAKE_PROMPT = """\
You are a medical record data extractor for a family health tracker.
The user will tell you about a medical event, appointment, or health detail.
Extract structured data from their statement.

Respond ONLY with valid JSON matching this schema (no other text):

{
  "family_member_name": "name of the person this applies to",
  "date": "YYYY-MM-DD or empty string if not mentioned",
  "category": "checkup" | "dental" | "vision" | "specialist" | "emergency" | "lab" | "vaccination" | "prescription" | "surgery" | "mental_health" | "other",
  "provider": "doctor or facility name if mentioned, else empty string",
  "summary": "concise summary of what happened or was discussed",
  "details": "any specific numbers, results, medications, diagnoses mentioned",
  "follow_up": "any follow-up instructions or next appointment mentioned, else empty string",
  "medications": ["list of medications if mentioned"],
  "vitals": {
    "weight": "if mentioned",
    "blood_pressure": "if mentioned",
    "notes": "any other vital signs"
  }
}

Rules:
- Extract the family member's first name from context
- If no date is mentioned, leave it empty
- Keep the summary factual and concise
- Include all specific medical details (lab values, diagnoses, medications)
- Never fabricate information not present in the input
"""


async def process_medical_input(user_text: str, http_client=None) -> dict:
    """Parse natural language medical info and store it."""
    raw = await generate(
        prompt=user_text,
        system_prompt=MEDICAL_INTAKE_PROMPT,
        http_client=http_client,
    )

    parsed = _parse_json(raw)
    if parsed.get("parse_error"):
        return {"error": "Could not parse medical input. Try rephrasing.", "raw": raw}

    member_name = parsed.get("family_member_name", "").strip()
    if not member_name:
        return {"error": "Could not identify which family member this is about."}

    # Resolve family member ID
    member_id = await _resolve_member(member_name)
    if not member_id:
        return {"error": f"Family member '{member_name}' not found. Add them first via /skills/family/member."}

    # Store the record
    record = await add_medical_record(
        family_member_id=member_id,
        date=parsed.get("date", ""),
        category=parsed.get("category", "other"),
        provider=parsed.get("provider", ""),
        summary=parsed.get("summary", ""),
        details=parsed.get("details", ""),
        follow_up=parsed.get("follow_up", ""),
        medications=parsed.get("medications", []),
        vitals=parsed.get("vitals", {}),
    )

    return {
        "intent": "medical",
        "family_member": member_name,
        "record": record,
        "actions": [
            f"Recorded {parsed.get('category', 'medical')} entry for {member_name}",
            f"Summary: {parsed.get('summary', '')}",
        ],
    }


async def _resolve_member(name: str) -> int | None:
    """Look up a family member by name (case-insensitive)."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id FROM family_members WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
            {"name": name.strip()},
        )
        row = result.scalar()
        return row


async def add_medical_record(
    family_member_id: int,
    date: str = "",
    category: str = "other",
    provider: str = "",
    summary: str = "",
    details: str = "",
    follow_up: str = "",
    medications: list[str] | None = None,
    vitals: dict | None = None,
) -> dict:
    """Insert a medical record."""
    record_date = None
    if date:
        try:
            record_date = datetime.strptime(date.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    if not record_date:
        record_date = datetime.now(timezone.utc).date()

    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO medical_records "
                "(family_member_id, record_date, category, provider, summary, details, "
                " follow_up, medications, vitals) "
                "VALUES (:member_id, :rdate, :cat, :provider, :summary, :details, "
                " :follow_up, :meds, CAST(:vitals AS jsonb)) "
                "RETURNING id, family_member_id, record_date, category, provider, summary, "
                "  details, follow_up, medications, vitals, created_at"
            ),
            {
                "member_id": family_member_id,
                "rdate": record_date,
                "cat": category,
                "provider": provider if isinstance(provider, str) else str(provider or ""),
                "summary": summary.strip() if isinstance(summary, str) else json.dumps(summary) if summary else "",
                "details": details.strip() if isinstance(details, str) else json.dumps(details) if details else "",
                "follow_up": follow_up.strip() if isinstance(follow_up, str) else json.dumps(follow_up) if follow_up else "",
                "meds": medications or [],
                "vitals": json.dumps(vitals or {}),
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        logger.info("medical_record_added", extra={"member_id": family_member_id, "category": category})
        return dict(row)


async def get_medical_records(
    family_member_id: int | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve medical records, optionally filtered."""
    conditions = []
    params: dict = {"limit": limit}

    if family_member_id:
        conditions.append("r.family_member_id = :member_id")
        params["member_id"] = family_member_id
    if category:
        conditions.append("r.category = :cat")
        params["cat"] = category

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT r.id, r.family_member_id, m.name AS member_name, "
                f"  r.record_date, r.category, r.provider, r.summary, r.details, "
                f"  r.follow_up, r.medications, r.vitals, r.created_at "
                f"FROM medical_records r "
                f"JOIN family_members m ON m.id = r.family_member_id "
                f"{where} "
                f"ORDER BY r.record_date DESC "
                f"LIMIT :limit"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def get_medical_record(record_id: int) -> dict | None:
    """Get a single medical record by ID."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT r.id, r.family_member_id, m.name AS member_name, "
                "  r.record_date, r.category, r.provider, r.summary, r.details, "
                "  r.follow_up, r.medications, r.vitals, r.file_references, r.created_at "
                "FROM medical_records r "
                "JOIN family_members m ON m.id = r.family_member_id "
                "WHERE r.id = :id"
            ),
            {"id": record_id},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def delete_medical_record(record_id: int) -> bool:
    """Delete a medical record."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM medical_records WHERE id = :id RETURNING id"),
            {"id": record_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


async def attach_file_to_record(record_id: int, filename: str, file_path: str) -> dict:
    """Attach an ingested file reference to a medical record."""
    async with async_session() as session:
        # Append to file_references array
        await session.execute(
            text(
                "UPDATE medical_records "
                "SET file_references = file_references || CAST(:ref AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "id": record_id,
                "ref": json.dumps([{"filename": filename, "path": file_path}]),
            },
        )
        await session.commit()
    return {"record_id": record_id, "attached": filename}


async def build_medical_context(family_member_id: int | None = None) -> str:
    """Build plain-text medical summary for chat context."""
    records = await get_medical_records(family_member_id=family_member_id, limit=20)
    if not records:
        return ""

    lines = ["Family Medical Records (recent):"]
    for r in records:
        date_str = str(r["record_date"]) if r.get("record_date") else "unknown date"
        lines.append(
            f"  {r['member_name']} — {r['category']} on {date_str}: {r['summary']}"
        )
        if r.get("follow_up"):
            lines.append(f"    Follow-up: {r['follow_up']}")
        if r.get("medications"):
            meds = r["medications"] if isinstance(r["medications"], list) else []
            if meds:
                lines.append(f"    Medications: {', '.join(meds)}")
    return "\n".join(lines)


def _parse_json(raw: str) -> dict:
    """Extract JSON from LLM response."""
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
