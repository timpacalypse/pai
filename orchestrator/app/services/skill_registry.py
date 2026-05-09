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
        ex = skill.examples[0] if skill.examples else ""
        lines.append(f'  - "{skill.id}" — {skill.description} (e.g. "{ex}")')
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
        from app.services.article_curation_service import curate_articles_text
        return await curate_articles_text()

    async def _article_curation_write(message, http_client=None):
        from app.services.article_curation_service import curate_articles_text
        from app.services.ollama_service import generate as llm_generate
        # Use LLM to extract custom topic from message
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
        description="Curate top articles from the web, score them for relevance, and suggest LinkedIn thought leadership angles; supports custom topics",
        examples=["curate articles", "find articles about AI governance", "what are the top articles this week", "curate content for LinkedIn"],
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

    # ── Villain Challenge skills ──

    async def _villain_status_read(message, http_client=None):
        from app.services.villain_challenge.hero_engine import get_hero_profile
        from app.services.villain_challenge.villain_engine import get_active_challenge
        from app.services.villain_challenge.battle_system import calculate_daily_battle_probability
        from app.services.villain_challenge.narrative import format_hero_status

        hero_data = await get_hero_profile()
        challenge = await get_active_challenge()
        battle_status = None
        if challenge:
            battle_status = await calculate_daily_battle_probability(challenge, hero_data)
        return await format_hero_status(hero_data, challenge, battle_status)

    async def _villain_checkin_write(message, http_client=None):
        from app.services.villain_challenge.xp_engine import award_xp
        from app.core.database import async_session as db_session
        from sqlalchemy import text as sql_text

        # Parse basic check-in from chat message
        async with db_session() as session:
            await session.execute(sql_text("""
                INSERT INTO daily_checkins (checkin_date)
                VALUES (CURRENT_DATE)
                ON CONFLICT (checkin_date) DO NOTHING
            """))
            await session.commit()

        xp = await award_xp(25, "daily_checkin", category="checkin")
        return f"Check-in logged! +{xp['awarded']} XP (Total: {xp['total_xp']})"

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
