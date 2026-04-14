"""LLM-based intent classification and role inference.

Uses a dynamic skill registry so new skills are automatically accessible
via natural language without changing routing code.
"""

import json
import logging

import httpx

from app.services.ollama_service import generate

logger = logging.getLogger("pai.llm_intent")

_cached_classifier_prompt: str | None = None


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
    """Classify a chat message into action, skill, role, and domain using qwen3:4b.

    Returns dict with keys: action, skill, role, domain.
    Falls back to safe defaults on any failure.
    """
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
