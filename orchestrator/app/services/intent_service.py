import logging
from enum import Enum

import httpx

from app.core.config import settings
from app.services.ollama_service import generate

logger = logging.getLogger("pai.intent")


class IntentType(str, Enum):
    question = "question"
    analysis = "analysis"
    planning = "planning"
    execution = "execution"
    research = "research"
    creative = "creative"
    conversation = "conversation"


_INTENT_SYSTEM_PROMPT = (
    "Classify the user's message into exactly one intent type. "
    "Return ONLY the intent word, nothing else.\n\n"
    "Intent types:\n"
    "  question — asking for information or facts\n"
    "  analysis — comparing, evaluating, reviewing, auditing\n"
    "  planning — scheduling, roadmaps, strategy, prioritization\n"
    "  execution — building, creating, implementing, deploying, configuring\n"
    "  research — investigating, surveying, exploring, finding information\n"
    "  creative — writing, drafting, composing, brainstorming\n"
    "  conversation — casual chat, greetings, unclear intent\n"
)


async def classify_intent(task_input: str, http_client=None) -> IntentType:
    """Classify user intent using LLM."""
    try:
        raw = await generate(
            prompt=task_input,
            system_prompt=_INTENT_SYSTEM_PROMPT,
            model="qwen3:4b",
            http_client=http_client,
        )
        intent_str = raw.strip().lower().strip('"').strip("'")
        try:
            return IntentType(intent_str)
        except ValueError:
            # Fuzzy match
            for it in IntentType:
                if it.value in intent_str or intent_str in it.value:
                    return it
    except Exception as e:
        logger.warning("intent_classification_failed", extra={"error": str(e)})

    return IntentType.question
