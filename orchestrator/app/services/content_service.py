"""Content generation services — grocery lists, LinkedIn posts, weekly digests, daily dinner.

Uses native tool calling for structured output where applicable.
"""

import json
import logging

from sqlalchemy import text

from app.core.config import settings
from app.core.database import async_session
from app.services.ollama_service import generate, generate_tool_call

logger = logging.getLogger("pai.services.content")


# ── Grocery List ──────────────────────────────────────────────


async def generate_grocery_list(http_client=None) -> dict:
    """Generate a consolidated grocery list from the current meal plan + daily recipes."""
    from app.services.meal_planner import get_meal_plans

    plans = await get_meal_plans(limit=1)
    recent_recipes = await _get_redis_recent_recipes()

    # Gather ingredients from the meal plan
    meal_items = []
    if plans:
        plan = plans[0]
        week = plan.get("plan", {}).get("week", [])
        for day in week:
            if isinstance(day, dict):
                meal_items.append(f"{day.get('day', '')}: {day.get('dinner', '?')}")

    # Also get recipe details from the recipes table
    recipe_details = await _get_recipe_ingredients()

    context_parts = []
    if meal_items:
        context_parts.append("This week's meal plan:\n" + "\n".join(f"  - {m}" for m in meal_items))
    if recipe_details:
        context_parts.append("Saved recipe ingredients:\n" + recipe_details)
    if recent_recipes:
        context_parts.append("Recent daily recipes (from Redis):\n" + "\n".join(f"  - {r}" for r in recent_recipes))

    if not context_parts:
        return {"error": "No meal plans or recipes found. Generate a meal plan first."}

    context = "\n\n".join(context_parts)

    system_prompt = (
        "You are a grocery list generator. Given a meal plan and/or recipes, "
        "produce a consolidated grocery list. Group items by store section. "
        "Combine duplicate ingredients and sum quantities. "
        "Call the generate_grocery_list tool with the consolidated list."
    )

    tools = [{
        "type": "function",
        "function": {
            "name": "generate_grocery_list",
            "description": "Return a consolidated grocery list grouped by store section",
            "parameters": {
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Section name (Produce, Meat/Seafood, Dairy, Pantry, Frozen, Bakery)"},
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "item": {"type": "string"},
                                            "quantity": {"type": "string"},
                                        },
                                        "required": ["item", "quantity"],
                                    },
                                },
                            },
                            "required": ["name", "items"],
                        },
                    },
                },
                "required": ["sections"],
            },
        },
    }]

    result = await generate_tool_call(
        prompt=f"Generate a grocery list for these meals:\n\n{context}",
        system_prompt=system_prompt,
        tools=tools,
        http_client=http_client,
    )

    if result and "sections" in result:
        return result
    return {"error": "Could not generate grocery list"}


def format_grocery_text(grocery: dict) -> str:
    """Format grocery list dict as readable text."""
    if grocery.get("error"):
        return grocery["error"]
    lines = ["🛒 GROCERY LIST", ""]
    for section in grocery.get("sections", []):
        lines.append(f"── {section['name'].upper()} ──")
        for item in section.get("items", []):
            qty = item.get("quantity", "")
            name = item.get("item", "")
            lines.append(f"  □ {qty} {name}".strip())
        lines.append("")
    return "\n".join(lines)


async def _get_recipe_ingredients() -> str:
    """Get ingredient lists from recent saved recipes."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT title, ingredients FROM recipes "
                "ORDER BY created_at DESC LIMIT 7"
            )
        )
        parts = []
        for r in result.mappings():
            title = r["title"]
            ingredients = r["ingredients"]
            if isinstance(ingredients, list):
                ing_text = ", ".join(str(i) for i in ingredients[:10])
            elif isinstance(ingredients, str):
                ing_text = ingredients[:200]
            else:
                ing_text = str(ingredients)[:200]
            parts.append(f"  {title}: {ing_text}")
        return "\n".join(parts)


async def _get_redis_recent_recipes() -> list[str]:
    """Get recent daily recipe names from Redis."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        recent = await r.lrange("pai:daily_recipes", 0, 6)
        await r.aclose()
        return recent
    except Exception:
        return []


# ── LinkedIn Post Draft ──────────────────────────────────────


async def draft_linkedin_post(topic: str = "", http_client=None) -> dict:
    """Draft a LinkedIn thought leadership post from top-scored articles."""
    articles = await _get_top_articles_for_linkedin(limit=5, topic=topic)

    # If topic filter yielded nothing, fall back to all recent articles
    if not articles and topic:
        articles = await _get_top_articles_for_linkedin(limit=5, topic="")

    if not articles:
        return {"error": "No recent articles found for LinkedIn content."}

    article_context = "\n".join(
        f"  {i+1}. [{a['score']:.2f}] {a['title']}\n     URL: {a['url']}\n     Topic: {a['topic']}"
        for i, a in enumerate(articles)
    )

    system_prompt = (
        "You are a LinkedIn ghostwriter for a cybersecurity leader. "
        "Draft a thought-provoking LinkedIn post (150-250 words) based on the provided articles. "
        "Style: professional but approachable, insightful, calls to action. "
        "Include 1-2 relevant hashtags. Reference specific articles as supporting evidence. "
        "Structure: hook → insight → implication → call to action. "
        "Call the draft_linkedin_post tool with the result."
    )

    tools = [{
        "type": "function",
        "function": {
            "name": "draft_linkedin_post",
            "description": "Return a drafted LinkedIn post with title, body, hashtags, and source articles",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short headline for the post"},
                    "post": {"type": "string", "description": "The full LinkedIn post text (150-250 words)"},
                    "hashtags": {"type": "array", "items": {"type": "string"}, "description": "1-2 relevant hashtags"},
                    "source_articles": {"type": "array", "items": {"type": "string"}, "description": "Titles of articles referenced"},
                },
                "required": ["title", "post", "hashtags", "source_articles"],
            },
        },
    }]

    result = await generate_tool_call(
        prompt=f"Draft a LinkedIn post{' about ' + topic if topic else ''} using these top articles:\n\n{article_context}",
        system_prompt=system_prompt,
        tools=tools,
        http_client=http_client,
    )

    if result and "post" in result:
        result["articles_used"] = articles
        return result
    return {"error": "Could not draft LinkedIn post"}


def format_linkedin_text(draft: dict) -> str:
    """Format LinkedIn draft as readable text."""
    if draft.get("error"):
        return draft["error"]
    lines = [
        "📝 LINKEDIN POST DRAFT",
        "",
        f"Title: {draft.get('title', '')}",
        "",
        draft.get("post", ""),
        "",
        "Hashtags: " + " ".join(draft.get("hashtags", [])),
        "",
        "Source articles:",
    ]
    for a in draft.get("source_articles", []):
        lines.append(f"  - {a}")
    return "\n".join(lines)


async def _get_top_articles_for_linkedin(limit: int = 5, topic: str = "") -> list[dict]:
    """Get top scoring articles from the last 7 days."""
    async with async_session() as session:
        where_clause = "WHERE discovered_at > NOW() - INTERVAL '7 days'"
        params: dict = {"limit": limit}
        if topic:
            where_clause += " AND (LOWER(title) LIKE :topic OR LOWER(topic) LIKE :topic)"
            params["topic"] = f"%{topic.lower()}%"
        result = await session.execute(
            text(
                f"SELECT title, url, source, topic, score "
                f"FROM article_ledger "
                f"{where_clause} "
                f"ORDER BY score DESC LIMIT :limit"
            ),
            params,
        )
        return [dict(r) for r in result.mappings()]


# ── Weekly Security Digest ────────────────────────────────────


async def generate_weekly_digest(http_client=None) -> dict:
    """Generate a curated weekly security digest from collected articles."""
    articles = await _get_week_articles(limit=20)

    if not articles:
        return {"error": "No articles collected this week."}

    article_context = "\n".join(
        f"  {i+1}. [{a['score']:.2f}] {a['title']}\n     Source: {a['source']}\n     Topic: {a['topic']}"
        for i, a in enumerate(articles)
    )

    system_prompt = (
        "You are a cybersecurity intelligence analyst. "
        "Synthesize the provided articles into a concise weekly digest. "
        "Structure: executive summary, top developments, key trends, action items. "
        "Call the generate_weekly_digest tool with the result."
    )

    tools = [{
        "type": "function",
        "function": {
            "name": "generate_weekly_digest",
            "description": "Return a structured weekly security digest",
            "parameters": {
                "type": "object",
                "properties": {
                    "executive_summary": {"type": "string", "description": "2-3 sentence overview"},
                    "top_developments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "analysis": {"type": "string"},
                            },
                            "required": ["title", "analysis"],
                        },
                        "description": "Top 5 developments with brief analysis",
                    },
                    "trends": {"type": "array", "items": {"type": "string"}, "description": "Key patterns across articles"},
                    "action_items": {"type": "array", "items": {"type": "string"}, "description": "What a security leader should do this week"},
                    "articles_analyzed": {"type": "integer", "description": "Number of articles analyzed"},
                },
                "required": ["executive_summary", "top_developments", "trends", "action_items", "articles_analyzed"],
            },
        },
    }]

    result = await generate_tool_call(
        prompt=f"Create a weekly security digest from these {len(articles)} articles:\n\n{article_context}",
        system_prompt=system_prompt,
        tools=tools,
        http_client=http_client,
    )

    if result and "executive_summary" in result:
        result["source_articles"] = articles
        return result
    return {"error": "Could not generate weekly digest"}


def format_digest_text(digest: dict) -> str:
    """Format weekly digest as readable text."""
    if digest.get("error"):
        return digest["error"]
    lines = [
        "🔒 WEEKLY AI + CYBERSECURITY DIGEST",
        "",
        "EXECUTIVE SUMMARY:",
        f"  {digest.get('executive_summary', '')}",
        "",
        "TOP DEVELOPMENTS:",
    ]
    for d in digest.get("top_developments", []):
        lines.append(f"  • {d.get('title', '')}")
        lines.append(f"    {d.get('analysis', '')}")
    lines.append("")
    lines.append("KEY TRENDS:")
    for t in digest.get("trends", []):
        lines.append(f"  → {t}")
    lines.append("")
    lines.append("ACTION ITEMS:")
    for a in digest.get("action_items", []):
        lines.append(f"  ☐ {a}")
    lines.append("")
    lines.append(f"Based on {digest.get('articles_analyzed', '?')} articles analyzed this week.")
    return "\n".join(lines)


async def _get_week_articles(limit: int = 20) -> list[dict]:
    """Get top articles from the last 7 days."""
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT title, url, source, topic, score "
                "FROM article_ledger "
                "WHERE discovered_at > NOW() - INTERVAL '7 days' "
                "ORDER BY score DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(r) for r in result.mappings()]


# ── Tonight's Dinner ──────────────────────────────────────────


async def get_tonights_dinner(http_client=None) -> dict:
    """Get today's dinner recipe — either from Redis cache or generate a new one."""
    # Check Redis for today's cached recipe
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        cached = await r.get("pai:tonight_dinner")
        await r.aclose()
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    # Generate a new recipe
    from app.services.meal_scheduler import generate_daily_recipe
    recipe = await generate_daily_recipe(http_client=http_client)

    # Cache it for the rest of the day (expire at midnight-ish)
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.set("pai:tonight_dinner", json.dumps(recipe), ex=86400)
        await r.aclose()
    except Exception:
        pass

    return recipe


def format_dinner_text(recipe: dict) -> str:
    """Format tonight's dinner as readable text."""
    if recipe.get("parse_error"):
        return f"Tonight's recipe couldn't be parsed:\n{recipe.get('raw_recipe', '')}"

    dish = recipe.get("dish_name", "Unknown")
    desc = recipe.get("description", "")
    prep = recipe.get("prep_time_min", "?")
    cook = recipe.get("cook_time_min", "?")
    servings = recipe.get("servings", "?")

    lines = [
        f"🍽️ TONIGHT'S DINNER: {dish}",
        f"  {desc}",
        f"  Prep: {prep} min | Cook: {cook} min | Servings: {servings}",
        "",
        "INGREDIENTS:",
    ]
    for ing in recipe.get("ingredients", []):
        if isinstance(ing, dict):
            notes = f" ({ing['notes']})" if ing.get("notes") else ""
            lines.append(f"  • {ing.get('quantity', '')} {ing.get('item', '')}{notes}")
        else:
            lines.append(f"  • {ing}")

    lines.append("")
    lines.append("INSTRUCTIONS:")
    for step in recipe.get("instructions", []):
        lines.append(f"  {step}")

    if recipe.get("tips"):
        lines.append(f"\nTIPS: {recipe['tips']}")
    if recipe.get("why_this_dish"):
        lines.append(f"\nWHY THIS DISH: {recipe['why_this_dish']}")

    return "\n".join(lines)
