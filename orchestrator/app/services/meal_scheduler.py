"""Daily meal scheduler — generates a daily dinner recipe and emails it each morning."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.services.family_preference_service import build_preference_context
from app.services.ollama_service import generate

logger = logging.getLogger("pai.services.meal_scheduler")

DAILY_RECIPE_SYSTEM_PROMPT = """\
You are PAI's Family Chef — a daily recipe assistant.
You suggest a single dinner recipe that the whole family will enjoy.

Rules:
- Suggest ONE dinner recipe for tonight.
- Respect ALL dietary restrictions and allergies — never include allergens.
- Favor meals the family loves/likes. Avoid meals they dislike/hate.
- Include a full ingredient list with quantities.
- Include clear, numbered step-by-step cooking instructions.
- Estimate prep time and cook time.
- Keep it realistic for a weeknight home cook (under 60 min total preferred).
- Respond ONLY with valid JSON matching this schema (no other text):

{
  "dish_name": "...",
  "description": "one-sentence description",
  "prep_time_min": 15,
  "cook_time_min": 30,
  "servings": 4,
  "ingredients": [
    {"item": "chicken breast", "quantity": "1 lb", "notes": "boneless, skinless"}
  ],
  "instructions": [
    "Step 1: ...",
    "Step 2: ..."
  ],
  "tips": "optional chef tips",
  "why_this_dish": "brief note on why this fits the family's preferences"
}
"""


async def generate_daily_recipe(http_client=None) -> dict:
    """Generate a single daily dinner recipe based on family preferences."""
    pref_context = await build_preference_context()
    recent_context = await _get_recent_recipes(limit=7)

    now = datetime.now(timezone.utc)
    day_name = now.strftime("%A")
    date_str = now.strftime("%B %d, %Y")

    user_prompt_parts = [
        f"Suggest a dinner recipe for {day_name}, {date_str}.",
        "",
        pref_context,
    ]
    if recent_context:
        user_prompt_parts.extend([
            "",
            "Recent dinners (DO NOT repeat these):",
            recent_context,
        ])

    user_prompt = "\n".join(user_prompt_parts)

    raw = await generate(
        prompt=user_prompt,
        system_prompt=DAILY_RECIPE_SYSTEM_PROMPT,
        http_client=http_client,
    )

    recipe = _parse_recipe_json(raw)
    recipe["date"] = date_str
    recipe["day"] = day_name

    logger.info("daily_recipe_generated", extra={"dish": recipe.get("dish_name", "unknown")})
    return recipe


def _parse_recipe_json(raw: str) -> dict:
    """Extract JSON from LLM response, handling code fences."""
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
        return {"raw_recipe": raw, "parse_error": True}


async def _get_recent_recipes(limit: int = 7) -> str:
    """Get recent daily recipe names from Redis (lightweight tracking)."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        recent = await r.lrange("pai:daily_recipes", 0, limit - 1)
        await r.aclose()
        if recent:
            return "\n".join(f"  - {name}" for name in recent)
    except Exception as e:
        logger.debug("redis_recent_recipes_failed: %s", e)
    return ""


async def _record_recipe(dish_name: str):
    """Record a recipe name in Redis so we can avoid repeats."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.lpush("pai:daily_recipes", dish_name)
        await r.ltrim("pai:daily_recipes", 0, 13)  # keep last 14
        await r.aclose()
    except Exception as e:
        logger.debug("redis_record_recipe_failed: %s", e)


async def send_daily_recipe_email(recipe: dict) -> bool:
    """Send the daily recipe as a formatted email via Gmail."""
    if not settings.gmail_address or not settings.gmail_app_password:
        logger.warning("gmail_not_configured_for_meal_scheduler")
        return False

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    dish = recipe.get("dish_name", "Tonight's Dinner")
    day = recipe.get("day", "")
    date = recipe.get("date", "")
    subject = f"PAI Tonight's Dinner: {dish} ({day})"

    html_body = _build_recipe_html(recipe)
    text_body = _build_recipe_text(recipe)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"PAI Family Chef <{settings.gmail_address}>"
        msg["To"] = settings.gmail_recipient or settings.gmail_address

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)

        logger.info("daily_recipe_email_sent", extra={"dish": dish, "to": msg["To"]})
        return True

    except Exception as e:
        logger.error("daily_recipe_email_failed: %s", e)
        return False


def _build_recipe_html(recipe: dict) -> str:
    """Build the HTML email body for a daily recipe."""
    dish = recipe.get("dish_name", "Tonight's Dinner")
    desc = recipe.get("description", "")
    prep = recipe.get("prep_time_min", "?")
    cook = recipe.get("cook_time_min", "?")
    servings = recipe.get("servings", "?")
    tips = recipe.get("tips", "")
    why = recipe.get("why_this_dish", "")
    day = recipe.get("day", "")
    date = recipe.get("date", "")

    if recipe.get("parse_error"):
        return f"""
        <html><body style="background:#0f1117;color:#e4e6f0;font-family:'Segoe UI',system-ui,sans-serif;padding:20px;">
        <div style="max-width:600px;margin:0 auto;background:#161822;border-radius:12px;padding:30px;border:1px solid #2a2d42;">
            <h1 style="color:#4f8ef7;">PAI Daily Recipe</h1>
            <p>The recipe couldn't be fully parsed, but here's what we got:</p>
            <pre style="white-space:pre-wrap;color:#b0b3c8;">{recipe.get('raw_recipe', '')}</pre>
        </div></body></html>"""

    # Ingredients
    ingredients_html = ""
    for ing in recipe.get("ingredients", []):
        if isinstance(ing, dict):
            item = ing.get("item", "")
            qty = ing.get("quantity", "")
            notes = ing.get("notes", "")
            note_span = f' <span style="color:#8b8fa8;">({notes})</span>' if notes else ""
            ingredients_html += f'<li style="padding:3px 0;color:#e4e6f0;">{qty} {item}{note_span}</li>'
        else:
            ingredients_html += f'<li style="padding:3px 0;color:#e4e6f0;">{ing}</li>'

    # Instructions
    instructions_html = ""
    for step in recipe.get("instructions", []):
        instructions_html += f'<li style="padding:6px 0;color:#e4e6f0;line-height:1.5;">{step}</li>'

    why_block = f"""
        <div style="background:#1e2235;border-left:3px solid #4f8ef7;padding:12px 16px;margin-top:20px;border-radius:0 8px 8px 0;">
            <strong style="color:#4f8ef7;">Why this dish?</strong>
            <p style="color:#b0b3c8;margin:4px 0 0 0;">{why}</p>
        </div>""" if why else ""

    tips_block = f"""
        <div style="background:#1e2235;padding:12px 16px;margin-top:12px;border-radius:8px;">
            <strong style="color:#ff9800;">Chef's Tips:</strong>
            <p style="color:#b0b3c8;margin:4px 0 0 0;">{tips}</p>
        </div>""" if tips else ""

    return f"""
    <html>
    <body style="background:#0f1117;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px;">
        <div style="max-width:600px;margin:0 auto;background:#161822;border-radius:12px;overflow:hidden;border:1px solid #2a2d42;">
            <div style="background:#1a1d2e;padding:24px 30px;border-bottom:1px solid #2a2d42;">
                <h1 style="color:#4f8ef7;margin:0;font-size:24px;">
                    🍽️ {dish}
                </h1>
                <p style="color:#8b8fa8;margin:6px 0 0 0;font-size:14px;">
                    {day}, {date} &mdash; PAI Family Chef
                </p>
                <p style="color:#b0b3c8;margin:8px 0 0 0;font-size:15px;">{desc}</p>
            </div>

            <div style="padding:24px 30px;">
                <div style="display:flex;gap:24px;margin-bottom:20px;">
                    <div style="text-align:center;">
                        <div style="color:#4f8ef7;font-size:20px;font-weight:700;">{prep}</div>
                        <div style="color:#8b8fa8;font-size:11px;">PREP MIN</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="color:#ff9800;font-size:20px;font-weight:700;">{cook}</div>
                        <div style="color:#8b8fa8;font-size:11px;">COOK MIN</div>
                    </div>
                    <div style="text-align:center;">
                        <div style="color:#4caf50;font-size:20px;font-weight:700;">{servings}</div>
                        <div style="color:#8b8fa8;font-size:11px;">SERVINGS</div>
                    </div>
                </div>

                <h2 style="color:#4f8ef7;font-size:16px;margin:20px 0 10px 0;border-bottom:1px solid #2a2d42;padding-bottom:6px;">
                    Ingredients
                </h2>
                <ul style="list-style:disc;padding-left:20px;margin:0;">
                    {ingredients_html}
                </ul>

                <h2 style="color:#4f8ef7;font-size:16px;margin:24px 0 10px 0;border-bottom:1px solid #2a2d42;padding-bottom:6px;">
                    Instructions
                </h2>
                <ol style="padding-left:20px;margin:0;">
                    {instructions_html}
                </ol>

                {tips_block}
                {why_block}
            </div>

            <div style="padding:16px 30px;background:#1a1d2e;border-top:1px solid #2a2d42;text-align:center;">
                <p style="color:#5c6078;font-size:12px;margin:0;">
                    Generated by PAI Family Chef &bull; Preferences-aware &bull;
                    <a href="http://localhost:3000" style="color:#4f8ef7;text-decoration:none;">Open PAI</a>
                </p>
            </div>
        </div>
    </body>
    </html>"""


def _build_recipe_text(recipe: dict) -> str:
    """Build a plain-text version of the recipe email."""
    dish = recipe.get("dish_name", "Tonight's Dinner")
    desc = recipe.get("description", "")
    prep = recipe.get("prep_time_min", "?")
    cook = recipe.get("cook_time_min", "?")
    servings = recipe.get("servings", "?")
    day = recipe.get("day", "")
    date = recipe.get("date", "")

    if recipe.get("parse_error"):
        return f"PAI Daily Recipe\n\n{recipe.get('raw_recipe', '')}"

    lines = [
        f"PAI Daily Recipe — {day}, {date}",
        f"{'=' * 40}",
        f"\n{dish}",
        f"{desc}",
        f"\nPrep: {prep} min | Cook: {cook} min | Servings: {servings}",
        f"\nINGREDIENTS:",
    ]

    for ing in recipe.get("ingredients", []):
        if isinstance(ing, dict):
            item = ing.get("item", "")
            qty = ing.get("quantity", "")
            notes = f" ({ing['notes']})" if ing.get("notes") else ""
            lines.append(f"  - {qty} {item}{notes}")
        else:
            lines.append(f"  - {ing}")

    lines.append("\nINSTRUCTIONS:")
    for i, step in enumerate(recipe.get("instructions", []), 1):
        lines.append(f"  {step}")

    if recipe.get("tips"):
        lines.append(f"\nCHEF'S TIPS: {recipe['tips']}")
    if recipe.get("why_this_dish"):
        lines.append(f"\nWHY THIS DISH: {recipe['why_this_dish']}")

    return "\n".join(lines)


async def run_daily_meal(send_email: bool = True) -> dict:
    """
    Full daily meal cycle:
    1. Generate a recipe based on family preferences
    2. Email it
    3. Record in Redis for dedup

    Returns a summary dict.
    """
    start = datetime.now(timezone.utc)
    logger.info("daily_meal_run_started")

    async with httpx.AsyncClient(timeout=120.0) as http_client:
        recipe = await generate_daily_recipe(http_client=http_client)

    dish_name = recipe.get("dish_name", "unknown")
    email_sent = False

    if send_email:
        email_sent = await send_daily_recipe_email(recipe)

    # Track in Redis to avoid repeats
    if dish_name != "unknown":
        await _record_recipe(dish_name)

    summary = {
        "started_at": start.isoformat(),
        "dish": dish_name,
        "email_sent": email_sent,
        "parse_error": recipe.get("parse_error", False),
    }

    logger.info("daily_meal_run_completed", extra=summary)
    return summary


async def meal_scheduler_loop():
    """
    Background scheduler loop for daily meal emails.
    Runs once per day at the configured hour (default 7 AM local).
    """
    interval_hours = settings.meal_schedule_hours
    if interval_hours <= 0:
        logger.info("meal_scheduler_disabled")
        return

    logger.info("meal_scheduler_started", extra={"interval_hours": interval_hours})

    while True:
        try:
            await run_daily_meal(
                send_email=bool(settings.gmail_address),
            )
        except Exception as e:
            logger.error("meal_scheduler_run_failed: %s", e)

        await asyncio.sleep(interval_hours * 3600)
