"""LLM-based intent classification and role inference.

Uses native tool calling for structured output — eliminates JSON parse failures.
Dynamic skill registry ensures new skills are automatically accessible.
"""

import logging

import httpx

from app.services.ollama_service import generate_tool_call

logger = logging.getLogger("pai.llm_intent")

_cached_classifier_prompt: str | None = None
_cached_tool_def: list[dict] | None = None

_VALID_ROLES = {
    "cybersecurity_executive", "ai_cybersecurity_strategist",
    "ai_governance_practitioner", "educator_scholar",
    "solutions_architect", "proposal_strategist",
    "fitness_longevity_optimist", "aesthetics_focused_builder",
    "family_chef", "family_activity_coordinator", "parent",
    "polymath_in_training",
}
_VALID_DOMAINS = {"professional", "personal", "family", "intellectual_growth"}


def invalidate_classifier_cache() -> None:
    """Clear cached classifier prompt (called when skills change)."""
    global _cached_classifier_prompt, _cached_tool_def
    _cached_classifier_prompt = None
    _cached_tool_def = None


def _build_tool_def() -> list[dict]:
    """Build the tool definition for intent classification."""
    global _cached_tool_def
    if _cached_tool_def is not None:
        return _cached_tool_def

    from app.services.skill_registry import list_skills

    skills = list_skills()
    skill_enum = [s.id for s in skills] + ["none"]

    _cached_tool_def = [{
        "type": "function",
        "function": {
            "name": "classify_intent",
            "description": "Classify the user's message into action, skill, role, and domain",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["query", "execute", "conversation"],
                        "description": "query=asking/viewing data, execute=creating/running/scheduling, conversation=general chat",
                    },
                    "skill": {
                        "type": "string",
                        "enum": skill_enum,
                        "description": "The skill best matching the user's request, or 'none' for general conversation",
                    },
                    "role": {
                        "type": "string",
                        "enum": list(_VALID_ROLES),
                        "description": "The persona role best suited to respond",
                    },
                    "domain": {
                        "type": "string",
                        "enum": list(_VALID_DOMAINS),
                        "description": "The domain category of the request",
                    },
                },
                "required": ["action", "skill", "role", "domain"],
            },
        },
    }]
    return _cached_tool_def


def _build_classifier_prompt() -> str:
    """Build the classifier system prompt dynamically from the skill registry."""
    global _cached_classifier_prompt
    if _cached_classifier_prompt is not None:
        return _cached_classifier_prompt

    from app.services.skill_registry import build_skill_catalog

    skill_catalog = build_skill_catalog()

    prompt = f"""\
You are an intent classifier for a Personal AI assistant. You MUST call the classify_intent tool for EVERY message. Always classify — never respond with text.

Available skills:
{skill_catalog}
  - "none" — general conversation, advice, or topics not covered above

Rules:
- IMPORTANT: "check in" messages (weight, body fat, bf, lbs, mobility done, nutrition) = execute + villain_challenge. Examples: "check in 203 lbs bf 19%", "check in weight 185 bf 12", "mobility done nutrition 80"
- "what's on my calendar" = query + calendar. "add dentist Thursday" = execute + calendar
- "run the threat intel digest" = execute + that process skill
- Viewing/checking data = query. Creating/adding/running = execute
- Default: action "conversation", skill "none"
- For greetings like "hello" or "hi" → action "conversation", skill "none", role "polymath_in_training", domain "personal"
"""
    _cached_classifier_prompt = prompt
    return prompt


async def classify_chat_intent(
    message: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Classify a chat message into action, skill, role, and domain.

    Uses native tool calling (qwen3:4b) for reliable structured output.
    Returns dict with keys: action, skill, role, domain.
    """
    import re
    lower = message.lower()

    # ── Fast pre-classifier: regex fast-paths (avoids LLM for ~60% of requests) ──
    # NOTE: Order matters — more specific patterns must come before general ones.

    # ── Villain Challenge / check-in (most specific — must be first) ──
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'weight|lbs|bf|body\s*fat|mobility|nutrition|adherence', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'\d', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'(completed|did|done)\s*(mobility|stretch)', lower) or re.search(r'(mobility\s*(done|complete)|met\s+\d+%\s*(nutrition|diet))', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\b(hero\s*status|villain\s*(challenge|update|battle|status)|hci\s*score|power\s*(level|surge)|battle\s*(status|report|update|probability)|xp\s*status|nemesis|pause\s*challenge|resume\s*challenge|pause\s*villain|resume\s*villain)\b', lower):
        return {"action": "query", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\b(battle\s*(update|report|narrative|history|log)|past\s*fights?|who\s*(have\s*i|did\s*i)\s*(beat|defeat|fight)|win.?loss|villain\s*(defeat|history)|battle\s*record)\b', lower):
        return {"action": "query", "skill": "villain_history", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\b(mission\s*status|tactical\s*update|battle\s*narrative|how.?s\s*the\s*fight|am\s*i\s*(beating|winning|losing))\b', lower):
        return {"action": "query", "skill": "villain_battle", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Idea factory ──
    if re.match(r'(?:new\s+)?idea[:\s]', lower) or re.match(r'challenge[:\s]', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.search(r'\b(list|show|my)\s+ideas?\b', lower) or re.search(r'\bidea\s+retro', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.match(r'(advance|kill)\s+idea', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}

    # ── Planner / goals / priorities ──
    if re.search(r'\b(monthly\s*goal|weekly\s*goal|daily\s*(top|priorit)|today\s*priorit|complete\s*priorit|done\s*ship|weekly\s*review|monthly\s*review|show\s*planner|recommend\s*priorit)\b', lower):
        action = "execute" if re.search(r'\b(add|set|complete|done|mark)\b', lower) else "query"
        return {"action": action, "skill": "planner", "role": "cybersecurity_executive", "domain": "professional"}
    if re.search(r'\b(planner|my\s*(goals?|priorities|top\s*3))\b', lower) and not re.search(r'\b(meal|workout|fitness)\b', lower):
        action = "execute" if re.search(r'\b(add|set|create|complete|done)\b', lower) else "query"
        return {"action": action, "skill": "planner", "role": "cybersecurity_executive", "domain": "professional"}

    # ── Calendar ──
    if re.search(r'\b(calendar|agenda|appointment|meeting)\b', lower):
        action = "execute" if re.search(r'\b(add|create|move|cancel|reschedule|remove|delete)\b', lower) else "query"
        return {"action": action, "skill": "calendar", "role": "family_activity_coordinator", "domain": "family"}
    if re.search(r'\b(schedule)\b', lower) and not re.search(r'\b(workout|training|gym|exercise|program)\b', lower):
        action = "execute" if re.search(r'\b(add|create|move|cancel|reschedule)\b', lower) else "query"
        return {"action": action, "skill": "calendar", "role": "family_activity_coordinator", "domain": "family"}

    # ── Weather ──
    if re.search(r'\b(weather|forecast|rain|snow|sunny|humidity|wind\s*speed|precipitation)\b', lower):
        return {"action": "query", "skill": "weather", "role": "polymath_in_training", "domain": "personal"}
    if re.search(r'\b(how\s*(hot|cold|warm)|is\s*it\s*(going\s*to\s*)?(rain|snow)|what.?s\s*(the\s*)?temp)\b', lower):
        return {"action": "query", "skill": "weather", "role": "polymath_in_training", "domain": "personal"}

    # ── Music / Sonos (before anything with "play") ──
    if re.search(r'\b(play|pause|resume|skip|next\s*song|previous\s*song|volume|sonos|spotify|now\s*playing|what.?s\s*playing)\b', lower):
        action = "query" if re.search(r'\b(what.?s\s*playing|status|now\s*playing)\b', lower) else "execute"
        return {"action": action, "skill": "music", "role": "polymath_in_training", "domain": "personal"}
    if re.search(r'\b(music|playlist)\b', lower) and not re.search(r'\b(workout\s*playlist|fitness\s*playlist)\b', lower):
        return {"action": "execute", "skill": "music", "role": "polymath_in_training", "domain": "personal"}

    # ── Fitness platform data (Whoop/Peloton/Tonal — specific platforms first) ──
    if re.search(r'\b(peloton|tonal|whoop)\b', lower) and not re.search(r'\b(workout|exercise|today|schedule|program|planned)\b', lower):
        return {"action": "query", "skill": "fitness_data", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Strength / PRs / Tonal specific ──
    if re.search(r'\b(strength\s*(score|progress|trend)|muscle\s*breakdown|lifting\s*volume|tonal\s*(strength|score|workout))\b', lower):
        return {"action": "query", "skill": "strength_data", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\b(pr|prs|personal\s*record|best\s*(lift|squat|deadlift|bench|press)|top\s*lifts?|strongest)\b', lower):
        return {"action": "query", "skill": "exercise_prs", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Recovery / HRV / Sleep ──
    if re.search(r'\b(recovery\s*(score|data|trend)|hrv|resting\s*heart|spo2|strain\s*(score|data)|overtraining)\b', lower):
        return {"action": "query", "skill": "recovery_data", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\b(sleep\s*(quality|performance|efficiency|trend|data|analysis|score)|how\s*(did\s*i|well\s*did\s*i)\s*sleep|am\s*i\s*sleeping)\b', lower):
        return {"action": "query", "skill": "sleep_data", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Workout history / past workouts (before current-schedule routing) ──
    if re.search(r'\b(workout\s*history|training\s*(history|log)|past\s*workouts?|workouts?\s*(this\s*week|last\s*week|log)|exercise\s*history)\b', lower):
        return {"action": "query", "skill": "workout_history", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Workout schedule / today's workout ──
    if re.search(r'\b(workout|exercise|training|gym|lift|sauna|cold\s*plunge)\b', lower):
        action = "execute" if re.search(r'\b(log|add|record|save|track|did|completed|finished)\b', lower) else "query"
        return {"action": action, "skill": "workout", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Fitness summary (general) ──
    if re.search(r'\b(fitness\s*(data|summary|overview)|cardio|fitness\s*trends?|am\s*i\s*(fit|over\s*training))\b', lower):
        return {"action": "query", "skill": "fitness_data", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Tonight's dinner ──
    if re.search(r"\b(tonight.?s\s*(dinner|meal)|what.?s\s*for\s*(dinner|supper)\s*tonight)\b", lower):
        return {"action": "query", "skill": "tonights_dinner", "role": "family_chef", "domain": "family"}

    # ── Grocery ──
    if re.search(r'\b(grocery|groceries|shopping\s*list|what\s*(do\s*i\s*need|to\s*buy)|store\s*list)\b', lower):
        return {"action": "query", "skill": "grocery", "role": "family_chef", "domain": "family"}

    # ── Meal rating/feedback ──
    if re.search(r'\b(rate|rating|rated|stars?|out\s*of\s*5|was\s*(terrible|great|amazing|awful|delicious))\b', lower) and re.search(r'\b(meal|dinner|lunch|breakfast|food|dish|recipe)\b', lower):
        return {"action": "execute", "skill": "meal_feedback", "role": "family_chef", "domain": "family"}
    if re.search(r'\b(meal\s*ratings?|show\s*ratings?|past\s*ratings?)\b', lower):
        return {"action": "query", "skill": "meal_feedback", "role": "family_chef", "domain": "family"}

    # ── Recipes ──
    if re.search(r'\b(recipe|how\s*do\s*i\s*(cook|make)|ingredient(s)?|step(s)?\s*to\s*(cook|make)|cooking\s*instructions?)\b', lower):
        action = "execute" if re.search(r'\b(save|add|store|create|generate)\b', lower) else "query"
        return {"action": action, "skill": "recipes", "role": "family_chef", "domain": "family"}

    # ── Meal planning (general meals without recipe specifics) ──
    if re.search(r'\b(meal\s*plan|weekly\s*meals?|what.?s\s*for\s*(dinner|lunch|breakfast)|dinner\s*this\s*week|plan\s*(dinner|meals?))\b', lower):
        action = "execute" if re.search(r'\b(generate|create|make|plan|suggest)\b', lower) else "query"
        return {"action": action, "skill": "meal_planning", "role": "family_chef", "domain": "family"}

    # ── Family members / preferences ──
    if re.search(r'\b(family\s*(member|preference|restriction)|add\s*family|show\s*family|dietary\s*(restriction|preference)|who\s*(is\s*in|are\s*in)\s*(the|my)\s*family)\b', lower):
        action = "execute" if re.search(r'\b(add|set|update|remove|create)\b', lower) else "query"
        return {"action": action, "skill": "family", "role": "family_activity_coordinator", "domain": "family"}

    # ── Home maintenance ──
    if re.search(r'\b(home\s*(maintenance|item|appliance|repair|task)|hvac|air\s*filter|water\s*heater|overdue\s*maintenance|maintenance\s*(schedule|overdue|due))\b', lower):
        action = "execute" if re.search(r'\b(add|log|record|changed|replaced|fixed|completed)\b', lower) else "query"
        return {"action": action, "skill": "home", "role": "polymath_in_training", "domain": "personal"}

    # ── Medical / health records ──
    if re.search(r'\b(medical|doctor|medication|prescription|health\s*record|blood\s*pressure|lab\s*result|vaccination|health\s*history)\b', lower):
        action = "execute" if re.search(r'\b(log|add|record|save|update)\b', lower) else "query"
        return {"action": action, "skill": "medical", "role": "fitness_longevity_optimist", "domain": "personal"}

    # ── Family health check ──
    if re.search(r'\b(family\s*health\s*(check|report)|health\s*check\s*(report|email)|send\s*health\s*report)\b', lower):
        return {"action": "execute", "skill": "health_check", "role": "fitness_longevity_optimist", "domain": "family"}

    # ── Receipts ──
    if re.search(r'\b(receipt|expense|purchase|spent|cost)\b', lower):
        action = "execute" if re.search(r'\b(scan|upload|add|log)\b', lower) else "query"
        return {"action": action, "skill": "receipts", "role": "polymath_in_training", "domain": "personal"}

    # ── LinkedIn ──
    if re.search(r'\b(linkedin|draft\s*(a\s*)?post|write\s*(a\s*)?post|thought\s*leadership\s*post)\b', lower):
        return {"action": "execute", "skill": "linkedin", "role": "ai_cybersecurity_strategist", "domain": "professional"}

    # ── Weekly security digest ──
    if re.search(r'\b(weekly\s*(security|intelligence|digest|briefing|roundup)|security\s*(roundup|digest|this\s*week)|what\s*happened\s*in\s*(cyber|ai|security)\s*this\s*week)\b', lower):
        return {"action": "query", "skill": "weekly_digest", "role": "cybersecurity_executive", "domain": "professional"}

    # ── Article curation (must come before web_research — more specific) ──
    if re.search(r'\b(articles?\s*(about|on|related|with|links)|curate\s*articles?|collected\s*articles?|find\s*articles?|articles?\s*(for\s*linkedin|content))\b', lower):
        action = "execute" if re.search(r'\b(curate|find|search|fetch)\b', lower) else "query"
        return {"action": action, "skill": "article_curation", "role": "ai_cybersecurity_strategist", "domain": "professional"}

    # ── Web research (general search) ──
    if re.search(r'\b(search|research|look\s*up|find\s*out|google)\b', lower) and not re.search(r'\b(idea|article|memory)\b', lower):
        return {"action": "query", "skill": "web_research", "role": "polymath_in_training", "domain": "personal"}

    # ── Document ingestion / memory save ──
    if re.search(r'\b(ingest|remember\s*this|save\s*(this\s*(to|in)?\s*)?(memory|knowledge)|store\s*this|add\s*to\s*memory|add\s*to\s*knowledge)\b', lower):
        return {"action": "execute", "skill": "document_ingestion", "role": "polymath_in_training", "domain": "professional"}
    if re.search(r'^https?://', lower):
        return {"action": "execute", "skill": "document_ingestion", "role": "polymath_in_training", "domain": "professional"}

    # ── Semantic memory search ──
    if re.search(r'\b(recall|what\s*do\s*you\s*know\s*about|search\s*(memory|knowledge)|retrieve|from\s*memory)\b', lower):
        return {"action": "query", "skill": "memory", "role": "polymath_in_training", "domain": "professional"}

    # ── Daily briefing ──
    if re.search(r'\b(brief|briefing|morning\s*update|daily\s*summary|my\s*day)\b', lower):
        return {"action": "query", "skill": "briefing", "role": "cybersecurity_executive", "domain": "professional"}

    # ── Learning / agent quality ──
    if re.search(r'\b(agent\s*(performance|quality|stats)|quality\s*stats?|learning\s*experiments?|prompt\s*overrides?|how\s*(are\s*the\s*)?agents?\s*performing)\b', lower):
        return {"action": "query", "skill": "learning", "role": "cybersecurity_executive", "domain": "professional"}

    # ── Greetings / simple chat ──
    if re.match(r'^(hi|hello|hey|good\s+(morning|afternoon|evening)|what\'?s up|howdy|yo)\b', lower):
        return {"action": "conversation", "skill": "none", "role": "polymath_in_training", "domain": "personal"}

    # ── Fall through to LLM classification for ambiguous requests ──

    try:
        system_prompt = _build_classifier_prompt()
        tools = _build_tool_def()

        result = await generate_tool_call(
            prompt=message,
            system_prompt=system_prompt,
            tools=tools,
            model="qwen3:4b",
            http_client=http_client,
        )

        if result:
            action = result.get("action", "conversation")
            skill = result.get("skill", "none")
            role = result.get("role", "cybersecurity_executive")
            domain = result.get("domain", "professional")

            # Validate
            if action not in ("query", "execute", "conversation"):
                action = "conversation"

            from app.services.skill_registry import get_skill
            if skill != "none" and not get_skill(skill):
                from app.services.skill_registry import list_skills
                for s in list_skills():
                    if skill in s.id or s.id in skill:
                        skill = s.id
                        break
                else:
                    skill = "none"

            if role not in _VALID_ROLES:
                role = "cybersecurity_executive"
            if domain not in _VALID_DOMAINS:
                domain = "professional"

            classified = {"action": action, "skill": skill, "role": role, "domain": domain}
            logger.info("chat_intent_classified", extra=classified)
            return classified

    except Exception as e:
        logger.warning("chat_intent_classification_failed", extra={"error": str(e)})

    return {
        "action": "conversation",
        "skill": "none",
        "role": "cybersecurity_executive",
        "domain": "professional",
    }


async def infer_roles_llm(
    message: str,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None]:
    """Infer primary and optional secondary role using qwen3:4b."""
    result = await classify_chat_intent(message, http_client)
    return result["role"], None
