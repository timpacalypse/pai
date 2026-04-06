"""Family Health Check — aggregates medications, refill dates, overdue appointments,
immunizations, drug interactions, and expiring prescriptions for all family members."""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import async_session
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.health_check")

# ── Drug interaction checker prompt ──

INTERACTION_CHECK_PROMPT = """\
You are a pharmacology assistant. Given a list of medications currently taken by family members,
identify any known drug-drug interactions, contraindications, or warnings.

Respond ONLY with valid JSON:
{
  "interactions": [
    {
      "drugs": ["drug_a", "drug_b"],
      "severity": "major" | "moderate" | "minor",
      "description": "brief explanation of the interaction"
    }
  ],
  "warnings": ["any general medication warnings"]
}

If no interactions are found, return {"interactions": [], "warnings": []}.
Only flag well-established, clinically significant interactions.
Do NOT fabricate interactions.
"""


async def build_health_check_report(http_client=None) -> dict:
    """Build a comprehensive family health check report."""
    members = await _get_all_family_members()
    if not members:
        return {"error": "No family members found. Add family members first."}

    report_sections = []
    all_medications = {}  # member_name -> [meds]

    for member in members:
        member_id = member["id"]
        member_name = member["name"]
        section = await _build_member_section(member_id, member_name)
        report_sections.append(section)

        if section.get("current_medications"):
            all_medications[member_name] = section["current_medications"]

    # Check drug interactions across all family members
    interactions = {}
    if all_medications:
        interactions = await _check_drug_interactions(all_medications, http_client)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "family_members": report_sections,
        "drug_interactions": interactions,
        "summary": _build_summary(report_sections, interactions),
    }


async def build_health_check_text(http_client=None) -> str:
    """Build a plain-text health check report suitable for chat or email."""
    report = await build_health_check_report(http_client=http_client)
    if report.get("error"):
        return report["error"]

    lines = ["═══ Family Health Check Report ═══", ""]

    for member in report["family_members"]:
        lines.append(f"── {member['name']} ──")

        # Medications
        meds = member.get("current_medications", [])
        if meds:
            lines.append(f"  Medications ({len(meds)}):")
            for med in meds:
                lines.append(f"    • {med}")
        else:
            lines.append("  Medications: None on record")

        # Expiring prescriptions
        expiring = member.get("expiring_prescriptions", [])
        if expiring:
            lines.append(f"  ⚠ Expiring/refill needed ({len(expiring)}):")
            for rx in expiring:
                lines.append(f"    • {rx['medication']} — {rx['note']}")

        # Overdue appointments
        overdue = member.get("overdue_appointments", [])
        if overdue:
            lines.append(f"  ⚠ Overdue appointments ({len(overdue)}):")
            for appt in overdue:
                lines.append(f"    • {appt['category']} — last: {appt['last_date']}, {appt['note']}")

        # Immunization status
        immunizations = member.get("immunizations", [])
        if immunizations:
            lines.append(f"  Immunizations ({len(immunizations)}):")
            for imm in immunizations:
                lines.append(f"    • {imm['summary']} ({imm['date']})")
        else:
            lines.append("  Immunizations: None on record")

        lines.append("")

    # Drug interactions
    interactions = report.get("drug_interactions", {})
    if interactions.get("interactions"):
        lines.append("── Drug Interactions ──")
        for inter in interactions["interactions"]:
            drugs = " + ".join(inter["drugs"])
            lines.append(f"  ⚠ [{inter['severity'].upper()}] {drugs}: {inter['description']}")
        lines.append("")

    if interactions.get("warnings"):
        lines.append("── Warnings ──")
        for w in interactions["warnings"]:
            lines.append(f"  • {w}")
        lines.append("")

    lines.append(report.get("summary", ""))
    return "\n".join(lines)


async def _get_all_family_members() -> list[dict]:
    """Get all family members."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name FROM family_members ORDER BY name")
        )
        return [dict(r) for r in result.mappings()]


async def _build_member_section(member_id: int, member_name: str) -> dict:
    """Build health check section for a single family member."""
    records = await _get_member_records(member_id)

    current_meds = _extract_current_medications(records)
    expiring_rx = _find_expiring_prescriptions(records)
    overdue = _find_overdue_appointments(records)
    immunizations = _get_immunizations(records)

    return {
        "name": member_name,
        "current_medications": current_meds,
        "expiring_prescriptions": expiring_rx,
        "overdue_appointments": overdue,
        "immunizations": immunizations,
        "total_records": len(records),
    }


async def _get_member_records(member_id: int) -> list[dict]:
    """Get all medical records for a member."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, record_date, category, provider, summary, details, "
                "follow_up, medications, vitals, created_at "
                "FROM medical_records WHERE family_member_id = :mid "
                "ORDER BY record_date DESC"
            ),
            {"mid": member_id},
        )
        return [dict(r) for r in result.mappings()]


def _extract_current_medications(records: list[dict]) -> list[str]:
    """Extract unique medications from recent records."""
    meds = set()
    for r in records:
        record_meds = r.get("medications")
        if isinstance(record_meds, list):
            for m in record_meds:
                if m and isinstance(m, str):
                    meds.add(m.strip())
        elif isinstance(record_meds, str) and record_meds.strip():
            meds.add(record_meds.strip())
    return sorted(meds)


def _find_expiring_prescriptions(records: list[dict]) -> list[dict]:
    """Find prescriptions that may need refills (oldest prescription records)."""
    today = datetime.now(timezone.utc).date()
    expiring = []

    rx_records = [r for r in records if r.get("category") == "prescription"]
    for r in rx_records:
        record_date = r.get("record_date")
        if not record_date:
            continue
        if isinstance(record_date, str):
            try:
                record_date = datetime.strptime(record_date, "%Y-%m-%d").date()
            except ValueError:
                continue

        age_days = (today - record_date).days
        meds = r.get("medications", [])
        if isinstance(meds, str):
            meds = [meds] if meds else []

        if age_days > 60:  # Prescriptions older than 60 days may need refill
            for med in meds:
                if med:
                    expiring.append({
                        "medication": med,
                        "last_prescribed": str(record_date),
                        "days_ago": age_days,
                        "note": f"Prescribed {age_days} days ago — may need refill",
                    })

    return expiring


# Standard appointment intervals (in days)
_APPOINTMENT_INTERVALS = {
    "checkup": 365,       # Annual
    "dental": 180,        # Every 6 months
    "vision": 365,        # Annual
    "lab": 365,           # Annual bloodwork
    "mental_health": 90,  # Quarterly
}


def _find_overdue_appointments(records: list[dict]) -> list[dict]:
    """Find categories where the last appointment is overdue."""
    today = datetime.now(timezone.utc).date()
    overdue = []

    # Group records by category, find the most recent date per category
    latest_by_category = {}
    for r in records:
        cat = r.get("category", "other")
        if cat not in _APPOINTMENT_INTERVALS:
            continue
        record_date = r.get("record_date")
        if not record_date:
            continue
        if isinstance(record_date, str):
            try:
                record_date = datetime.strptime(record_date, "%Y-%m-%d").date()
            except ValueError:
                continue
        if cat not in latest_by_category or record_date > latest_by_category[cat]:
            latest_by_category[cat] = record_date

    for cat, interval_days in _APPOINTMENT_INTERVALS.items():
        last_date = latest_by_category.get(cat)
        if not last_date:
            continue  # No records — can't determine if overdue
        days_since = (today - last_date).days
        if days_since > interval_days:
            overdue_by = days_since - interval_days
            overdue.append({
                "category": cat,
                "last_date": str(last_date),
                "days_since": days_since,
                "interval_days": interval_days,
                "overdue_by_days": overdue_by,
                "note": f"Last {cat} was {days_since} days ago (recommended every {interval_days} days) — overdue by {overdue_by} days",
            })

    return overdue


def _get_immunizations(records: list[dict]) -> list[dict]:
    """Get immunization/vaccination records."""
    immunizations = []
    for r in records:
        if r.get("category") == "vaccination":
            immunizations.append({
                "date": str(r.get("record_date", "unknown")),
                "summary": r.get("summary", ""),
                "provider": r.get("provider", ""),
            })
    return immunizations


async def _check_drug_interactions(
    all_medications: dict[str, list[str]],
    http_client=None,
) -> dict:
    """Use LLM to check for drug-drug interactions across all family members."""
    # Build medication list for the prompt
    med_lines = []
    for member, meds in all_medications.items():
        if meds:
            med_lines.append(f"{member}: {', '.join(meds)}")

    if not med_lines:
        return {"interactions": [], "warnings": []}

    prompt = "Check these current medications for drug-drug interactions:\n\n" + "\n".join(med_lines)

    try:
        raw = await generate(
            prompt=prompt,
            system_prompt=INTERACTION_CHECK_PROMPT,
            http_client=http_client,
        )
        return _parse_json(raw)
    except Exception as e:
        logger.error("drug_interaction_check_failed", extra={"error": str(e)})
        return {"interactions": [], "warnings": [], "error": str(e)}


def _build_summary(sections: list[dict], interactions: dict) -> str:
    """Build a brief summary line."""
    total_members = len(sections)
    total_meds = sum(len(s.get("current_medications", [])) for s in sections)
    total_overdue = sum(len(s.get("overdue_appointments", [])) for s in sections)
    total_expiring = sum(len(s.get("expiring_prescriptions", [])) for s in sections)
    total_interactions = len(interactions.get("interactions", []))

    parts = [f"Report covers {total_members} family member(s)"]
    if total_meds:
        parts.append(f"{total_meds} active medication(s)")
    if total_overdue:
        parts.append(f"{total_overdue} overdue appointment(s)")
    if total_expiring:
        parts.append(f"{total_expiring} prescription(s) may need refill")
    if total_interactions:
        parts.append(f"{total_interactions} drug interaction(s) flagged")
    return ". ".join(parts) + "."


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
        return {"interactions": [], "warnings": []}
