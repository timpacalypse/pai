"""LLM-based intent classification and role inference.

Uses a dynamic skill registry so new skills are automatically accessible
via natural language without changing routing code.

Includes a fast rule-based pre-classifier for common patterns to avoid
unnecessary LLM calls and improve accuracy.
"""

import json
import logging
import re

import httpx

from app.services.ollama_service import generate

logger = logging.getLogger("pai.llm_intent")

_cached_classifier_prompt: str | None = None


# ── Rule-based pre-classifier ────────────────────────────────

_RULE_PATTERNS: list[tuple[str, dict]] = [
    # Calendar
    (r'\b(calendar|agenda|schedule|appointment)\b', {"skill": "calendar", "domain": "family", "role": "family_activity_coordinator"}),
    (r'\bwhat\'?s (on )?(my |the )?(calendar|agenda|schedule)\b', {"action": "query", "skill": "calendar", "domain": "family", "role": "family_activity_coordinator"}),
    (r'\b(add|schedule|create) .*(appointment|meeting|event)\b', {"action": "execute", "skill": "calendar", "domain": "family", "role": "family_activity_coordinator"}),

    # Medical
    (r'\b(medical|health record|doctor|vaccination|immuniz|shot|medication|prescription|lab result|checkup|dental)\b', {"skill": "medical", "domain": "family", "role": "parent"}),
    (r'\b(when were|what are|show|list).*(shots?|vaccin|medications?|medical|health)\b', {"action": "query", "skill": "medical", "domain": "family", "role": "parent"}),

    # Workout
    (r'\b(workout|exercise|peloton|sauna|cold plunge|weights?|gym|fitness|training)\b', {"skill": "workout", "domain": "personal", "role": "fitness_longevity_optimist"}),
    (r'\bwhat\'?s (my |today\'?s? )?workout\b', {"action": "query", "skill": "workout", "domain": "personal", "role": "fitness_longevity_optimist"}),
    (r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\'?s? workout\b', {"action": "query", "skill": "workout", "domain": "personal", "role": "fitness_longevity_optimist"}),

    # Recipes
    (r'\b(recipe|recipes|cooking|ingredients?|instructions?)\b', {"skill": "recipes", "domain": "family", "role": "family_chef"}),
    (r'\b(find|search|show) .*(recipe|recipes)\b', {"action": "query", "skill": "recipes", "domain": "family", "role": "family_chef"}),

    # Meals
    (r'\b(meal plan|dinner this week|what\'?s for dinner|breakfast|lunch)\b', {"skill": "meal_planning", "domain": "family", "role": "family_chef"}),

    # Home
    (r'\b(home|maintenance|hvac|plumbing|filter|appliance|furnace|water heater)\b', {"skill": "home", "domain": "family", "role": "parent"}),
    (r'\b(overdue|maintenance due|home alert)\b', {"action": "query", "skill": "home", "domain": "family", "role": "parent"}),

    # Briefing
    (r'\b(briefing|morning brief|daily brief|what\'?s my day)\b', {"skill": "briefing", "domain": "professional", "role": "cybersecurity_executive"}),

    # Research
    (r'\b(research|search the web|find articles|latest news)\b', {"skill": "web_research", "domain": "professional", "role": "ai_cybersecurity_strategist"}),

    # Memory
    (r'\b(what do you know|search memory|recall|remember when)\b', {"action": "query", "skill": "memory", "domain": "professional", "role": "polymath_in_training"}),
]

# Action detection patterns
_QUERY_PATTERNS = re.compile(
    r'^(what|when|where|which|who|how|show|list|get|display|retrieve|tell me|do |does |did |has |have |is |are |was |were )',
    re.IGNORECASE,
)
_EXECUTE_PATTERNS = re.compile(
    r'^(add|create|save|log|record|schedule|set|run|send|generate|delete|remove|update|rate|ingest)',
    re.IGNORECASE,
)


def _rule_based_classify(message: str) -> dict | None:
    """Fast rule-based classification. Returns None if no rule matches."""
    msg = message.strip().lower()

    for pattern, result in _RULE_PATTERNS:
        if re.search(pattern, msg, re.IGNORECASE):
            classification = {
                "action": result.get("action", "query"),
                "skill": result["skill"],
                "role": result.get("role", "cybersecurity_executive"),
                "domain": result.get("domain", "professional"),
            }
            # Refine action if not already set
            if "action" not in result:
                if _EXECUTE_PATTERNS.search(message):
                    classification["action"] = "execute"
                elif _QUERY_PATTERNS.search(message):
                    classification["action"] = "query"
                elif message.strip().endswith("?"):
                    classification["action"] = "query"
            logger.info("rule_based_classification", extra=classification)
            return classification

    return None


def invalidate_classifier_cache() -> None:
    """Clear cached classifier prompt (called when skills change)."""
    global _cached_classifier_prompt
    _cached_classifier_prompt = None


def _build_classifier_prompt() -> str:
    """Build the classifier system prompt dynamically from the skill registry.
    
    Cached after first build since skills only change at startup.
    """
    global _cached_classifier_prompt
    if _cached_classifier_prompt is not None:
        return _cached_classifier_prompt

    from app.services.skill_registry import build_skill_catalog

    skill_catalog = build_skill_catalog()

    prompt = f"""\
You are an intent classifier for a Personal AI assistant. Given a user message, output JSON with these fields:

**action**: "query" (asking/viewing) | "execute" (creating/running/scheduling) | "conversation" (general chat)

**skill**: Pick the best match from this list, or "none" for general conversation:
{skill_catalog}
  - "none" — general conversation, advice, or topics not covered above

**role**: Pick one — cybersecurity_executive, ai_cybersecurity_strategist, ai_governance_practitioner, educator_scholar, solutions_architect, proposal_strategist, fitness_longevity_optimist, aesthetics_focused_builder, family_chef, family_activity_coordinator, parent, polymath_in_training

**domain**: Pick one — professional, personal, family, intellectual_growth

Respond ONLY with valid JSON: {{"action": "...", "skill": "...", "role": "...", "domain": "..."}}

Rules:
- "what's on my calendar" = query + calendar. "add dentist Thursday" = execute + calendar
- "run the threat intel digest" = execute + that process skill
- Viewing/checking data = query. Creating/adding/running = execute
- Default: action "conversation", skill "none"
"""
    _cached_classifier_prompt = prompt
    return prompt


async def classify_chat_intent(
    message: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Classify a chat message into action, skill, role, and domain.

    Uses fast rule-based matching first, falls back to LLM (qwen3:4b).
    Returns dict with keys: action, skill, role, domain.
    """
    # ── Try rule-based classification first (instant, no LLM call) ──
    rule_result = _rule_based_classify(message)
    if rule_result:
        return rule_result

    # ── Fall back to LLM classification ──
    try:
        system_prompt = _build_classifier_prompt()
        raw = await generate(
            prompt=message,
            system_prompt=system_prompt,
            model="qwen3:4b",
            http_client=http_client,
        )
        logger.info("classifier_raw_response", extra={"raw": raw[:500]})
        parsed = _parse_json(raw)
        logger.info("classifier_parsed", extra={"parsed": str(parsed)})
        if parsed:
            action = parsed.get("action", "conversation")
            skill = parsed.get("skill", "none")
            role = parsed.get("role", "cybersecurity_executive")
            domain = parsed.get("domain", "professional")

            # Validate action
            if action not in ("query", "execute", "conversation"):
                action = "conversation"

            # Validate skill exists in registry
            from app.services.skill_registry import get_skill
            if skill != "none" and not get_skill(skill):
                # Maybe partial match — try to find it
                from app.services.skill_registry import list_skills
                for s in list_skills():
                    if skill in s.id or s.id in skill:
                        skill = s.id
                        break
                else:
                    skill = "none"

            # Validate role
            valid_roles = {
                "cybersecurity_executive", "ai_cybersecurity_strategist",
                "ai_governance_practitioner", "educator_scholar",
                "solutions_architect", "proposal_strategist",
                "fitness_longevity_optimist", "aesthetics_focused_builder",
                "family_chef", "family_activity_coordinator", "parent",
                "polymath_in_training",
            }
            if role not in valid_roles:
                role = "cybersecurity_executive"

            # Validate domain
            valid_domains = {"professional", "personal", "family", "intellectual_growth"}
            if domain not in valid_domains:
                domain = "professional"

            result = {
                "action": action,
                "skill": skill,
                "role": role,
                "domain": domain,
            }
            logger.info("chat_intent_classified", extra=result)
            return result

    except Exception as e:
        logger.warning("chat_intent_classification_failed", extra={"error": str(e)})

    # Fallback defaults
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


def _parse_json(raw: str) -> dict | None:
    """Extract JSON from LLM response, handling markdown fences and <think> tags."""
    import re
    text = raw.strip()
    # Strip qwen3 thinking tags
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return None
