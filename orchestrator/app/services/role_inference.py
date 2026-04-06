"""Auto-role inference — selects the best role(s) from the prompt content."""

import logging
import re

from app.models.schemas import RoleType, DomainType, RoleContext
from app.services.role_service import get_all_roles

logger = logging.getLogger("pai.role_inference")

# Domain-level keyword signals
_DOMAIN_KEYWORDS: dict[DomainType, list[str]] = {
    DomainType.professional: [
        "cybersecurity", "security", "compliance", "nist", "framework", "governance",
        "architecture", "enterprise", "infrastructure", "policy", "risk", "audit",
        "proposal", "rfp", "contract", "business", "strategy", "ciso", "soc",
        "ai governance", "threat", "vulnerability", "ato", "fedramp", "stig",
        "zero trust", "cloud security", "devsecops", "automation",
    ],
    DomainType.personal: [
        "fitness", "workout", "exercise", "gym", "diet", "nutrition", "health",
        "longevity", "bodybuilding", "physique", "muscle", "cardio", "weight",
        "meal", "recipe", "cook", "food", "dinner", "lunch", "breakfast",
        "ingredient", "kitchen", "menu",
        "medical", "doctor", "prescription", "medication", "dentist", "checkup",
        "colonoscopy", "surgery", "diagnosis", "vaccine", "lab", "blood",
        "home", "house", "hvac", "appliance", "maintenance", "repair",
        "plumbing", "furnace", "water heater", "air filter", "roof",
        "serviced", "replaced", "installed", "garage", "lake anna",
    ],
    DomainType.family: [
        "family", "kids", "children", "son", "daughter", "wife", "parenting",
        "school", "homework", "activity", "weekend", "trip", "vacation",
        "schedule", "birthday", "chore",
        "medical record", "health record", "appointment", "pediatrician",
        "allergy", "allergies", "dentist appointment",
    ],
    DomainType.intellectual_growth: [
        "learn", "study", "understand", "philosophy", "science", "history",
        "mathematics", "physics", "reading", "book", "course", "knowledge",
        "curiosity", "polymath", "interdisciplinary", "research methodology",
        "personal ai", "building an ai", "self-improvement",
    ],
}

# Role-specific keyword boosters (beyond what their description/goals already cover)
_ROLE_KEYWORDS: dict[RoleType, list[str]] = {
    RoleType.cybersecurity_executive: [
        "ciso", "security strategy", "modernization", "executive", "leadership",
        "organization", "program", "initiative", "board", "stakeholder",
    ],
    RoleType.ai_cybersecurity_strategist: [
        "ai security", "ai cybersecurity", "ai and security", "convergence",
        "ai tool", "machine learning threat", "adversarial", "llm security",
        "ai application", "emerging", "innovation",
    ],
    RoleType.ai_governance_practitioner: [
        "ai governance", "ai policy", "ai risk", "ai compliance", "ai rmf",
        "ai regulation", "executive order", "accountability", "ai audit",
        "responsible ai", "ai ethics",
    ],
    RoleType.educator_scholar: [
        "teach", "explain", "course", "curriculum", "phd", "doctoral",
        "dissertation", "academic", "lecture", "pedagogy", "concept",
    ],
    RoleType.solutions_architect: [
        "architect", "design", "implementation", "system", "diagram",
        "deploy", "pipeline", "integration", "technical solution", "build",
        "infrastructure", "code", "script", "api", "microservice",
    ],
    RoleType.proposal_strategist: [
        "proposal", "rfp", "rfq", "bid", "win theme", "evaluation criteria",
        "government contract", "solicitation", "pwins", "capture",
    ],
    RoleType.fitness_longevity_optimist: [
        "fitness", "workout", "exercise", "longevity", "healthspan",
        "strength", "cardio", "recovery", "supplement", "sleep",
    ],
    RoleType.aesthetics_focused_builder: [
        "physique", "bodybuilding", "aesthetics", "body composition",
        "muscle", "cut", "bulk", "lean", "definition",
    ],
    RoleType.family_chef: [
        "recipe", "cook", "meal plan", "dinner", "food", "ingredient",
        "kitchen", "nutrition", "menu", "grocery", "snack", "bake",
    ],
    RoleType.family_activity_coordinator: [
        "activity", "schedule", "trip", "vacation", "event", "outing",
        "weekend plan", "coordinate", "logistics",
        "home database", "house", "hvac", "appliance", "maintenance",
        "repair", "serviced", "replaced", "installed", "home",
        "plumbing", "furnace", "water heater", "garage", "lake anna",
    ],
    RoleType.parent: [
        "parent", "child", "kid", "son", "daughter", "school", "homework",
        "behavior", "discipline", "development", "teenager",
        "medical record", "doctor", "dentist", "checkup", "colonoscopy",
        "prescription", "medication", "vaccine", "surgery", "diagnosis",
        "health record", "lab result", "blood work", "appointment",
        "pediatrician", "allergy", "allergies",
    ],
    RoleType.polymath_in_training: [
        "polymath", "interdisciplinary", "cross-domain", "curiosity",
        "learn everything", "self-improvement", "personal ai", "methodology",
        "how to learn", "knowledge management",
    ],
}


def infer_roles(prompt: str) -> tuple[RoleType, RoleType | None]:
    """
    Infer the best primary and optional secondary role from prompt content.
    Returns (primary_role, secondary_role_or_None).
    """
    roles = get_all_roles()
    if not roles:
        return RoleType.cybersecurity_executive, None

    lower = prompt.lower()
    scores: dict[RoleType, float] = {}

    for role_ctx in roles:
        score = _score_role(lower, role_ctx)
        scores[role_ctx.role] = score

    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    primary = ranked[0][0]
    primary_score = ranked[0][1]

    # Secondary: pick if it's from a different domain AND has meaningful score
    secondary = None
    if len(ranked) > 1 and primary_score > 0:
        primary_domain = _get_domain(primary)
        for role, score in ranked[1:]:
            if score < 1.0:  # must have real relevance, not just noise
                break
            role_domain = _get_domain(role)
            # Prefer cross-domain secondary, but allow same-domain if strong signal
            if role_domain != primary_domain and score >= primary_score * 0.5:
                secondary = role
                break
            elif role_domain == primary_domain and score >= primary_score * 0.75:
                secondary = role
                break

    logger.info(
        "role_inferred",
        extra={
            "primary": primary.value,
            "primary_score": round(primary_score, 2),
            "secondary": secondary.value if secondary else None,
            "top_3": [(r.value, round(s, 2)) for r, s in ranked[:3]],
        },
    )

    return primary, secondary


def _score_role(lower_prompt: str, role_ctx: RoleContext) -> float:
    """Score how well a role matches the prompt."""
    score = 0.0

    # 1. Domain keyword matches (broad signal)
    domain_keywords = _DOMAIN_KEYWORDS.get(role_ctx.domain, [])
    domain_hits = sum(1 for kw in domain_keywords if kw in lower_prompt)
    score += min(domain_hits * 0.3, 2.0)

    # 2. Role-specific keyword matches (strong signal)
    role_keywords = _ROLE_KEYWORDS.get(role_ctx.role, [])
    role_hits = sum(1 for kw in role_keywords if kw in lower_prompt)
    score += role_hits * 0.8

    # 3. Description match (moderate signal)
    if role_ctx.description:
        desc_words = set(re.findall(r'\b\w{4,}\b', role_ctx.description.lower()))
        prompt_words = set(re.findall(r'\b\w{4,}\b', lower_prompt))
        overlap = len(desc_words & prompt_words)
        score += min(overlap * 0.4, 2.0)

    # 4. Goal relevance (moderate signal)
    for goal in role_ctx.goals:
        goal_words = set(re.findall(r'\b\w{4,}\b', goal.lower()))
        prompt_words = set(re.findall(r'\b\w{4,}\b', lower_prompt))
        overlap = len(goal_words & prompt_words)
        score += min(overlap * 0.3, 1.0)

    return score


def _get_domain(role: RoleType) -> DomainType:
    from app.models.schemas import ROLE_DOMAIN_MAP
    return ROLE_DOMAIN_MAP.get(role, DomainType.professional)
