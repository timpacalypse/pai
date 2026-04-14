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


import re

def parse_recipe_text(raw_text: str) -> dict:
    """Parse pasted recipe text into structured fields without LLM.
    
    Handles common recipe layouts:
    - Title on first non-empty line
    - Sections identified by headings like 'Ingredients', 'Instructions', 'Directions', 'Notes'
    - Servings/prep/cook time extracted from metadata lines
    """
    lines = raw_text.strip().split("\n")
    if not lines:
        return {"error": "Empty input"}

    title = ""
    ingredients: list[str] = []
    instructions: list[str] = []
    notes_lines: list[str] = []
    source = ""
    source_url = ""
    cuisine = ""
    prep_time = 0
    cook_time = 0
    servings = 0
    tags: list[str] = []

    # Section detection patterns
    section_patterns = {
        "ingredients": re.compile(r"^(ingredients|what you.?ll need|you.?ll need)\s*:?\s*$", re.IGNORECASE),
        "instructions": re.compile(r"^(instructions|directions|steps|method|preparation|how to make)\s*:?\s*$", re.IGNORECASE),
        "notes": re.compile(r"^(notes|tips|variations|chef.?s? notes?|storage)\s*:?\s*$", re.IGNORECASE),
    }

    # Metadata patterns
    time_re = re.compile(r"(\d+)\s*(min|minute|hour|hr)", re.IGNORECASE)
    servings_re = re.compile(r"(?:serves?|servings?|yield|makes)\s*:?\s*(\d+)", re.IGNORECASE)
    prep_re = re.compile(r"^\s*prep\s*(?:time)?\s*:?\s*(\d+)\s*(min|minute|hour|hr)", re.IGNORECASE)
    cook_re = re.compile(r"^\s*(?:cook|bake|roast)\s*(?:time)?\s*:\s*(\d+)\s*(min|minute|hour|hr)", re.IGNORECASE)
    total_re = re.compile(r"total\s*(?:time)?\s*:?\s*(\d+)\s*(min|minute|hour|hr)", re.IGNORECASE)
    url_re = re.compile(r"https?://\S+")
    cuisine_re = re.compile(r"cuisine\s*:?\s*(.+)", re.IGNORECASE)

    current_section = "preamble"
    step_counter = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for section headers
        matched_section = False
        for sec_name, pattern in section_patterns.items():
            if pattern.match(stripped):
                current_section = sec_name
                matched_section = True
                break
        if matched_section:
            continue

        # Extract metadata only from preamble
        if current_section == "preamble":
            pm = prep_re.search(stripped)
            if pm:
                val = int(pm.group(1))
                prep_time = val * 60 if "hour" in pm.group(2).lower() or "hr" in pm.group(2).lower() else val
                continue

            cm = cook_re.search(stripped)
            if cm:
                val = int(cm.group(1))
                cook_time = val * 60 if "hour" in cm.group(2).lower() or "hr" in cm.group(2).lower() else val
                continue

            sm = servings_re.search(stripped)
            if sm:
                servings = int(sm.group(1))
                continue

        um = url_re.search(stripped)
        if um:
            source_url = um.group(0)

        cum = cuisine_re.match(stripped)
        if cum:
            cuisine = cum.group(1).strip()
            continue

        # Assign to sections
        if current_section == "preamble":
            if not title:
                # Skip common junk prefixes
                clean = stripped.lstrip("#").strip().rstrip(":")
                # Strip chat command prefixes like "save this recipe:"
                clean = re.sub(r"^(save|add|store)\s+(this\s+)?recipe\s*:?\s*", "", clean, flags=re.IGNORECASE).strip()
                if clean and len(clean) > 2:
                    title = clean
            elif not any([prep_time, cook_time, servings]) and stripped.lower().startswith("source"):
                source = stripped.split(":", 1)[-1].strip() if ":" in stripped else stripped
            # Auto-detect section start without header
            elif stripped.startswith(("- ", "• ", "* ", "– ")) or re.match(r"^\d+[\./\)]\s", stripped):
                # If we see bullet points early, guess ingredients
                current_section = "ingredients"
                item = re.sub(r"^[-•*–]\s*", "", stripped).strip()
                if item:
                    ingredients.append(item)
        elif current_section == "ingredients":
            item = re.sub(r"^[-•*–]\s*", "", stripped).strip()
            if item:
                # Auto-detect transition to instructions
                if re.match(r"^(instructions|directions|steps|method)\s*:?\s*$", item, re.IGNORECASE):
                    current_section = "instructions"
                else:
                    ingredients.append(item)
        elif current_section == "instructions":
            step = re.sub(r"^\d+[\./\)]\s*", "", stripped).strip()
            step = re.sub(r"^[-•*–]\s*", "", step).strip()
            if step:
                if re.match(r"^(notes|tips)\s*:?\s*$", step, re.IGNORECASE):
                    current_section = "notes"
                else:
                    instructions.append(step)
        elif current_section == "notes":
            note = re.sub(r"^[-•*–]\s*", "", stripped).strip()
            if note:
                notes_lines.append(note)

    if not title:
        title = lines[0].strip().lstrip("#").strip() if lines else "Untitled Recipe"

    return {
        "title": title,
        "ingredients": ingredients,
        "instructions": instructions,
        "source": source,
        "source_url": source_url,
        "cuisine": cuisine,
        "prep_time_min": prep_time,
        "cook_time_min": cook_time,
        "servings": servings,
        "tags": tags,
        "notes": "\n".join(notes_lines),
    }


async def ingest_recipe_text(raw_text: str) -> dict:
    """Parse raw recipe text and save it. No LLM needed."""
    parsed = parse_recipe_text(raw_text)
    if parsed.get("error"):
        return parsed

    if not parsed.get("ingredients") and not parsed.get("instructions"):
        return {"error": "Could not find ingredients or instructions in the text. "
                "Try formatting with 'Ingredients' and 'Instructions' headings."}

    recipe = await save_recipe(
        title=parsed["title"],
        ingredients=parsed["ingredients"],
        instructions=parsed["instructions"],
        source=parsed["source"],
        source_url=parsed["source_url"],
        cuisine=parsed["cuisine"],
        prep_time_min=parsed["prep_time_min"],
        cook_time_min=parsed["cook_time_min"],
        servings=parsed["servings"],
        tags=parsed["tags"],
        notes=parsed["notes"],
    )
    return {"recipe": recipe, "parsed_fields": {k: len(v) if isinstance(v, list) else bool(v) for k, v in parsed.items()}}
