"""LLM-based intent classification and role inference.

Replaces keyword-based routing with a single fast LLM call (qwen3:4b)
that determines intent, role, domain, and what skill context to inject.
"""

import json
import logging

import httpx

from app.services.ollama_service import generate

logger = logging.getLogger("pai.llm_intent")

CLASSIFY_SYSTEM_PROMPT = """\
You are an intent classifier for a Personal AI assistant. Given a user message, determine:

1. **intent** — what the user wants to do:
   - "conversation" — general chat, questions, advice, discussion
   - "briefing" — requesting a daily briefing or summary of their day
   - "medical_record" — adding, updating, or logging medical/health information
   - "home_record" — adding, updating, or logging home maintenance, appliance, or property information
   - "calendar_event" — ONLY for adding/creating/scheduling a NEW event (e.g. "add dentist Thursday", "schedule meeting with Bob")
   - "meal_planning" — requesting meal plans, discussing food preferences
   - "recipe" — saving, searching, or discussing specific recipes

2. **role** — the best-fitting role for this request (pick exactly one):
   - "cybersecurity_executive" — cybersecurity strategy, leadership, programs
   - "ai_cybersecurity_strategist" — AI + security intersection, emerging tools
   - "ai_governance_practitioner" — AI governance, policy, compliance, frameworks
   - "educator_scholar" — teaching, explaining concepts, academic topics
   - "solutions_architect" — technical design, architecture, implementation
   - "proposal_strategist" — RFP responses, proposals, government contracting
   - "fitness_longevity_optimist" — fitness, health, longevity, exercise
   - "aesthetics_focused_builder" — physique, bodybuilding, body composition
   - "family_chef" — cooking, meal planning, recipes, nutrition for family
   - "family_activity_coordinator" — scheduling, activities, home management, logistics
   - "parent" — parenting, children, family medical records, family health
   - "polymath_in_training" — learning, cross-domain knowledge, curiosity

3. **domain** — the life domain this falls under:
   - "professional" — work, cybersecurity, AI, proposals, architecture
   - "personal" — fitness, health, cooking, home maintenance
   - "family" — children, family activities, family medical, parenting
   - "intellectual_growth" — learning, studying, knowledge building

4. **skill_context** — list of data sources to query for context (can be empty, or multiple):
   - "home" — home items, tasks, maintenance alerts, documents
   - "medical" — medical records, health history
   - "meals" — family food preferences, recent meal plans
   - "recipes" — saved recipes
   - "calendar" — upcoming events and appointments

Respond ONLY with valid JSON, no other text:

{"intent": "...", "role": "...", "domain": "...", "skill_context": [...]}

Rules:
- If the user is ADDING or RECORDING new data ("add", "schedule", "log", "record"), choose the mutation intent
- If the user is ASKING about or QUERYING existing data ("what's on", "show me", "when is", "list", "check"), choose "conversation" with appropriate skill_context
- CRITICAL: "what's on my calendar" / "what do I have tomorrow" / "any appointments" = "conversation" + skill_context ["calendar"], NOT "calendar_event"
- Default to "conversation" when uncertain about intent
- Pick the role that best matches the SUBJECT MATTER, not just keywords
- Medical/health topics for family → "parent" role, for personal health → "fitness_longevity_optimist"
- Home/property/appliance topics → "family_activity_coordinator"
- Do not overthink — pick the most natural fit
"""


async def classify_chat_intent(
    message: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Classify a chat message's intent, role, domain, and needed context using qwen3:4b.

    Returns dict with keys: intent, role, domain, skill_context.
    Falls back to safe defaults on any failure.
    """
    try:
        raw = await generate(
            prompt=message,
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            model="qwen3:4b",
            http_client=http_client,
        )
        parsed = _parse_json(raw)
        if parsed:
            # Validate fields
            intent = parsed.get("intent", "conversation")
            role = parsed.get("role", "cybersecurity_executive")
            domain = parsed.get("domain", "professional")
            skill_context = parsed.get("skill_context", [])

            # Validate intent
            valid_intents = {
                "conversation", "briefing", "medical_record",
                "home_record", "calendar_event", "meal_planning", "recipe",
            }
            if intent not in valid_intents:
                intent = "conversation"

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

            # Validate skill_context
            valid_ctx = {"home", "medical", "meals", "recipes", "calendar"}
            skill_context = [c for c in skill_context if c in valid_ctx]

            result = {
                "intent": intent,
                "role": role,
                "domain": domain,
                "skill_context": skill_context,
            }
            logger.info("chat_intent_classified", extra=result)
            return result

    except Exception as e:
        logger.warning("chat_intent_classification_failed", extra={"error": str(e)})

    # Fallback defaults
    return {
        "intent": "conversation",
        "role": "cybersecurity_executive",
        "domain": "professional",
        "skill_context": [],
    }


async def infer_roles_llm(
    message: str,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None]:
    """Infer primary and optional secondary role using qwen3:4b.

    Returns (primary_role, secondary_role_or_None).
    """
    result = await classify_chat_intent(message, http_client)
    return result["role"], None  # secondary role from LLM could be added later


def _parse_json(raw: str) -> dict | None:
    """Extract JSON from LLM response, handling markdown fences."""
    text = raw.strip()
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
