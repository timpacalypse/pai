import logging
import re
from enum import Enum

from app.services.intent_service import IntentType

logger = logging.getLogger("pai.workflow")


class WorkflowType(str, Enum):
    direct_response = "direct_response"
    retrieval_augmented = "retrieval_augmented"
    agent_research = "agent_research"
    agent_planning = "agent_planning"
    agent_analysis = "agent_analysis"
    multi_agent_competition = "multi_agent_competition"
    execution = "execution"


# Intent → Workflow mapping (config-driven, easily extensible)
_INTENT_WORKFLOW_MAP: dict[IntentType, WorkflowType] = {
    IntentType.question: WorkflowType.direct_response,
    IntentType.conversation: WorkflowType.direct_response,
    IntentType.creative: WorkflowType.direct_response,
    IntentType.analysis: WorkflowType.agent_analysis,
    IntentType.research: WorkflowType.agent_research,
    IntentType.planning: WorkflowType.agent_planning,
    IntentType.execution: WorkflowType.execution,
}


def route_workflow(intent: IntentType) -> WorkflowType:
    """Map an intent to the appropriate workflow type."""
    workflow = _INTENT_WORKFLOW_MAP.get(intent, WorkflowType.direct_response)
    logger.info("workflow_routed", extra={"intent": intent.value, "workflow": workflow.value})
    return workflow


# ── Complexity signals for auto-escalation ──

_COMPLEXITY_KEYWORDS = [
    "best", "compare", "evaluate", "comprehensive", "detailed", "thorough",
    "in-depth", "methodology", "strategy", "framework", "approach",
    "pros and cons", "tradeoff", "recommend", "should i", "which is better",
    "options", "alternatives", "how should", "what is the best",
]

_MULTI_DOMAIN_KEYWORDS = [
    "and also", "in addition", "as well as", "both", "multiple",
    "across", "holistic", "end-to-end", "full picture",
]


def select_agents_for_task(prompt: str, intent: IntentType) -> list[str]:
    """
    Determine which agents should be involved based on prompt content and intent.
    Returns a list of agent names. If len > 1, the orchestrator runs competition.
    """
    lower = prompt.lower()
    agents: list[str] = []

    # ── Intent-based agent selection ──
    # Research signals
    if intent == IntentType.research or any(kw in lower for kw in [
        "research", "investigate", "find out", "look into", "explore", "survey",
        "what is known", "state of the art", "latest", "current trends",
    ]):
        agents.append("research")

    # Analysis signals
    if intent == IntentType.analysis or any(kw in lower for kw in [
        "analyze", "compare", "evaluate", "assess", "pros and cons",
        "tradeoff", "strengths and weaknesses", "impact", "review",
    ]):
        agents.append("analysis")

    # Planning signals
    if intent == IntentType.planning or any(kw in lower for kw in [
        "plan", "roadmap", "strategy", "prioritize", "timeline",
        "how should i", "steps to", "methodology", "approach", "best way to",
        "how to", "implement",
    ]):
        agents.append("planning")

    # ── Complexity-based escalation ──
    complexity = _estimate_complexity(lower)

    # If complexity is high and we only have one agent, add complementary ones
    if complexity >= 2 and len(agents) <= 1:
        if "research" not in agents:
            agents.append("research")
        if "analysis" not in agents:
            agents.append("analysis")

    # Very high complexity gets planning too
    if complexity >= 3 and "planning" not in agents:
        agents.append("planning")

    # ── Fallback ──
    if not agents:
        # Simple questions/conversations — no agents needed (direct response)
        if intent in (IntentType.question, IntentType.conversation, IntentType.creative):
            return []
        # Execution tasks — direct
        if intent == IntentType.execution:
            return []
        # Default: research
        agents.append("research")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for a in agents:
        if a not in seen:
            seen.add(a)
            unique.append(a)

    logger.info(
        "agents_selected",
        extra={
            "agents": unique,
            "intent": intent.value,
            "complexity": complexity,
            "prompt_len": len(prompt),
        },
    )

    return unique


def _estimate_complexity(lower_prompt: str) -> int:
    """Estimate prompt complexity on a 0-4 scale."""
    complexity = 0

    # Length signal
    word_count = len(lower_prompt.split())
    if word_count > 50:
        complexity += 1
    if word_count > 100:
        complexity += 1

    # Complexity keywords
    hits = sum(1 for kw in _COMPLEXITY_KEYWORDS if kw in lower_prompt)
    if hits >= 2:
        complexity += 1
    if hits >= 4:
        complexity += 1

    # Multi-domain signals
    if any(kw in lower_prompt for kw in _MULTI_DOMAIN_KEYWORDS):
        complexity += 1

    return min(complexity, 4)
