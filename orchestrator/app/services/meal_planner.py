"""Meal planning service — generates weekly meal plans using LLM + family preferences."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.services.family_preference_service import build_preference_context
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.meal_planner")

MEAL_PLAN_SYSTEM_PROMPT = """\
You are PAI's Family Chef — a meal planning assistant.
You create practical, family-friendly weekly meal plans.

Rules:
- Plan 7 days (Monday through Sunday) with breakfast, lunch, dinner, and an optional snack.
- Respect ALL dietary restrictions and allergies — never include allergens.
- Favor meals the family loves/likes. Avoid meals they dislike/hate.
- Include variety — don't repeat the same meal twice in a week.
- Keep meals realistic and achievable for a home cook.
- Respond ONLY with valid JSON matching this schema (no other text):

{
  "week": [
    {
      "day": "Monday",
      "breakfast": "...",
      "lunch": "...",
      "dinner": "...",
      "snack": "..."
    }
  ],
  "shopping_list": ["item1", "item2"],
  "notes": "any special notes"
}
"""


async def generate_meal_plan(
    week_label: str | None = None,
    extra_instructions: str = "",
    http_client=None,
) -> dict:
    """Generate a weekly meal plan based on family preferences."""
    if not week_label:
        now = datetime.now(timezone.utc)
        week_label = f"{now.year}-W{now.isocalendar()[1]:02d}"

    # Build preference context
    pref_context = await build_preference_context()

    # Get recent meal plan history to avoid repetition
    history_context = await _get_recent_plan_summary(limit=2)

    user_prompt_parts = [
        f"Generate a meal plan for week: {week_label}",
        "",
        pref_context,
    ]
    if history_context:
        user_prompt_parts.extend(["", "Recent meal plans (avoid repeating):", history_context])
    if extra_instructions:
        user_prompt_parts.extend(["", f"Additional instructions: {extra_instructions}"])

    user_prompt = "\n".join(user_prompt_parts)

    raw = await generate(
        prompt=user_prompt,
        system_prompt=MEAL_PLAN_SYSTEM_PROMPT,
        http_client=http_client,
    )

    # Parse LLM response
    plan_data = _parse_plan_json(raw)

    # Store in DB
    plan_id = await _store_plan(week_label, plan_data, pref_context)
    plan_data["plan_id"] = plan_id
    plan_data["week_label"] = week_label

    logger.info("meal_plan_generated", extra={"week": week_label, "plan_id": plan_id})
    return plan_data


def _parse_plan_json(raw: str) -> dict:
    """Extract JSON from LLM response, handling code fences."""
    text_clean = raw.strip()
    # Strip code fences
    if text_clean.startswith("```"):
        lines = text_clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text_clean = "\n".join(lines)

    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = text_clean.find("{")
        end = text_clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text_clean[start:end])
            except json.JSONDecodeError:
                pass
        # Return as unstructured if we can't parse
        return {"raw_plan": raw, "parse_error": True}


async def _store_plan(week_label: str, plan_data: dict, pref_snapshot: str) -> int:
    """Store the meal plan in the database."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO meal_plans (week_label, plan, preferences_snapshot, model) "
                "VALUES (:week, :plan, :snapshot, :model) "
                "RETURNING id"
            ),
            {
                "week": week_label,
                "plan": json.dumps(plan_data),
                "snapshot": json.dumps({"context": pref_snapshot}),
                "model": settings.ollama_default_model,
            },
        )
        plan_id = result.scalar()
        await session.commit()
        return plan_id


async def get_meal_plans(limit: int = 5) -> list[dict]:
    """Retrieve recent meal plans."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, week_label, plan, model, created_at "
                "FROM meal_plans ORDER BY created_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        plans = []
        for row in result.mappings():
            plan = dict(row)
            if isinstance(plan["plan"], str):
                plan["plan"] = json.loads(plan["plan"])
            plans.append(plan)
        return plans


async def get_meal_plan(plan_id: int) -> dict | None:
    """Retrieve a specific meal plan by ID."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, week_label, plan, model, created_at "
                "FROM meal_plans WHERE id = :id"
            ),
            {"id": plan_id},
        )
        row = result.mappings().fetchone()
        if not row:
            return None
        plan = dict(row)
        if isinstance(plan["plan"], str):
            plan["plan"] = json.loads(plan["plan"])
        return plan


async def _get_recent_plan_summary(limit: int = 2) -> str:
    """Get a brief text summary of recent plans to avoid repetition."""
    plans = await get_meal_plans(limit=limit)
    if not plans:
        return ""

    summaries = []
    for p in plans:
        plan_data = p["plan"]
        week = plan_data.get("week", [])
        dinners = [day.get("dinner", "?") for day in week if isinstance(day, dict)]
        if dinners:
            summaries.append(f"  {p['week_label']}: dinners were {', '.join(dinners)}")
    return "\n".join(summaries)


# ── Meal Ratings / Feedback ─────────────────────────────────────


async def rate_meal(
    meal_name: str,
    family_member_id: int,
    rating: int,
    would_repeat: bool = True,
    meal_plan_id: int | None = None,
    day_of_week: str = "",
    notes: str = "",
) -> dict:
    """Rate a meal and auto-update preferences based on the rating."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO meal_ratings "
                "(meal_plan_id, meal_name, day_of_week, family_member_id, rating, would_repeat, notes) "
                "VALUES (:plan_id, :meal, :dow, :member_id, :rating, :repeat, :notes) "
                "RETURNING id"
            ),
            {
                "plan_id": meal_plan_id,
                "meal": meal_name.strip().lower(),
                "dow": day_of_week,
                "member_id": family_member_id,
                "rating": rating,
                "repeat": would_repeat,
                "notes": notes,
            },
        )
        rating_id = result.scalar()
        await session.commit()

    # Auto-update preferences based on rating
    sentiment = _rating_to_sentiment(rating)
    from app.services.family_preference_service import set_preference
    await set_preference(
        family_member_id=family_member_id,
        item=meal_name.strip().lower(),
        sentiment=sentiment,
        item_type="dish",
        notes=f"auto from rating {rating}/5",
    )

    logger.info("meal_rated", extra={
        "meal": meal_name, "member_id": family_member_id,
        "rating": rating, "sentiment": sentiment,
    })
    return {"rating_id": rating_id, "sentiment_updated": sentiment}


def _rating_to_sentiment(rating: int) -> str:
    """Map a 1-5 rating to a sentiment label."""
    return {1: "hate", 2: "dislike", 3: "neutral", 4: "like", 5: "love"}.get(rating, "neutral")


async def get_meal_ratings(
    meal_plan_id: int | None = None,
    family_member_id: int | None = None,
) -> list[dict]:
    """Get meal ratings, optionally filtered."""
    conditions = []
    params: dict = {}

    if meal_plan_id:
        conditions.append("r.meal_plan_id = :plan_id")
        params["plan_id"] = meal_plan_id
    if family_member_id:
        conditions.append("r.family_member_id = :member_id")
        params["member_id"] = family_member_id

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT r.id, r.meal_plan_id, r.meal_name, r.day_of_week, "
                f"  r.family_member_id, m.name AS member_name, "
                f"  r.rating, r.would_repeat, r.notes, r.created_at "
                f"FROM meal_ratings r "
                f"LEFT JOIN family_members m ON m.id = r.family_member_id "
                f"{where} "
                f"ORDER BY r.created_at DESC"
            ),
            params,
        )
        return [dict(row) for row in result.mappings()]
