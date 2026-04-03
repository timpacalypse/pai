import logging
from pydantic import BaseModel, Field

from app.agents.base import AgentOutput

logger = logging.getLogger("pai.evaluation")


class EvaluationScore(BaseModel):
    agent_name: str
    accuracy: float = Field(0.0, ge=0.0, le=1.0)
    relevance: float = Field(0.0, ge=0.0, le=1.0)
    depth: float = Field(0.0, ge=0.0, le=1.0)
    clarity: float = Field(0.0, ge=0.0, le=1.0)
    actionability: float = Field(0.0, ge=0.0, le=1.0)
    consistency: float = Field(0.0, ge=0.0, le=1.0)
    total: float = 0.0


# Default weights for the 6 evaluation criteria
DEFAULT_WEIGHTS = {
    "accuracy": 0.20,
    "relevance": 0.20,
    "depth": 0.15,
    "clarity": 0.15,
    "actionability": 0.20,
    "consistency": 0.10,
}


def evaluate_output(
    output: AgentOutput,
    task: str,
    weights: dict[str, float] | None = None,
) -> EvaluationScore:
    """
    Score an agent output on the 6 evaluation criteria from the spec.
    Uses heuristic scoring — fast, deterministic, no LLM call.
    """
    w = weights or DEFAULT_WEIGHTS

    # Heuristic scoring based on output characteristics
    result = output.result
    has_structured = bool(output.metadata)
    result_len = len(result)

    # Accuracy: use agent's self-reported confidence as a proxy
    accuracy = output.confidence

    # Relevance: check how many task keywords appear in the result
    task_words = set(task.lower().split())
    result_lower = result.lower()
    if task_words:
        overlap = sum(1 for word in task_words if word in result_lower)
        relevance = min(overlap / max(len(task_words), 1), 1.0)
    else:
        relevance = 0.5

    # Depth: based on response length and structure
    if result_len > 500:
        depth = 0.9
    elif result_len > 200:
        depth = 0.7
    elif result_len > 50:
        depth = 0.5
    else:
        depth = 0.3

    # Clarity: structured output is clearer
    clarity = 0.8 if has_structured else 0.5

    # Actionability: check for action-oriented keywords
    action_keywords = ["recommend", "should", "step", "implement", "action", "plan", "next", "consider"]
    action_hits = sum(1 for kw in action_keywords if kw in result_lower)
    actionability = min(0.3 + (action_hits * 0.1), 1.0)

    # Consistency: structured outputs with reasoning get bonus
    consistency = 0.7
    if has_structured and output.reasoning:
        consistency = 0.9

    # Weighted total
    total = (
        w["accuracy"] * accuracy
        + w["relevance"] * relevance
        + w["depth"] * depth
        + w["clarity"] * clarity
        + w["actionability"] * actionability
        + w["consistency"] * consistency
    )

    score = EvaluationScore(
        agent_name=output.agent_name,
        accuracy=round(accuracy, 3),
        relevance=round(relevance, 3),
        depth=round(depth, 3),
        clarity=round(clarity, 3),
        actionability=round(actionability, 3),
        consistency=round(consistency, 3),
        total=round(total, 3),
    )

    logger.info(
        "agent_evaluated",
        extra={"agent": output.agent_name, "total": score.total},
    )

    return score
