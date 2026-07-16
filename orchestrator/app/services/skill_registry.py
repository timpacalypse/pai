"""Unified skill registry — makes all capabilities accessible via natural language.

Each skill registers with a name, description, and handlers for read/write actions.
The LLM intent classifier uses this registry dynamically, so new skills are
automatically accessible without changing routing code.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable

logger = logging.getLogger("pai.skill_registry")


@dataclass
class Skill:
    """A registered skill that can be invoked via natural language."""
    id: str                           # unique identifier (e.g. "calendar")
    name: str                         # human-readable name
    description: str                  # what this skill does (shown to LLM)
    examples: list[str]               # example queries that trigger this skill
    read_handler: Callable | None = None    # async fn(message, http_client) -> str
    write_handler: Callable | None = None   # async fn(message, http_client) -> str
    category: str = "general"         # grouping for display


_REGISTRY: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    """Register a skill in the global registry."""
    _REGISTRY[skill.id] = skill
    logger.info("skill_registered", extra={"skill_id": skill.id, "skill_name": skill.name})
    # Invalidate cached classifier prompt
    try:
        from app.services.llm_intent_service import invalidate_classifier_cache
        invalidate_classifier_cache()
    except ImportError:
        pass


def get_skill(skill_id: str) -> Skill | None:
    return _REGISTRY.get(skill_id)


def list_skills() -> list[Skill]:
    return list(_REGISTRY.values())


def build_skill_catalog() -> str:
    """Build a compact catalog of skills for the LLM classifier prompt."""
    lines = []
    for skill in _REGISTRY.values():
        # Compressed: id + description only (no examples) — reduces classifier prompt tokens
        lines.append(f'  - "{skill.id}" — {skill.description}')
    return "\n".join(lines)


# ── Built-in skill registrations ──────────────────────────────


def register_all_skills():
    """Register all built-in skills. Called once at startup."""

    # ── Calendar ──
    async def _calendar_read(message, http_client=None):
        from app.services.calendar_service import build_calendar_context
        return await build_calendar_context(days=14)

    async def _calendar_write(message, http_client=None):
        from app.services.calendar_service import process_calendar_input
        result = await process_calendar_input(message, http_client=http_client)
        if result.get("error"):
            return f"Calendar error: {result['error']}"
        actions = result.get("actions", [])
        return " | ".join(actions) if actions else "Added to calendar."

    register_skill(Skill(
        id="calendar",
        name="Calendar & Events",
        description="View upcoming events, add/schedule appointments, check agenda (includes Google Calendar)",
        examples=["what's on my calendar tomorrow", "add dentist appointment Thursday at 2pm", "when is the next meeting"],
        read_handler=_calendar_read,
        write_handler=_calendar_write,
        category="family",
    ))

    # ── Medical Records ──
    async def _medical_read(message, http_client=None):
        from app.services.medical_service import build_medical_context
        from app.memory.semantic import search_semantic
        from app.services.family_preference_service import get_family_members

        # Detect family member names in the query to scope results
        members = await get_family_members()
        mentioned_names = []
        msg_lower = message.lower()
        for m in members:
            name = m["name"].lower()
            # Check for name or notes (e.g. "Azure" in notes for "Kellan")
            if name in msg_lower:
                mentioned_names.append(m["name"])
            elif m.get("notes"):
                for alias in m["notes"].lower().split():
                    if len(alias) > 2 and alias in msg_lower:
                        mentioned_names.append(m["name"])
                        break

        parts = []
        if mentioned_names:
            # Search documents scoped to each mentioned family member
            for name in mentioned_names:
                docs = await search_semantic(
                    message, limit=5, http_client=http_client,
                    source_prefix=f"file:{name}",
                )
                if docs:
                    parts.append(f"Documents for {name}:")
                    for d in docs:
                        parts.append(f"  [{d['source']}]\n{d['content']}")
        else:
            # No specific family member — search all medical documents
            docs = await search_semantic(
                message, limit=5, http_client=http_client, source_prefix="file:"
            )
            if docs:
                parts.append("Ingested medical documents:")
                for d in docs:
                    parts.append(f"  [{d['source']}]\n{d['content']}")

        # Also include structured medical records (optionally filtered)
        records_ctx = await build_medical_context()
        if records_ctx:
            parts.append(records_ctx)
        return "\n\n".join(parts) if parts else "No medical records or documents found."

    async def _medical_write(message, http_client=None):
        from app.services.medical_service import process_medical_input
        result = await process_medical_input(message, http_client=http_client)
        if result.get("error"):
            return f"Medical record error: {result['error']}"
        actions = result.get("actions", [])
        return " | ".join(actions) if actions else f"Recorded medical entry for {result.get('family_member', 'unknown')}."

    register_skill(Skill(
        id="medical",
        name="Medical Records",
        description="Log medical visits, medications, health data; query health history for any family member",
        examples=["log colonoscopy results from Dr. Smith", "what medications is mom on", "show medical records for Tim"],
        read_handler=_medical_read,
        write_handler=_medical_write,
        category="family",
    ))

    # ── Home Knowledge ──
    async def _home_read(message, http_client=None):
        from app.services.home_knowledge_service import get_alerts, get_home_tasks, get_home_items
        alerts = await get_alerts()
        parts = []
        if alerts["overdue"]:
            parts.append("OVERDUE maintenance:\n" + "\n".join(
                f"  - {t['item_name']}: {t['description']} (due {t.get('next_due_at', 'N/A')})"
                for t in alerts["overdue"]
            ))
        if alerts["upcoming"]:
            parts.append("Upcoming maintenance:\n" + "\n".join(
                f"  - {t['item_name']}: {t['description']} (due {t.get('next_due_at', 'N/A')})"
                for t in alerts["upcoming"]
            ))
        items = await get_home_items()
        if items:
            parts.append(f"Home items tracked: {len(items)}")
            for item in items[:10]:
                parts.append(f"  - {item['name']} ({item.get('category', 'general')}): {item.get('location', '')}")
        return "\n".join(parts) if parts else "No home items or maintenance tasks tracked yet."

    async def _home_write(message, http_client=None):
        from app.services.home_knowledge_service import process_natural_input
        result = await process_natural_input(user_text=message, http_client=http_client)
        if result.get("error"):
            return f"Home record error: {result['error']}"
        actions = result.get("actions", [])
        return " | ".join(actions) if actions else "Saved to the home database."

    register_skill(Skill(
        id="home",
        name="Home Knowledge Base",
        description="Track home appliances, maintenance schedules, property info; log repairs and tasks; check overdue maintenance",
        examples=["add HVAC filter changed today", "what maintenance is overdue", "show all home items"],
        read_handler=_home_read,
        write_handler=_home_write,
        category="family",
    ))

    # ── Meal Planning ──
    async def _meal_read(message, http_client=None):
        from app.services.meal_planner import get_meal_plans
        from app.services.family_preference_service import build_preference_context
        parts = []
        prefs = await build_preference_context()
        if prefs and "No family members" not in prefs:
            parts.append(prefs)
        plans = await get_meal_plans(limit=2)
        for plan in plans:
            week = plan.get("plan", {}).get("week", [])
            if week:
                dinners = [f"  {d.get('day','')}: {d.get('dinner','?')}" for d in week if isinstance(d, dict)]
                parts.append(f"Meal plan ({plan.get('week_label','')}):\n" + "\n".join(dinners))
        return "\n".join(parts) if parts else "No meal plans generated yet."

    async def _meal_write(message, http_client=None):
        from app.services.meal_planner import generate_meal_plan
        result = await generate_meal_plan(http_client=http_client)
        if result.get("error"):
            return f"Meal plan error: {result['error']}"
        week = result.get("plan", {}).get("week", [])
        if week:
            dinners = [f"  {d.get('day','')}: {d.get('dinner','?')}" for d in week if isinstance(d, dict)]
            return "Generated new meal plan:\n" + "\n".join(dinners)
        return "Meal plan generated."

    register_skill(Skill(
        id="meal_planning",
        name="Meal Planning",
        description="Generate weekly meal plans based on family preferences; view current/past meal plans; manage food preferences",
        examples=["generate a meal plan for this week", "what's for dinner this week", "show current meal plan"],
        read_handler=_meal_read,
        write_handler=_meal_write,
        category="family",
    ))

    # ── Recipes ──
    async def _recipe_read(message, http_client=None):
        from app.services.recipe_service import get_recipes
        recipes = await get_recipes(search=message, limit=10)
        if not recipes:
            return "No recipes found matching that query."
        lines = [f"Found {len(recipes)} recipe(s):"]
        for r in recipes:
            rating = f" (rating: {r['family_rating']}/5)" if r.get("family_rating") else ""
            lines.append(f"  - {r['title']} [{r.get('cuisine','')}]{rating}")
        return "\n".join(lines)

    async def _recipe_write(message, http_client=None):
        from app.services.recipe_service import ingest_recipe_text
        result = await ingest_recipe_text(message)
        if result.get("error"):
            return f"Could not parse recipe: {result['error']}"
        r = result["recipe"]
        fields = result["parsed_fields"]
        parts = [f"Saved recipe: {r['title']}"]
        if fields.get("ingredients"):
            parts.append(f"{fields['ingredients']} ingredients")
        if fields.get("instructions"):
            parts.append(f"{fields['instructions']} steps")
        return " · ".join(parts)

    register_skill(Skill(
        id="recipes",
        name="Recipe Collection",
        description="Save, search, and manage recipes; find recipes by cuisine or ingredient",
        examples=["save this recipe for chicken tikka masala", "find pasta recipes", "show my saved recipes"],
        read_handler=_recipe_read,
        write_handler=_recipe_write,
        category="family",
    ))

    # ── Briefing ──
    async def _briefing_read(message, http_client=None):
        from app.services.briefing_service import build_daily_briefing, build_briefing_text
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            briefing = await build_daily_briefing(client)
        return build_briefing_text(briefing)

    async def _briefing_write(message, http_client=None):
        from app.services.briefing_service import send_daily_briefing
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            sent = await send_daily_briefing(client)
        return "Daily briefing sent to your email." if sent else "Failed to send briefing — check Gmail configuration."

    register_skill(Skill(
        id="briefing",
        name="Daily Briefing",
        description="Build or send the daily intelligence briefing (weather, calendar, news, email summary)",
        examples=["give me my daily briefing", "send the morning briefing email", "what's my day look like"],
        read_handler=_briefing_read,
        write_handler=_briefing_write,
        category="professional",
    ))

    # ── Web Research ──
    async def _research_read(message, http_client=None):
        from app.services.article_dedup import get_ledger_stats
        stats = await get_ledger_stats()
        return f"Research stats: {stats.get('total_articles', 0)} articles collected, {stats.get('sources', 0)} unique sources."

    async def _research_write(message, http_client=None):
        from app.services.web_search_service import search_and_extract
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            results = await search_and_extract(message, max_results=5, http_client=client)
        if not results:
            return "No results found."
        lines = [f"Found {len(results)} results:"]
        for r in results:
            d = r.to_dict() if hasattr(r, 'to_dict') else {"title": str(r)}
            lines.append(f"  - {d.get('title', 'Untitled')}: {d.get('snippet', '')[:120]}")
            if d.get('url'):
                lines.append(f"    {d['url']}")
        return "\n".join(lines)

    register_skill(Skill(
        id="web_research",
        name="Web Research",
        description="Search the web for information, articles, news; check research article stats",
        examples=["search for latest AI security news", "research quantum computing advances", "how many articles have been collected"],
        read_handler=_research_read,
        write_handler=_research_write,
        category="professional",
    ))

    # ── Document Ingestion ──
    async def _ingest_write(message, http_client=None):
        from app.services.document_ingestion import ingest_url, ingest_text
        msg = message.strip()
        # Check if it's a URL
        if msg.startswith("http://") or msg.startswith("https://"):
            result = await ingest_url(msg, http_client=http_client)
            return f"Ingested URL: {result.get('title', msg)} ({result.get('chunks', 0)} chunks stored)"
        else:
            result = await ingest_text(msg, source="chat")
            return f"Ingested text ({result.get('chunks', 0)} chunks stored in memory)"

    register_skill(Skill(
        id="document_ingestion",
        name="Document Ingestion",
        description="Ingest a URL or text into the knowledge base / semantic memory for future reference",
        examples=["ingest this article https://example.com/article", "remember this: the server password is...", "save this to memory"],
        read_handler=None,
        write_handler=_ingest_write,
        category="professional",
    ))

    # ── Family Members ──
    async def _family_read(message, http_client=None):
        from app.services.family_preference_service import get_family_members, get_preferences
        members = await get_family_members()
        if not members:
            return "No family members registered yet."
        lines = ["Family members:"]
        for m in members:
            prefs = await get_preferences(m["id"])
            pref_str = ", ".join(f"{p['preference_type']}: {p['value']}" for p in prefs) if prefs else "no preferences set"
            lines.append(f"  - {m['name']} (age {m.get('age', '?')}): {pref_str}")
        return "\n".join(lines)

    async def _family_write(message, http_client=None):
        from app.services.family_preference_service import add_family_member
        from app.services.ollama_service import generate
        import json
        raw = await generate(
            prompt=f"Extract family member info from this message. Return JSON: {{\"name\": \"...\", \"age\": N, \"dietary_restrictions\": \"...\", \"notes\": \"...\"}}. Message: {message}",
            system_prompt="Extract structured data. Return only valid JSON.",
            model="qwen3:4b",
            http_client=http_client,
        )
        try:
            text = raw.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            data = json.loads(text[start:end]) if start >= 0 else {}
        except Exception:
            return "Couldn't parse family member info. Try: 'add family member John, age 12, allergic to peanuts'"
        if not data.get("name"):
            return "I need at least a name."
        result = await add_family_member(data)
        return f"Added family member: {result.get('name', 'unknown')}"

    register_skill(Skill(
        id="family",
        name="Family Members & Preferences",
        description="Add/view family members, set dietary restrictions and food preferences",
        examples=["show family members", "add family member Sarah age 8", "set Tim's preference to no gluten"],
        read_handler=_family_read,
        write_handler=_family_write,
        category="family",
    ))

    # ── Meal Feedback ──
    async def _feedback_read(message, http_client=None):
        from app.services.meal_planner import get_meal_ratings
        ratings = await get_meal_ratings(limit=10)
        if not ratings:
            return "No meal ratings recorded yet."
        lines = ["Recent meal ratings:"]
        for r in ratings:
            lines.append(f"  - {r.get('meal_name', '?')}: {r.get('rating', '?')}/5 — {r.get('notes', '')}")
        return "\n".join(lines)

    async def _feedback_write(message, http_client=None):
        from app.services.meal_planner import rate_meal
        from app.services.ollama_service import generate
        import json
        raw = await generate(
            prompt=f'Extract meal rating from this message. Return JSON: {{"meal_name": "...", "rating": 1-5, "notes": "..."}}. Message: {message}',
            system_prompt="Extract structured data. Return only valid JSON.",
            model="qwen3:4b",
            http_client=http_client,
        )
        try:
            text = raw.strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            data = json.loads(text[start:end]) if start >= 0 else {}
        except Exception:
            return "Couldn't parse that rating. Try: 'rate the chicken pasta 4 out of 5, it was great'"
        result = await rate_meal(data)
        return f"Rated {data.get('meal_name', 'meal')}: {data.get('rating', '?')}/5"

    register_skill(Skill(
        id="meal_feedback",
        name="Meal Ratings & Feedback",
        description="Rate meals, view past meal ratings and feedback",
        examples=["rate tonight's dinner 4 out of 5", "show meal ratings", "the pasta was terrible, 2 stars"],
        read_handler=_feedback_read,
        write_handler=_feedback_write,
        category="family",
    ))

    # ── Learning & Quality ──
    async def _learning_read(message, http_client=None):
        from app.services.quality_service import get_agent_stats
        from app.services.learning_service import get_experiments, get_active_overrides
        stats = await get_agent_stats()
        experiments = await get_experiments(limit=5)
        overrides = await get_active_overrides()
        parts = []
        if stats:
            parts.append("Agent quality stats:\n" + "\n".join(
                f"  - {s.get('agent_name', '?')}: avg={s.get('avg_score', 0):.2f}, tasks={s.get('task_count', 0)}"
                for s in stats
            ))
        if experiments:
            parts.append(f"Recent experiments: {len(experiments)}")
        if overrides:
            parts.append(f"Active prompt overrides: {len(overrides)}")
        return "\n".join(parts) if parts else "No quality data collected yet."

    register_skill(Skill(
        id="learning",
        name="Learning & Quality",
        description="View agent performance metrics, quality stats, learning experiments, and prompt overrides",
        examples=["how are the agents performing", "show quality stats", "any active learning experiments"],
        read_handler=_learning_read,
        write_handler=None,
        category="professional",
    ))

    # ── Family Health Check ──
    async def _health_check_read(message, http_client=None):
        from app.services.health_check_service import build_health_check_text
        return await build_health_check_text(http_client=http_client)

    async def _health_check_write(message, http_client=None):
        from app.services.health_check_service import build_health_check_text
        from app.services.process_engine import _skill_email_send
        from app.core.config import settings
        report = await build_health_check_text(http_client=http_client)
        if report.startswith("No family members") or report.startswith("Error"):
            return report
        try:
            await _skill_email_send({
                "to": [settings.gmail_address],
                "subject": "PAI Family Health Check Report",
                "body": report,
            })
            return report + "\n\n✉ Report emailed."
        except Exception as e:
            logger.warning("health_check_email_failed", extra={"error": str(e)})
            return report + f"\n\n(Email failed: {e})"

    register_skill(Skill(
        id="health_check",
        name="Family Health Check",
        description="Review medications, refill dates, overdue appointments, immunization status, and drug interactions for all family members; optionally email the report",
        examples=["run a family health check", "any overdue appointments", "check medication interactions", "health check report"],
        read_handler=_health_check_read,
        write_handler=_health_check_write,
        category="family",
    ))

    # ── Article Curation ──
    async def _article_curation_read(message, http_client=None):
        from app.services.article_curation_service import (
            curate_articles_text,
            search_collected_articles_text,
        )
        import re
        lower = message.lower()

        # Detect "search my collected articles" intent
        search_patterns = [
            r"\b(based on|from|using|in)\b.*\b(collected|stored|my)\b.*\bartcicle",
            r"\b(collected|stored|my)\b.*\barticles?\b",
            r"\bfind\b.*\barticles?\b",
            r"\barticles?\b.*\b(about|on|related to|concerning|regarding)\b",
            r"\bsearch\b.*\barticles?\b",
            r"\buse cases?\b.*\barticles?\b",
            r"\barticles?\b.*\b(links?|url)\b",
            r"\bexample.*from.*article",
        ]
        is_search = any(re.search(p, lower) for p in search_patterns)

        # Extract query: strip command words and use remaining as search terms
        query = re.sub(
            r"\b(find|search|show|get|retrieve|list|give me|provide|based on|from|using|in|my|collected|stored|articles?|links?|urls?)\b",
            " ", lower
        ).strip()
        query = re.sub(r"\s+", " ", query).strip()

        if is_search and query and len(query) > 3:
            return await search_collected_articles_text(query, limit=10)

        # Default: curate fresh articles from web
        return await curate_articles_text()

    async def _article_curation_write(message, http_client=None):
        from app.services.article_curation_service import (
            curate_articles_text,
            search_collected_articles_text,
        )
        from app.services.ollama_service import generate as llm_generate
        import re
        lower = message.lower()

        # Search collected articles when intent is retrieval
        search_patterns = [
            r"\b(based on|from|using|in)\b.*\b(collected|stored|my)\b.*\barticle",
            r"\b(collected|stored|my)\b.*\barticles?\b",
            r"\bfind\b.*\barticles?\b",
            r"\barticles?\b.*\b(about|on|related to|concerning|regarding)\b",
            r"\bsearch\b.*\barticles?\b",
            r"\buse cases?\b.*\barticles?\b",
            r"\barticles?\b.*\b(links?|url)\b",
            r"\bexample.*from.*article",
        ]
        is_search = any(re.search(p, lower) for p in search_patterns)

        if is_search:
            query = re.sub(
                r"\b(find|search|show|get|retrieve|list|give me|provide|based on|from|using|in|my|collected|stored|articles?|links?|urls?)\b",
                " ", lower
            ).strip()
            query = re.sub(r"\s+", " ", query).strip()
            if query and len(query) > 3:
                return await search_collected_articles_text(query, limit=10)

        # Curate fresh articles, optionally for a specific topic
        topics = None
        raw_topic = await llm_generate(
            prompt=f"Extract the topic the user wants articles about. Return ONLY the topic phrase. If no specific topic, return 'general'.\n\nUser message: {message}",
            system_prompt="Extract topics. Return only the short topic phrase. No explanation.",
            model="qwen3:4b",
            http_client=http_client,
        )
        topic = raw_topic.strip().strip('"').strip("'")
        if topic.lower() not in ("general", "none", "n/a", ""):
            topics = [topic]
        return await curate_articles_text(topics=topics)

    register_skill(Skill(
        id="article_curation",
        name="Article Curation",
        description="Search collected articles by topic or keyword; curate fresh top articles from the web; score for relevance and suggest LinkedIn thought leadership angles",
        examples=[
            "find articles about AI governance",
            "articles about NIST framework with links",
            "articles related to AI framework use cases",
            "curate articles",
            "what are the top articles this week",
            "curate content for LinkedIn",
            "use cases based on collected articles",
        ],
        read_handler=_article_curation_read,
        write_handler=_article_curation_write,
        category="professional",
    ))

    # ── Memory ──
    async def _memory_read(message, http_client=None):
        from app.memory.semantic import search_semantic
        results = await search_semantic(message, limit=5, http_client=http_client)
        if not results:
            return "No relevant memories found."
        lines = ["Relevant memories:"]
        for r in results:
            sim = f" (similarity: {r['similarity']:.2f})" if r.get("similarity") else ""
            lines.append(f"  - {r['content'][:200]}{sim}")
        return "\n".join(lines)

    register_skill(Skill(
        id="memory",
        name="Knowledge Memory",
        description="Search the knowledge base / semantic memory for previously stored information",
        examples=["what do you know about kubernetes", "search memory for cybersecurity", "recall what we discussed about AI governance"],
        read_handler=_memory_read,
        write_handler=None,
        category="professional",
    ))

    # ── Workout Tracking ──
    async def _workout_read(message, http_client=None):
        from app.services.workout_service import build_workout_context
        return await build_workout_context(message)

    async def _workout_write(message, http_client=None):
        from app.services.workout_service import process_workout_input
        result = await process_workout_input(message, http_client=http_client)
        if result.get("error"):
            return f"Workout error: {result['error']}"
        actions = result.get("actions", [])
        return " | ".join(actions) if actions else "Workout recorded."

    register_skill(Skill(
        id="workout",
        name="Workout Program",
        description="View workout schedule for any day, check today's exercises; log activities like peloton, weights, sauna, cold plunge",
        examples=["what's my workout today", "what is friday's workout", "peloton M-W-F 30 minutes", "sauna 20 minutes cold plunge 5"],
        read_handler=_workout_read,
        write_handler=_workout_write,
        category="family",
    ))

    # ── Grocery List ──
    async def _grocery_read(message, http_client=None):
        from app.services.content_service import generate_grocery_list, format_grocery_text
        result = await generate_grocery_list(http_client=http_client)
        return format_grocery_text(result)

    register_skill(Skill(
        id="grocery",
        name="Grocery List",
        description="Generate a consolidated grocery list from this week's meal plan and saved recipes",
        examples=["make a grocery list", "what do I need from the store", "grocery list for this week", "shopping list"],
        read_handler=_grocery_read,
        write_handler=_grocery_read,
        category="family",
    ))

    # ── LinkedIn Post Draft ──
    async def _linkedin_read(message, http_client=None):
        from app.services.content_service import draft_linkedin_post, format_linkedin_text
        from app.services.ollama_service import generate as llm_generate
        # Use light LLM to extract the desired topic from the user's message
        raw_topic = await llm_generate(
            prompt=f"Extract the topic the user wants a LinkedIn post about. Return ONLY the topic phrase, nothing else. If no specific topic, return 'general'.\n\nUser message: {message}",
            system_prompt="Extract topics. Return only the short topic phrase. No explanation.",
            model="qwen3:4b",
            http_client=http_client,
        )
        topic = raw_topic.strip().strip('"').strip("'")
        if topic.lower() in ("general", "none", "n/a", ""):
            topic = ""
        result = await draft_linkedin_post(topic=topic, http_client=http_client)
        return format_linkedin_text(result)

    register_skill(Skill(
        id="linkedin",
        name="LinkedIn Post Drafting",
        description="Draft a thought leadership LinkedIn post from top-scored cybersecurity/AI articles this week",
        examples=["draft a LinkedIn post", "write a LinkedIn post about AI security", "LinkedIn content", "draft post for LinkedIn"],
        read_handler=_linkedin_read,
        write_handler=_linkedin_read,
        category="professional",
    ))

    # ── Weekly Security Digest ──
    async def _digest_read(message, http_client=None):
        from app.services.content_service import generate_weekly_digest, format_digest_text
        result = await generate_weekly_digest(http_client=http_client)
        return format_digest_text(result)

    register_skill(Skill(
        id="weekly_digest",
        name="Weekly Security Digest",
        description="Generate a curated weekly AI + cybersecurity intelligence digest with top developments, trends, and action items",
        examples=["weekly security digest", "weekly briefing", "security roundup this week", "what happened in cybersecurity this week"],
        read_handler=_digest_read,
        write_handler=_digest_read,
        category="professional",
    ))

    # ── Tonight's Dinner ──
    async def _dinner_read(message, http_client=None):
        from app.services.content_service import get_tonights_dinner, format_dinner_text
        result = await get_tonights_dinner(http_client=http_client)
        return format_dinner_text(result)

    register_skill(Skill(
        id="tonights_dinner",
        name="Tonight's Dinner",
        description="Check what's for dinner tonight; shows today's recipe with ingredients and instructions",
        examples=["what's for dinner", "what's for dinner tonight", "tonight's dinner", "what are we eating tonight"],
        read_handler=_dinner_read,
        write_handler=None,
        category="family",
    ))

    # ── Fitness platform skills ──

    async def _fitness_summary_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_fitness_summary
        return await get_fitness_summary(days=7)

    async def _fitness_detailed_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_fitness_summary
        days = 7
        lower = message.lower()
        if "month" in lower or "30" in lower:
            days = 30
        elif "2 week" in lower or "14" in lower:
            days = 14
        return await get_fitness_summary(days=days)

    async def _fitness_sync_write(message, http_client=None):
        from app.services.fitness.fitness_query import trigger_sync
        return await trigger_sync()

    register_skill(Skill(
        id="fitness_data",
        name="Fitness Data",
        description="Query fitness data from Whoop, Peloton, and Tonal — workouts, recovery, sleep, strain, HRV, strength scores. Ask for summaries, trends, or recommendations.",
        examples=[
            "how's my recovery looking", "show my fitness data",
            "whoop recovery trends", "my peloton workouts this week",
            "tonal strength scores", "how did I sleep this week",
            "fitness summary", "my HRV trends", "am I overtraining",
            "what does my recovery say about training today",
        ],
        read_handler=_fitness_detailed_read,
        write_handler=_fitness_sync_write,
        category="personal",
    ))

    async def _workout_history_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_workout_details
        days = 7
        lower = message.lower()
        if "month" in lower or "30" in lower:
            days = 30
        elif "2 week" in lower or "14" in lower:
            days = 14
        platform = ""
        if "whoop" in lower:
            platform = "whoop"
        elif "peloton" in lower:
            platform = "peloton"
        return await get_workout_details(days=days, platform=platform)

    register_skill(Skill(
        id="workout_history",
        name="Workout History",
        description="View detailed workout history from Whoop, Peloton, and Tonal — duration, heart rate, calories, strain",
        examples=[
            "my workouts this week", "show my peloton rides",
            "whoop workouts", "what exercises did I do",
            "workout log", "training history",
        ],
        read_handler=_workout_history_read,
        write_handler=None,
        category="personal",
    ))

    async def _recovery_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_recovery_trends
        days = 14
        lower = message.lower()
        if "month" in lower or "30" in lower:
            days = 30
        return await get_recovery_trends(days=days)

    register_skill(Skill(
        id="recovery_data",
        name="Recovery & HRV",
        description="View Whoop recovery scores, HRV trends, resting heart rate, SpO2 data",
        examples=[
            "recovery score", "HRV trends", "resting heart rate",
            "am I recovered", "recovery this week", "SpO2 data",
        ],
        read_handler=_recovery_read,
        write_handler=None,
        category="personal",
    ))

    async def _sleep_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_sleep_analysis
        days = 14
        lower = message.lower()
        if "month" in lower or "30" in lower:
            days = 30
        return await get_sleep_analysis(days=days)

    register_skill(Skill(
        id="sleep_data",
        name="Sleep Analysis",
        description="View sleep data from Whoop — duration, performance, efficiency, stages",
        examples=[
            "how did I sleep", "sleep quality", "sleep performance",
            "sleep trends", "am I sleeping enough",
        ],
        read_handler=_sleep_read,
        write_handler=None,
        category="personal",
    ))

    async def _strength_read(message, http_client=None):
        from app.services.fitness.fitness_query import get_strength_progress
        days = 30
        lower = message.lower()
        if "week" in lower or "7" in lower:
            days = 7
        return await get_strength_progress(days=days)

    register_skill(Skill(
        id="strength_data",
        name="Strength Progress",
        description="View Tonal strength scores, muscle breakdown, volume and rep trends",
        examples=[
            "tonal strength score", "strength progress",
            "muscle breakdown", "lifting volume", "tonal workouts",
        ],
        read_handler=_strength_read,
        write_handler=None,
        category="personal",
    ))

    # ── Exercise PRs ──
    async def _exercise_prs_read(message, http_client=None):
        from app.services.fitness.tonal_sync import get_all_prs, get_recent_prs
        lower = message.lower()

        # Recent PRs
        if "recent" in lower or "new" in lower or "latest" in lower or "this week" in lower:
            days = 7
            if "month" in lower or "30" in lower:
                days = 30
            prs = await get_recent_prs(days=days)
            if not prs:
                return f"No new PRs in the last {days} days. Keep pushing."
            lines = [f"**Recent PRs (last {days} days):**\n"]
            for p in prs:
                name = p["movement_name"] or p["movement_id"][:8]
                prev = f" (prev: {p['previous_value']:.1f})" if p.get("previous_value") else ""
                lines.append(f"  🏆 **{name}** — {p['value']:.1f} lbs 1RM{prev}")
            return "\n".join(lines)

        # Search for specific exercise
        import re
        search_terms = re.sub(r'\b(pr|prs|personal record|best|max|what|my|is|for|the|show|exercise)\b', '', lower).strip()
        if search_terms and len(search_terms) > 2:
            all_prs = await get_all_prs()
            matches = [p for p in all_prs if search_terms in (p.get("movement_name") or "").lower()]
            if matches:
                lines = [f"**PRs matching '{search_terms}':**\n"]
                for p in matches:
                    prev = f" (prev: {p['previous_value']:.1f})" if p.get("previous_value") else ""
                    lines.append(f"  🏆 **{p['movement_name']}** — {p['value']:.1f} lbs 1RM{prev} ({p['achieved_at'].strftime('%Y-%m-%d')})")
                return "\n".join(lines)

        # Default: top PRs
        all_prs = await get_all_prs()
        if not all_prs:
            return "No PRs tracked yet. Complete a Tonal workout to start tracking."
        lines = [f"**Top Personal Records** ({len(all_prs)} exercises tracked):\n"]
        for p in all_prs[:15]:
            name = p["movement_name"] or p["movement_id"][:8]
            prev = f" ↑ from {p['previous_value']:.1f}" if p.get("previous_value") else ""
            lines.append(f"  🏆 **{name}** — {p['value']:.1f} lbs{prev} ({p['achieved_at'].strftime('%Y-%m-%d')})")
        if len(all_prs) > 15:
            lines.append(f"\n  ...and {len(all_prs) - 15} more. Ask about a specific exercise.")
        return "\n".join(lines)

    register_skill(Skill(
        id="exercise_prs",
        name="Exercise PRs",
        description="View personal records (PRs) from Tonal — best estimated 1RM for each exercise, recent PRs, search by exercise name",
        examples=[
            "show my PRs", "personal records", "what are my PRs",
            "recent PRs", "new PRs this week",
            "PR for deadlift", "best squat", "bench press PR",
            "top lifts", "strongest exercises",
        ],
        read_handler=_exercise_prs_read,
        write_handler=None,
        category="personal",
    ))

    # ── Villain Challenge skills ──

    async def _villain_status_read(message, http_client=None):
        from app.services.villain_challenge.hero_engine import get_hero_profile
        from app.services.villain_challenge.villain_engine import get_active_challenge, get_paused_challenge
        from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
        from app.services.villain_challenge.narrative import format_hero_status

        hero_data = await get_hero_profile()
        challenge = await get_active_challenge()
        battle_status = None
        if challenge:
            battle_status = await calculate_daily_battle_probability(challenge, hero_data)
            return await format_hero_status(hero_data, challenge, battle_status)

        paused = await get_paused_challenge()
        if paused:
            status = await format_hero_status(hero_data, None, None)
            return status + f"\n\n⏸ **Challenge paused** — vs. {paused['villain_name']}. Say 'resume challenge' to continue."

        return await format_hero_status(hero_data, None, None)

    async def _villain_checkin_write(message, http_client=None):
        from app.services.villain_challenge.xp_engine import award_xp
        from app.services.villain_challenge.hero_engine import get_hero_profile
        from app.services.villain_challenge.villain_engine import get_active_challenge
        from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
        from app.services.villain_challenge.scheduler import _sync_objective_progress
        from app.core.database import async_session as db_session
        from app.services.ollama_service import generate
        from sqlalchemy import text as sql_text
        import re

        msg = message.lower()

        # Handle pause/resume commands
        if re.search(r'\b(pause|vacation|hold|freeze)\b', msg):
            from app.services.villain_challenge.villain_engine import (
                pause_challenge, get_paused_challenge, schedule_pause, get_scheduled_pauses,
            )
            from dateutil import parser as date_parser
            from datetime import date as dt_date

            # Check for date range: "pause from June 15 to June 22" or "pause June 15 - 22"
            date_range = re.search(
                r'(?:from|starting|begin)\s+(.+?)\s+(?:to|through|until|thru|-)\s+(.+?)(?:\s*$|\s+for|\s+because)',
                msg
            ) or re.search(
                r'(\w+\s+\d{1,2})\s*(?:to|-|through|thru)\s*(\w+\s+\d{1,2})',
                msg
            )

            if date_range:
                try:
                    start = date_parser.parse(date_range.group(1), fuzzy=True).date()
                    end = date_parser.parse(date_range.group(2), fuzzy=True).date()
                    if start < dt_date.today():
                        start = dt_date.today()
                    if end <= start:
                        return "End date must be after start date."
                    reason_match = re.search(r'(?:for|because|reason:?)\s+(.+)', msg)
                    reason = reason_match.group(1).strip() if reason_match else "vacation"
                    result = await schedule_pause(start, end, reason)
                    days = (end - start).days
                    if start == dt_date.today():
                        # Also pause immediately
                        await pause_challenge()
                        return (f"⏸ **Challenge paused now** through **{end.strftime('%b %d')}** ({days} days).\n"
                                f"Auto-resumes on {end.strftime('%b %d')}. Reason: {reason}")
                    return (f"📅 **Pause scheduled:** {start.strftime('%b %d')} → {end.strftime('%b %d')} ({days} days)\n"
                            f"Challenge will auto-pause and auto-resume. Reason: {reason}")
                except (ValueError, TypeError):
                    pass  # Fall through to immediate pause

            # Immediate pause (no dates)
            paused = await get_paused_challenge()
            if paused:
                scheduled = await get_scheduled_pauses()
                if scheduled:
                    sched_info = ", ".join(f"{s['pause_start']} to {s['pause_end']}" for s in scheduled)
                    return f"Challenge is already paused (vs. **{paused['villain_name']}**).\nScheduled pauses: {sched_info}\nSay 'resume challenge' when you're back."
                return f"Challenge is already paused (vs. **{paused['villain_name']}**). Say 'resume challenge' when you're back."
            result = await pause_challenge()
            if result:
                return f"⏸ **Challenge paused.** Your battle against **{result['villain_name']}** is on hold.\nNo objectives will be tracked and no new villains assigned until you resume.\nSay 'resume challenge' when you're back."
            return "No active challenge to pause."

        if re.search(r'\b(resume|unpause|back|restart)\b.*\b(challenge|battle|villain|fight)\b', msg) or \
           re.search(r'\b(challenge|battle|villain|fight)\b.*\b(resume|unpause|back|restart)\b', msg):
            from app.services.villain_challenge.villain_engine import resume_challenge, get_paused_challenge
            paused = await get_paused_challenge()
            if not paused:
                return "No paused challenge to resume. You're good to go."
            result = await resume_challenge()
            if result:
                return f"▶ **Challenge resumed!** Your battle against **{result['villain_name']}** is back on.\nObjectives are being tracked again. Let's go."
            return "Failed to resume challenge."

        # Parse weight (e.g. "weight 183", "183 lbs", "weigh 183.5")
        weight = None
        w_match = re.search(r'(?:weight|weigh|wt)\s*([\d.]+)', msg) or re.search(r'([\d.]+)\s*(?:lbs?|pounds?)', msg)
        if w_match:
            weight = float(w_match.group(1))

        # Parse body fat (e.g. "bf 12", "body fat 12.5", "12% bf", "12.5%")
        bf = None
        bf_match = re.search(r'(?:bf|body\s*fat|bodyfat)\s*([\d.]+)', msg) or re.search(r'([\d.]+)\s*%\s*(?:bf|body\s*fat|bodyfat)?', msg)
        if bf_match:
            bf = float(bf_match.group(1))

        # Parse mobility (flexible: "mobility done", "completed mobility", "did mobility", "mobility work", etc.)
        mobility = bool(re.search(r'mobility|stretch|foam\s*roll', msg) and
                        re.search(r'done|yes|did|complete|finished|work|session', msg))

        # Parse nutrition adherence (e.g. "nutrition 80", "adherence 90", "met 70% nutrition", "70% of nutrition")
        nutrition = None
        n_match = (re.search(r'(?:nutrition|adherence|diet)\s*([\d]+)', msg)
                   or re.search(r'([\d]+)\s*%?\s*(?:of\s+)?(?:nutrition|adherence|diet)', msg)
                   or re.search(r'met\s+([\d]+)\s*%', msg))
        if n_match:
            nutrition = int(n_match.group(1))

        async with db_session() as session:
            await session.execute(sql_text("""
                INSERT INTO daily_checkins (checkin_date, body_weight, body_fat_pct, mobility_done, nutrition_adherence)
                VALUES (CURRENT_DATE, :weight, :bf, CAST(:mobility AS boolean), COALESCE(CAST(:nutrition AS integer), 0))
                ON CONFLICT (checkin_date) DO UPDATE SET
                    body_weight = COALESCE(:weight, daily_checkins.body_weight),
                    body_fat_pct = COALESCE(:bf, daily_checkins.body_fat_pct),
                    mobility_done = CASE WHEN CAST(:mobility AS boolean) = TRUE THEN TRUE ELSE daily_checkins.mobility_done END,
                    nutrition_adherence = CASE WHEN :nutrition IS NOT NULL THEN CAST(:nutrition AS integer) ELSE daily_checkins.nutrition_adherence END
            """), {"weight": weight, "bf": bf, "mobility": mobility, "nutrition": nutrition})
            await session.commit()

        xp = await award_xp(25, "daily_checkin", category="checkin")

        # Sync objective progress after check-in
        try:
            await _sync_objective_progress()
        except Exception:
            pass

        # Build check-in summary
        logged = []
        if weight:
            logged.append(f"Weight: {weight} lbs")
        if bf:
            logged.append(f"Body fat: {bf}%")
        if mobility:
            logged.append("Mobility: done")
        if nutrition:
            logged.append(f"Nutrition: {nutrition}%")
        checkin_summary = ", ".join(logged) if logged else "Daily check-in"

        # Get battle context for narrative
        hero_data = await get_hero_profile()
        challenge = await get_active_challenge()

        if not challenge:
            return f"**Check-in logged!** +{xp['awarded']} XP (Total: {xp['total_xp']})\n{checkin_summary}\n\nNo active villain challenge — a new one starts Monday."

        battle_status = await calculate_daily_battle_probability(challenge, hero_data)
        status = battle_status.get("status", "Contested")
        days_left = battle_status.get("days_remaining", 0)
        completed = battle_status.get("completed_objectives", 0)
        total = battle_status.get("total_objectives", 0)
        villain = challenge.get("villain_name", "Unknown")
        actions = battle_status.get("recommended_actions", [])
        actions_text = "\n".join(f"  - {a}" for a in actions[:3])

        # Build objective progress lines
        obj_lines = []
        for o in challenge.get("objectives", []):
            mark = "x" if o.get("completed") else " "
            obj_lines.append(f"[{mark}] {o['description']} ({o.get('current_value', 0):.0f}/{o['target_value']:.0f})")
        obj_text = "\n".join(obj_lines)

        prompt = f"""You are a tactical X-Men mission handler giving a battle status update after the hero just checked in.

CHECK-IN DATA:
{checkin_summary}
XP Earned: +{xp['awarded']} (Total: {xp['total_xp']})

BATTLE SITUATION:
- Villain: {villain}
- Status: {status}
- Days Remaining: {days_left}
- Objectives: {completed}/{total} completed
{obj_text}
- Recommended Actions:
{actions_text}

HERO PROFILE:
- Archetype: {hero_data.get('archetype', {}).get('name', 'Recruit')}
- Tier: {hero_data.get('tier', 'Street Level')}
- HCI: {hero_data.get('hci', 0):.1f}
- Level: {hero_data.get('level', 1)}

Write a brief response (under 120 words) that:
1. Acknowledges the check-in data (weight/bf/mobility/nutrition)
2. Connects it to the current battle against {villain}
3. References the battle status ({status}) with appropriate urgency
4. Mentions 1-2 specific next actions from the recommended list
5. Use a tactical, motivating tone — like a war room briefing
Do NOT use hashtags or emoji. Do NOT be generic."""

        narrative = await generate(
            prompt=prompt,
            system_prompt="You are a tactical X-Men mission handler. Brief, punchy battle updates only.",
            model=None,
        )
        return narrative

    register_skill(Skill(
        id="villain_challenge",
        name="Villain Challenge",
        description="X-Men themed fitness challenge system — hero status, HCI score, current villain battle, daily check-ins, battle probability, XP and power surges",
        examples=[
            "hero status", "villain challenge", "what's my HCI",
            "who am I fighting this week", "battle status",
            "check in", "daily check-in", "how's the battle going",
            "am I winning", "villain update", "my power level",
            "XP status", "power surges", "nemesis list",
            "check in weight 185 bf 12", "weight 190 body fat 15",
            "completed mobility work", "nutrition 80",
            "I did mobility and met 70% nutrition",
            "log weight 200 lbs", "check in mobility done",
            "pause challenge", "pause villain", "going on vacation",
            "resume challenge", "resume battle",
        ],
        read_handler=_villain_status_read,
        write_handler=_villain_checkin_write,
        category="personal",
    ))

    async def _villain_battle_read(message, http_client=None):
        from app.services.villain_challenge.villain_engine import get_active_challenge
        from app.services.villain_challenge.hero_engine import get_hero_profile
        from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
        from app.services.villain_challenge.narrative import generate_daily_update

        challenge = await get_active_challenge()
        if not challenge:
            return "No active villain challenge. A new one starts Monday."

        hero_data = await get_hero_profile()
        battle_status = await calculate_daily_battle_probability(challenge, hero_data)
        tone = challenge.get("narrative_tone", "shield_tactical")
        narrative = await generate_daily_update(battle_status, challenge, hero_data, tone=tone)
        return narrative

    register_skill(Skill(
        id="villain_battle",
        name="Villain Battle Status",
        description="Get a dramatic narrative battle update for the current X-Men villain challenge — how the fight is going, what to do next",
        examples=[
            "battle update", "how's the fight", "give me a battle report",
            "am I beating the villain", "mission status", "tactical update",
        ],
        read_handler=_villain_battle_read,
        write_handler=None,
        category="personal",
    ))

    async def _villain_history_read(message, http_client=None):
        from app.services.villain_challenge.battle_system import get_battle_history
        from app.services.villain_challenge.xp_engine import get_xp_summary

        battles = await get_battle_history(limit=5)
        xp = await get_xp_summary()

        lines = [f"**Level {xp['level']}** — {xp['title']} | {xp['total_xp']} XP"]
        for b in battles:
            lines.append(f"- {b['villain_name']}: {b['outcome']} (Score: {b['battle_score']:.0f})")
        if not battles:
            lines.append("No battles fought yet.")
        return "\n".join(lines)

    register_skill(Skill(
        id="villain_history",
        name="Battle History",
        description="View past villain battles, win/loss record, and XP progression",
        examples=[
            "battle history", "past fights", "who have I beaten",
            "win loss record", "villain defeats",
        ],
        read_handler=_villain_history_read,
        write_handler=None,
        category="personal",
    ))

    # ── Idea Factory ──
    async def _idea_factory_read(message, http_client=None):
        from app.services.idea_factory_service import list_ideas, get_idea
        import re
        id_match = re.search(r'\bidea\s+(\d+)\b', message.lower())
        if id_match:
            idea = await get_idea(int(id_match.group(1)))
            if idea:
                lines = [
                    f"**#{idea['id']}: {idea['title']}** ({idea['stage']})",
                    f"_{idea['description']}_" if idea['description'] else "",
                    f"Tags: {', '.join(idea['tags'])}" if idea['tags'] else "",
                    f"Created: {idea['created_at'].strftime('%Y-%m-%d')}",
                ]
                if idea.get('challenge_output'):
                    lines.append(f"\n**Last Challenge:**\n{idea['challenge_output']}")
                return "\n".join(l for l in lines if l)
            return "Idea not found."
        ideas = await list_ideas()
        if not ideas:
            return "No ideas captured yet. Drop one with 'idea: your concept here'."
        lines = ["**Idea Factory** — Active Ideas:\n"]
        for i in ideas:
            age = (i['updated_at'] - i['created_at']).days
            lines.append(f"  #{i['id']} [{i['stage']}] {i['title']} ({age}d old)")
        return "\n".join(lines)

    async def _idea_factory_write(message, http_client=None):
        import re
        from app.services.idea_factory_service import (
            parse_idea_command, capture_idea, challenge_idea,
            list_ideas, advance_idea, kill_idea, generate_retrospective,
        )
        cmd = parse_idea_command(message)
        action = cmd["action"]
        if action == "capture":
            text = cmd["text"]
            parts = re.split(r'[.\n]', text, maxsplit=1)
            title = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            idea = await capture_idea(title, desc)
            return f"💡 Captured: **#{idea['id']} — {idea['title']}** (stage: spark)\nChallenge it with 'challenge: {title}'"
        elif action == "challenge":
            result = await challenge_idea(cmd["text"])
            return f"**Challenge Results:**\n\n{result}"
        elif action == "list":
            ideas = await list_ideas()
            if not ideas:
                return "No active ideas. Capture one with 'idea: your concept here'."
            lines = ["**Idea Pipeline:**\n"]
            for i in ideas:
                lines.append(f"  #{i['id']} [{i['stage']}] {i['title']}")
            return "\n".join(lines)
        elif action == "advance":
            idea = await advance_idea(cmd["id"], cmd["stage"])
            if idea:
                return f"✅ **#{idea['id']} — {idea['title']}** moved to **{idea['stage']}**"
            return "Failed to advance — check the idea ID and stage name."
        elif action == "kill":
            idea = await kill_idea(cmd["id"])
            if idea:
                return f"💀 **#{idea['id']} — {idea['title']}** killed."
            return "Idea not found."
        elif action == "retrospective":
            retro = await generate_retrospective()
            return f"**Idea Retrospective:**\n\n{retro}"
        return "Didn't understand that idea command. Try: 'idea: <concept>', 'challenge: <idea>', 'list ideas', 'advance idea 3 to exploring', 'kill idea 3'"

    register_skill(Skill(
        id="idea_factory",
        name="Idea Factory",
        description="Capture, challenge, evolve, and review product/project ideas — brainstorm, stress-test concepts, track idea pipeline",
        examples=[
            "idea: fitness app for couples",
            "new idea: automated meal prep scheduling",
            "challenge: what if I built a SaaS for personal trainers",
            "list my ideas", "show ideas",
            "advance idea 2 to validating",
            "kill idea 5",
            "idea retrospective",
        ],
        read_handler=_idea_factory_read,
        write_handler=_idea_factory_write,
        category="personal",
    ))

    # ── Receipts / Tax Tracking ──
    async def _receipts_read(message, http_client=None):
        from app.services.receipt_service import get_receipts, get_tax_summary
        import re
        lower = message.lower()
        if "summary" in lower or "total" in lower or "how much" in lower:
            year_match = re.search(r'20\d{2}', message)
            year = int(year_match.group()) if year_match else None
            summary = await get_tax_summary(year)
            if not summary["total_receipts"]:
                return f"No receipts for {summary['tax_year']}. Upload some via the UI or the /skills/receipts/upload endpoint."
            lines = [f"**Tax Receipt Summary — {summary['tax_year']}**\n"]
            lines.append(f"Total: **${summary['grand_total']:,.2f}** across {summary['total_receipts']} receipts\n")
            for cat in summary["by_category"]:
                name = cat["category"].replace("_", " ").title()
                lines.append(f"  {name}: ${float(cat['total'] or 0):,.2f} ({cat['count']} receipts)")
            return "\n".join(lines)
        category = None
        for cat in ["business_expense", "office_supplies", "software", "travel",
                    "meals", "professional_development", "equipment", "home_office",
                    "medical", "charitable", "vehicle", "insurance", "utilities"]:
            if cat.replace("_", " ") in lower or cat in lower:
                category = cat if cat != "software" else "software_subscriptions"
                if cat == "meals":
                    category = "meals_entertainment"
                break
        year_match = re.search(r'20\d{2}', message)
        year = int(year_match.group()) if year_match else None
        vendor = None
        vendor_match = re.search(r'(?:from|at|for)\s+([A-Z][\w\s]+)', message)
        if vendor_match:
            vendor = vendor_match.group(1).strip()
        receipts = await get_receipts(tax_year=year, category=category, vendor=vendor)
        if not receipts:
            filters = []
            if year:
                filters.append(str(year))
            if category:
                filters.append(category)
            if vendor:
                filters.append(vendor)
            return f"No receipts found{' for ' + ', '.join(filters) if filters else ''}."
        lines = [f"**Receipts** ({len(receipts)} found):\n"]
        total = 0
        for r in receipts[:20]:
            amt = f"${r['amount']:,.2f}" if r.get("amount") else "?"
            dt = r["receipt_date"].strftime("%m/%d") if r.get("receipt_date") else "?"
            cat = r["category"].replace("_", " ").title() if r.get("category") else ""
            lines.append(f"  #{r['id']} {dt} — **{r['vendor'] or 'Unknown'}** — {amt} [{cat}]")
            total += float(r["amount"] or 0)
        if len(receipts) > 20:
            lines.append(f"  ...and {len(receipts) - 20} more")
        lines.append(f"\n  **Total: ${total:,.2f}**")
        return "\n".join(lines)

    register_skill(Skill(
        id="receipts",
        name="Receipts & Tax Tracking",
        description="Query uploaded receipts for tax purposes — view by year, category, vendor; get tax summaries and totals",
        examples=[
            "show my receipts", "receipts for 2026",
            "tax summary", "how much did I spend this year",
            "business expenses", "software subscriptions receipts",
            "receipts from Amazon", "travel expenses 2026",
        ],
        read_handler=_receipts_read,
        write_handler=None,
        category="personal",
    ))

    # -- Planner (Panda-style) --
    async def _planner_read(message, http_client=None):
        from app.services.planner_service import (
            get_current_plan,
            get_weekly_review,
            get_monthly_review,
            suggest_daily_priorities,
            get_daily_priorities,
            parse_planner_command,
        )
        from datetime import timedelta

        cmd = parse_planner_command(message)
        action = cmd.get("action")

        if action == "weekly_review":
            review = await get_weekly_review()
            lines = [
                f"**Weekly Review** ({review['week_start']} to {review['week_end']})",
                f"Weekly goals: {review['weekly_done']}/{review['weekly_total']} complete",
                f"Daily priorities: {review['priorities_done']}/{review['priorities_total']} complete",
            ]
            if review.get("avg_sleep") is not None:
                lines.append(f"Avg sleep performance: {review['avg_sleep']:.1f}%")
            if review.get("avg_recovery") is not None:
                lines.append(f"Avg recovery score: {review['avg_recovery']:.1f}%")

            if review["weekly_goals"]:
                lines.append("\nGoals:")
                for g in review["weekly_goals"]:
                    mark = "[x]" if g["completed"] else "[ ]"
                    lines.append(f"  {mark} W{g['slot']}: {g['title']}")
            return "\n".join(lines)

        if action == "monthly_review":
            review = await get_monthly_review()
            lines = [
                f"**Monthly Review** ({review['month_start']} to {review['month_end']})",
                f"Monthly goals: {review['monthly_done']}/{review['monthly_total']} complete",
                f"Weekly goals completed this month: {review['weekly_done']}/{review['weekly_total']}",
            ]
            if review["monthly_goals"]:
                lines.append("\nGoals:")
                for g in review["monthly_goals"]:
                    mark = "[x]" if g["completed"] else "[ ]"
                    lines.append(f"  {mark} M{g['slot']}: {g['title']}")
            return "\n".join(lines)

        if action == "recommend_priorities":
            recs = await suggest_daily_priorities(limit=3)
            lines = [
                "**Recommended Daily Priorities**",
                f"Open goals: weekly {recs['weekly_open']}, monthly {recs['monthly_open']}",
            ]
            for i, r in enumerate(recs["recommendations"], 1):
                lines.append(f"  {i}. {r['text']}")
                lines.append(f"     Why: {r['rationale']}")
            return "\n".join(lines)

        if action == "show_day_goals":
            from datetime import date
            target = date.today() + timedelta(days=1) if cmd.get("day") == "tomorrow" else date.today()
            goals = await get_daily_priorities(target)
            heading = "Tomorrow's Goals" if cmd.get("day") == "tomorrow" else "Today's Goals"
            if not goals:
                if cmd.get("day") == "tomorrow":
                    return "No goals saved for tomorrow yet. Add them with: planner goals for tomorrow: goal 1, goal 2, goal 3"
                recs = await suggest_daily_priorities(limit=3)
                lines = [f"**{heading}:**"]
                for i, r in enumerate(recs["recommendations"], 1):
                    lines.append(f"{i}. **{r['text']}**")
                return "\n".join(lines)
            lines = [f"**{heading} in Planner:**"]
            for g in goals:
                mark = "✅" if g.get("completed") else "❌"
                lines.append(f"{g['slot']}. {g['title']} {mark}")
            return "\n".join(lines)

        plan = await get_current_plan()
        lines = [
            "**Planner Dashboard**",
            f"Month: {plan['month_start']}",
            f"Week:  {plan['week_start']} to {plan['week_end']}",
            f"Today: {plan['today']}",
            "",
            "**Monthly Goals**",
        ]
        if plan["monthly"]:
            for g in plan["monthly"]:
                mark = "[x]" if g["completed"] else "[ ]"
                lines.append(f"  {mark} M{g['slot']}: {g['title']}")
        else:
            lines.append("  No monthly goals yet. Add one with: monthly goal: <text>")

        lines.append("\n**Weekly Goals**")
        if plan["weekly"]:
            for g in plan["weekly"]:
                mark = "[x]" if g["completed"] else "[ ]"
                lines.append(f"  {mark} W{g['slot']}: {g['title']}")
        else:
            lines.append("  No weekly goals yet. Add one with: weekly goal: <text>")

        lines.append("\n**Top 3 Today**")
        if plan["daily"]:
            for d in plan["daily"]:
                mark = "[x]" if d["completed"] else "[ ]"
                lines.append(f"  {mark} P{d['slot']}: {d['title']}")
        else:
            lines.append("  No daily priorities yet. Add one with: today priority: <text>")

        recs = await suggest_daily_priorities(limit=3)
        lines.append("\n**Suggested Priorities**")
        for i, r in enumerate(recs["recommendations"], 1):
            lines.append(f"  {i}. {r['text']}")

        lines.append("\nCommands: monthly goal, weekly goal, today priority, planner goals for tomorrow: a, b, c, show tomorrow's goals, recommend priorities, complete priority 1, done <title>, weekly review, monthly review")
        return "\n".join(lines)

    async def _planner_write(message, http_client=None):
        from app.services.planner_service import (
            parse_planner_command,
            parse_goal_items,
            set_monthly_goal,
            set_weekly_goal,
            set_weekly_goals_batch,
            set_daily_priority,
            set_daily_priority_for_date,
            replace_daily_priorities_for_date,
            complete_daily_priority,
            complete_weekly_goal,
            complete_monthly_goal,
            complete_item_by_text,
            suggest_daily_priorities,
            get_current_plan,
            get_weekly_review,
            get_monthly_review,
        )
        from datetime import date, timedelta

        cmd = parse_planner_command(message)
        action = cmd.get("action")

        if action == "set_monthly":
            result = await set_monthly_goal(cmd["text"], slot=cmd.get("slot"))
            if result.get("error"):
                return result["error"]
            return f"Saved monthly goal M{result['slot']}: {result['title']}"

        if action == "set_monthly_batch":
            lines = []
            for item in cmd.get("items", []):
                result = await set_monthly_goal(item)
                if result.get("error"):
                    lines.append(f"  ⚠ {result['error']}: {item}")
                else:
                    lines.append(f"  M{result['slot']}: {result['title']}")
            return "Saved monthly goals:\n" + "\n".join(lines) if lines else "No monthly goals saved."

        if action == "set_weekly":
            result = await set_weekly_goal(cmd["text"], slot=cmd.get("slot"))
            if result.get("error"):
                return result["error"]
            return f"Saved weekly goal W{result['slot']}: {result['title']}"

        if action == "set_weekly_batch":
            result = await set_weekly_goals_batch(cmd["items"])
            saved = result.get("saved", [])
            skipped = result.get("skipped", [])
            lines = [f"Saved {len(saved)} weekly goals:"]
            for g in saved:
                lines.append(f"  W{g['slot']}: {g['title']}")
            if skipped:
                lines.append(f"Could not save (slots full): {', '.join(skipped)}")
            return "\n".join(lines)

        if action == "set_daily":
            result = await set_daily_priority(cmd["text"], slot=cmd.get("slot"))
            if result.get("error"):
                return result["error"]
            return f"Saved daily priority P{result['slot']}: {result['title']}"

        if action == "set_daily_for_day":
            target = date.today() + timedelta(days=1) if cmd.get("day") == "tomorrow" else date.today()
            result = await set_daily_priority_for_date(cmd["text"], for_date=target, slot=cmd.get("slot"))
            if result.get("error"):
                return result["error"]
            day_label = "tomorrow" if cmd.get("day") == "tomorrow" else "today"
            return f"Saved {day_label} priority P{result['slot']}: {result['title']}"

        if action == "set_daily_batch":
            target = date.today() + timedelta(days=1) if cmd.get("day") == "tomorrow" else date.today()
            items = parse_goal_items(cmd.get("text", ""))
            if not items:
                return "I didn't find any goals in that message. Try: planner goals for tomorrow: goal 1, goal 2, goal 3"
            result = await replace_daily_priorities_for_date(items, for_date=target)
            day_label = "tomorrow" if cmd.get("day") == "tomorrow" else "today"
            lines = [f"Saved {result['saved']} goals for {day_label}."]
            for item in result.get("items", []):
                lines.append(f"  P{item['slot']}: {item['title']}")
            if result.get("truncated"):
                lines.append(f"Note: kept top 3 only ({result['truncated']} extra omitted).")
            return "\n".join(lines)

        if action == "complete_daily":
            ok = await complete_daily_priority(cmd["slot"])
            if not ok:
                return f"Could not complete priority {cmd['slot']}. Add it first with: today priority {cmd['slot']}: <text>"
            return f"Marked daily priority P{cmd['slot']} complete."

        if action == "complete_weekly":
            ok = await complete_weekly_goal(cmd["slot"])
            if not ok:
                return f"Could not complete weekly goal {cmd['slot']}. Add it first with: weekly goal {cmd['slot']}: <text>"
            return f"Marked weekly goal W{cmd['slot']} complete."

        if action == "complete_monthly":
            ok = await complete_monthly_goal(cmd["slot"])
            if not ok:
                return f"Could not complete monthly goal {cmd['slot']}. Add it first with: monthly goal {cmd['slot']}: <text>"
            return f"Marked monthly goal M{cmd['slot']} complete."

        if action == "complete_by_text":
            result = await complete_item_by_text(cmd.get("text", ""))
            if not result.get("matched"):
                return "I couldn't match that to today's planner items. Try 'show planner' to see exact names, or use a slot number."
            if result.get("already_completed"):
                return (
                    f"Already complete: {result['kind']} {result['slot']} — {result['title']}"
                )
            prefix = {"daily": "P", "weekly": "W", "monthly": "M"}.get(result["kind"], "")
            return f"Marked complete: {prefix}{result['slot']} — {result['title']}"

        if action == "weekly_review":
            review = await get_weekly_review()
            return (
                f"Weekly review: goals {review['weekly_done']}/{review['weekly_total']}, "
                f"priorities {review['priorities_done']}/{review['priorities_total']}."
            )

        if action == "monthly_review":
            review = await get_monthly_review()
            return (
                f"Monthly review: goals {review['monthly_done']}/{review['monthly_total']}, "
                f"weekly goals complete {review['weekly_done']}/{review['weekly_total']}."
            )

        if action == "recommend_priorities":
            recs = await suggest_daily_priorities(limit=3)
            lines = ["Recommended priorities for today:"]
            for i, r in enumerate(recs["recommendations"], 1):
                lines.append(f"{i}. {r['text']} ({r['rationale']})")
            return "\n".join(lines)

        plan = await get_current_plan()
        return (
            f"Planner ready. Monthly goals: {len(plan['monthly'])}, "
            f"weekly goals: {len(plan['weekly'])}, today priorities: {len(plan['daily'])}."
        )

    register_skill(Skill(
        id="planner",
        name="Planner",
        description="Panda-style planning: set monthly goals, weekly goals, daily top 3 priorities, and run weekly/monthly reviews.",
        examples=[
            "monthly goal: launch receipts workflow",
            "weekly goal: complete 4 training sessions",
            "today priority: ship planner skill",
            "complete priority 1",
            "done ship planner mvp",
            "recommend priorities",
            "weekly review",
            "monthly review",
            "show planner",
        ],
        read_handler=_planner_read,
        write_handler=_planner_write,
        category="personal",
    ))

    # ── Music / Sonos ──
    async def _music_read(message, http_client=None):
        from app.services.music_service import get_music_status
        return await get_music_status(http_client)

    async def _music_write(message, http_client=None):
        from app.services.music_service import handle_music_command
        return await handle_music_command(message, http_client)

    register_skill(Skill(
        id="music",
        name="Music & Sonos",
        description="Play music on Sonos speakers — play/pause/skip, set volume, search Spotify playlists/songs by name, control by room",
        examples=[
            "play workout playlist on living room",
            "pause the music", "skip this song",
            "play jazz in the bedroom", "volume 40",
            "what's playing right now",
        ],
        read_handler=_music_read,
        write_handler=_music_write,
        category="personal",
    ))

    logger.info("all_skills_registered", extra={"count": len(_REGISTRY)})


async def register_process_skills():
    """Auto-register process definitions as skills so they're accessible via chat."""
    from app.services.process_engine import list_process_definitions, start_process

    processes = await list_process_definitions()
    for proc in processes:
        proc_id = proc["process_id"]
        proc_name = proc["name"]
        proc_desc = proc.get("description", proc_name)
        steps = proc.get("steps", [])
        step_names = [s.get("name", s.get("id", "?")) for s in steps]
        trigger = proc.get("trigger_config", {}).get("type", "manual")

        # Build examples from the process name words
        name_words = proc_name.lower()
        examples = [
            f"run the {name_words}",
            f"what is the {name_words}",
            f"execute {name_words}",
        ]

        # Create closures for the handlers
        def _make_read(pid, pname, pdesc, psteps, ptrigger):
            async def _read(message, http_client=None):
                schedule_info = f"Scheduled: {ptrigger}" if ptrigger != "manual" else "Trigger: manual (on-demand)"
                step_list = "\n".join(f"  {i+1}. {name}" for i, name in enumerate(psteps))
                return (
                    f"**{pname}**\n\n"
                    f"{pdesc}\n\n"
                    f"{schedule_info}\n\n"
                    f"**Steps ({len(psteps)}):**\n{step_list}\n\n"
                    f"Would you like me to run it now?"
                )
            return _read

        def _make_write(pid, pname):
            async def _write(message, http_client=None):
                try:
                    execution = await start_process(pid, params={}, http_client=http_client)
                    exec_id = execution.get("execution_id", "unknown")
                    status = execution.get("status", "unknown")
                    if status == "completed":
                        return (
                            f"**{pname}** completed successfully.\n\n"
                            f"Execution ID: `{exec_id}`\n"
                            f"Results stored in memory and emailed."
                        )
                    elif status == "waiting_for_gate":
                        return (
                            f"**{pname}** paused at a review gate.\n"
                            f"Execution ID: `{exec_id}`"
                        )
                    elif status in ("error", "failed"):
                        return f"**{pname}** failed: {execution.get('error', 'Unknown error')}"
                    else:
                        return f"**{pname}** started (status: {status}). Execution ID: `{exec_id}`"
                except Exception as e:
                    return f"Failed to run **{pname}**: {e}"
            return _write

        skill_id = f"process:{proc_id}"
        register_skill(Skill(
            id=skill_id,
            name=proc_name,
            description=f"{proc_desc} (automated process — can be run on demand)",
            examples=examples,
            read_handler=_make_read(proc_id, proc_name, proc_desc, step_names, trigger),
            write_handler=_make_write(proc_id, proc_name),
            category="process",
        ))

    logger.info("process_skills_registered", extra={"count": len(processes)})
