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

    # Fast pre-classifier: check-in messages always route to villain_challenge
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'weight|lbs|bf|body\s*fat|mobility|nutrition|adherence', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    # Also match standalone check-in phrases
    if re.search(r'\bcheck\s*in\b', lower) and re.search(r'\d', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}
    # "completed mobility" / "did mobility" / "nutrition 80" without "check in"
    if re.search(r'(completed|did|done)\s*(mobility|stretch)', lower) or re.search(r'(mobility\s*(done|complete)|met\s+\d+%\s*(nutrition|diet))', lower):
        return {"action": "execute", "skill": "villain_challenge", "role": "fitness_longevity_optimist", "domain": "personal"}

    # Fast pre-classifier: idea factory commands
    if re.match(r'(?:new\s+)?idea[:\s]', lower) or re.match(r'challenge[:\s]', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.search(r'\b(list|show|my)\s+ideas?\b', lower) or re.search(r'\bidea\s+retro', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}
    if re.match(r'(advance|kill)\s+idea', lower):
        return {"action": "execute", "skill": "idea_factory", "role": "polymath_in_training", "domain": "personal"}

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
