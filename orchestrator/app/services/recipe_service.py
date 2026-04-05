"""Recipe storage service — save, search, and manage family recipes."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.database import async_session

logger = logging.getLogger("pai.services.recipes")


async def save_recipe(
    title: str,
    ingredients: list[str] | None = None,
    instructions: list[str] | None = None,
    source: str = "",
    source_url: str = "",
    cuisine: str = "",
    prep_time_min: int = 0,
    cook_time_min: int = 0,
    servings: int = 0,
    tags: list[str] | None = None,
    notes: str = "",
) -> dict:
    """Save a new recipe or update existing by title."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "INSERT INTO recipes "
                "(title, ingredients, instructions, source, source_url, cuisine, "
                " prep_time_min, cook_time_min, servings, tags, notes) "
                "VALUES (:title, :ingredients, :instructions, :source, :source_url, "
                " :cuisine, :prep, :cook, :servings, :tags, :notes) "
                "ON CONFLICT (LOWER(title)) DO UPDATE SET "
                "  ingredients = EXCLUDED.ingredients, "
                "  instructions = EXCLUDED.instructions, "
                "  source = COALESCE(NULLIF(EXCLUDED.source, ''), recipes.source), "
                "  source_url = COALESCE(NULLIF(EXCLUDED.source_url, ''), recipes.source_url), "
                "  cuisine = COALESCE(NULLIF(EXCLUDED.cuisine, ''), recipes.cuisine), "
                "  prep_time_min = CASE WHEN EXCLUDED.prep_time_min > 0 THEN EXCLUDED.prep_time_min ELSE recipes.prep_time_min END, "
                "  cook_time_min = CASE WHEN EXCLUDED.cook_time_min > 0 THEN EXCLUDED.cook_time_min ELSE recipes.cook_time_min END, "
                "  servings = CASE WHEN EXCLUDED.servings > 0 THEN EXCLUDED.servings ELSE recipes.servings END, "
                "  tags = EXCLUDED.tags, "
                "  notes = COALESCE(NULLIF(EXCLUDED.notes, ''), recipes.notes), "
                "  updated_at = NOW() "
                "RETURNING id, title, ingredients, instructions, source, source_url, "
                "  cuisine, prep_time_min, cook_time_min, servings, tags, notes, created_at"
            ),
            {
                "title": title.strip(),
                "ingredients": ingredients or [],
                "instructions": instructions or [],
                "source": source,
                "source_url": source_url,
                "cuisine": cuisine,
                "prep": prep_time_min,
                "cook": cook_time_min,
                "servings": servings,
                "tags": tags or [],
                "notes": notes,
            },
        )
        row = result.mappings().fetchone()
        await session.commit()
        logger.info("recipe_saved", extra={"title": title})
        return dict(row)


async def get_recipes(
    search: str | None = None,
    cuisine: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search/list recipes."""
    conditions = []
    params: dict = {"limit": limit}

    if search:
        conditions.append("(title ILIKE :search OR :search_tag = ANY(tags))")
        params["search"] = f"%{search}%"
        params["search_tag"] = search.strip().lower()
    if cuisine:
        conditions.append("cuisine ILIKE :cuisine")
        params["cuisine"] = f"%{cuisine}%"
    if tag:
        conditions.append(":tag = ANY(tags)")
        params["tag"] = tag.strip().lower()

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with async_session() as session:
        result = await session.execute(
            text(
                f"SELECT id, title, ingredients, instructions, source, source_url, "
                f"  cuisine, prep_time_min, cook_time_min, servings, tags, notes, "
                f"  family_rating, created_at "
                f"FROM recipes "
                f"{where} "
                f"ORDER BY family_rating DESC NULLS LAST, title "
                f"LIMIT :limit"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


async def get_recipe(recipe_id: int) -> dict | None:
    """Get a single recipe by ID."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, title, ingredients, instructions, source, source_url, "
                "  cuisine, prep_time_min, cook_time_min, servings, tags, notes, "
                "  family_rating, created_at "
                "FROM recipes WHERE id = :id"
            ),
            {"id": recipe_id},
        )
        row = result.mappings().fetchone()
        return dict(row) if row else None


async def rate_recipe(recipe_id: int, rating: int) -> dict:
    """Rate a recipe (1-5). Updates running average."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "UPDATE recipes SET family_rating = :rating, updated_at = NOW() "
                "WHERE id = :id RETURNING id, title, family_rating"
            ),
            {"id": recipe_id, "rating": rating},
        )
        row = result.mappings().fetchone()
        await session.commit()
        if not row:
            return {"error": "Recipe not found"}
        return dict(row)


async def delete_recipe(recipe_id: int) -> bool:
    """Delete a recipe."""
    async with async_session() as session:
        result = await session.execute(
            text("DELETE FROM recipes WHERE id = :id RETURNING id"),
            {"id": recipe_id},
        )
        deleted = result.scalar() is not None
        await session.commit()
        return deleted


async def build_recipe_context(search: str = "") -> str:
    """Build plain-text recipe list for chat context."""
    recipes = await get_recipes(search=search if search else None, limit=10)
    if not recipes:
        return ""
    lines = ["Saved recipes:"]
    for r in recipes:
        rating = f" (rating: {r['family_rating']}/5)" if r.get("family_rating") else ""
        lines.append(f"  - {r['title']}{rating}")
        if r.get("cuisine"):
            lines[-1] += f" [{r['cuisine']}]"
    return "\n".join(lines)
