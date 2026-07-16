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

    # Check-in messages → villain_challenge
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'weight|lbs|bf|body\s*fat|mobility|nutrition|adherence', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'\d', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    if re.search(r'(completed|did|done)\s*(mobility|stretch)', lower) or re.search(r'(mobility\s*(done|complete)|met\s+\d+%\s*(nutrition|diet))', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}

    # Idea factory commands
    if re.match(r'(?:new\s+)?idea[:\s]', lower) or re.match(r'challenge[:\s]', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.search(r'\b(list|show|my)\s+ideas?\b', lower) or re.search(r'\bidea\s+retro', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.match(r'(advance|kill)\s+idea', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}

    # Calendar
    if re.search(r'\b(calendar|schedule|agenda|appointment|meeting)\b', lower):
        action = "execute" if re.search(r'\b(add|schedule|create|move|cancel|reschedule)\b', lower) else "query"
        return {"action": action, "skill": "calendar", "role": "family_activity_coordinator", "domain": "family"}

    # Weather
    if re.search(r'\b(weather|temperature|forecast|rain|snow|sunny)\b', lower):
        return {"action": "query", "skill": "weather", "role": "polymath_in_training", "domain": "personal"}

    # Music / Sonos (before workouts — "play workout playlist" should route to music)
    if re.search(r'\b(play|pause|resume|skip|next\s+song|previous\s+song|volume|playlist|sonos|music|spotify)\b', lower):
        action = "query" if re.search(r'\b(what.?s playing|status|now playing)\b', lower) else "execute"
        return {"action": action, "skill": "music", "role": "polymath_in_training", "domain": "personal"}

    # Meals / recipes
    if re.search(r'\b(meal|recipe|dinner|lunch|breakfast|cook|food|eat)\b', lower):
        action = "execute" if re.search(r'\b(plan|suggest|make|generate)\b', lower) else "query"
        return {"action": action, "skill": "meal_planner", "role": "family_chef", "domain": "family"}

    # Workouts / fitness
    if re.search(r'\b(workout|exercise|training|gym|lift|run|cardio|peloton|tonal|whoop|recovery|strain|hrv)\b', lower):
        return {"action": "query", "skill": "fitness", "role": "fitness_longevity_optimist", "domain": "personal"}

    # Receipts
    if re.search(r'\b(receipt|expense|purchase|spent|cost)\b', lower):
        action = "execute" if re.search(r'\b(scan|upload|add|log)\b', lower) else "query"
        return {"action": action, "skill": "receipts", "role": "polymath_in_training", "domain": "personal"}

    # Greetings / simple chat
    if re.match(r'^(hi|hello|hey|good\s+(morning|afternoon|evening)|what\'?s up|howdy|yo)\b', lower):
        return {"action": "conversation", "skill": "none", "role": "polymath_in_training", "domain": "personal"}

    # Briefing
    if re.search(r'\b(brief|briefing|morning\s+update|daily\s+summary)\b', lower):
        return {"action": "query", "skill": "briefing", "role": "cybersecurity_executive", "domain": "professional"}

    # Web search
    if re.search(r'\b(search|google|look\s+up|find\s+out)\b', lower) and not re.search(r'\bidea', lower):
        return {"action": "query", "skill": "web_search", "role": "polymath_in_training", "domain": "personal"}

    # Medical
    if re.search(r'\b(medical|doctor|medication|prescription|health\s*record|blood\s*pressure|lab\s*result)\b', lower):
        return {"action": "query", "skill": "medical", "role": "fitness_longevity_optimist", "domain": "personal"}

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
