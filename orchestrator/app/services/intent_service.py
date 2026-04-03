import logging
from enum import Enum

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.intent")


class IntentType(str, Enum):
    question = "question"
    analysis = "analysis"
    planning = "planning"
    execution = "execution"
    research = "research"
    creative = "creative"
    conversation = "conversation"


# Fast keyword-based classification — avoids an extra LLM call for common patterns
_KEYWORD_MAP: list[tuple[list[str], IntentType]] = [
    (["plan", "schedule", "roadmap", "timeline", "strategy", "prioritize"], IntentType.planning),
    (["analyze", "compare", "evaluate", "assess", "review", "audit", "pros and cons"], IntentType.analysis),
    (["research", "investigate", "survey", "study", "explore", "find out", "look into"], IntentType.research),
    (["build", "create", "implement", "deploy", "configure", "set up", "write code", "design"], IntentType.execution),
    (["write", "draft", "compose", "generate", "brainstorm", "ideate"], IntentType.creative),
]


def classify_intent(task_input: str) -> IntentType:
    """Classify user intent from input text using keyword matching."""
    lower = task_input.lower()

    for keywords, intent in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return intent

    # Heuristic: questions end with ? or start with interrogatives
    if lower.rstrip().endswith("?") or lower.split()[0] in (
        "what", "who", "where", "when", "why", "how", "is", "are", "can", "do", "does", "should", "would", "could",
    ):
        return IntentType.question

    return IntentType.question  # safe default
