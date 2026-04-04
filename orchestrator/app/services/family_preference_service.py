"""Family preference service — CRUD for family members and meal preferences."""

import logging
from datetime import datetime

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.services.family_preferences")


# ── Family Members ──────────────────────────────────────────────


async def add_family_member(
    name: str,
    age_group: str = "adult",
    dietary_restrictions: list[str] | None = None,
    notes: str = "",
) -> dict:
    """Add a new family member. Returns the created record."""
    restrictions = dietary_restrictions or []
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO family_members (name, age_group, dietary_restrictions, notes) "
                "VALUES (:name, :age_group, :restrictions, :notes) "
                "ON CONFLICT (name) DO UPDATE SET "
                "  age_group = EXCLUDED.age_group, "
                "  dietary_restrictions = EXCLUDED.dietary_restrictions, "
                "  notes = EXCLUDED.notes "
                "RETURNING id, name, age_group, dietary_restrictions, notes, created_at"
            ),
            {
                "name": name.strip(),
                "age_group": age_group,
                "restrictions": restrictions,
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        logger.info("family_member_upserted: %s", name)
        return dict(row)


async def get_family_members() -> list[dict]:
    """List all family members."""
    async with async_session() as session:
        result = await session.execute(
            text("SELECT id, name, age_group, dietary_restrictions, notes, created_at "
                 "FROM family_members ORDER BY name")
        )
        return [dict(row) for row in result.mappings()]


async def delete_family_member(member_id: int) -> bool:
    """Delete a family member by ID. Cascades to preferences."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM family_members WHERE id = :id RETURNING id"),
            {"id": member_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


# ── Preferences ─────────────────────────────────────────────────


async def set_preference(
    family_member_id: int,
    item: str,
    sentiment: str,
    item_type: str = "dish",
    notes: str = "",
) -> dict:
    """Add or update a preference for a family member."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO meal_preferences (family_member_id, item, item_type, sentiment, notes) "
                "VALUES (:member_id, :item, :item_type, :sentiment, :notes) "
                "ON CONFLICT (family_member_id, item, item_type) DO UPDATE SET "
                "  sentiment = EXCLUDED.sentiment, "
                "  notes = EXCLUDED.notes, "
                "  updated_at = NOW() "
                "RETURNING id, family_member_id, item, item_type, sentiment, notes"
            ),
            {
                "member_id": family_member_id,
                "item": item.strip().lower(),
                "item_type": item_type,
                "sentiment": sentiment,
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        logger.info("preference_set", extra={"member_id": family_member_id, "item": item, "sentiment": sentiment})
        return dict(row)


async def get_preferences(family_member_id: int | None = None) -> list[dict]:
    """Get preferences, optionally filtered by family member."""
    async with async_session() as session:
        if family_member_id:
            result = await session.execute(
                text(
                    "SELECT p.id, p.family_member_id, m.name AS member_name, "
                    "  p.item, p.item_type, p.sentiment, p.notes "
                    "FROM meal_preferences p "
                    "JOIN family_members m ON m.id = p.family_member_id "
                    "WHERE p.family_member_id = :member_id "
                    "ORDER BY p.item"
                ),
                {"member_id": family_member_id},
            )
        else:
            result = await session.execute(
                text(
                    "SELECT p.id, p.family_member_id, m.name AS member_name, "
                    "  p.item, p.item_type, p.sentiment, p.notes "
                    "FROM meal_preferences p "
                    "JOIN family_members m ON m.id = p.family_member_id "
                    "ORDER BY m.name, p.item"
                )
            )
        return [dict(row) for row in result.mappings()]


async def build_preference_context() -> str:
    """Build a plain-text summary of all family preferences for LLM prompting."""
    members = await get_family_members()
    if not members:
        return "No family members registered yet."

    all_prefs = await get_preferences()
    prefs_by_member: dict[int, list[dict]] = {}
    for p in all_prefs:
        prefs_by_member.setdefault(p["family_member_id"], []).append(p)

    lines = ["Family Meal Preferences:"]
    for m in members:
        mid = m["id"]
        lines.append(f"\n{m['name']} (age group: {m['age_group']}):")
        if m["dietary_restrictions"]:
            lines.append(f"  Dietary restrictions: {', '.join(m['dietary_restrictions'])}")
        member_prefs = prefs_by_member.get(mid, [])
        if member_prefs:
            for sentiment in ["allergy", "hate", "dislike", "neutral", "like", "love"]:
                items = [p["item"] for p in member_prefs if p["sentiment"] == sentiment]
                if items:
                    lines.append(f"  {sentiment.capitalize()}: {', '.join(items)}")
        else:
            lines.append("  No specific preferences recorded yet.")

    return "\n".join(lines)
