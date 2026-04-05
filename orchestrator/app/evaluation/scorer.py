import json
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


LLM_EVAL_SYSTEM_PROMPT = """\
You are an evaluation judge for a multi-agent AI system. You score an agent's output on 6 criteria.

Score each criterion from 0.0 to 1.0:
- accuracy: Are claims correct and well-supported? No hallucinations?
- relevance: Does the output directly address the task? No off-topic content?
- depth: Is the analysis thorough? Multiple angles explored?
- clarity: Is the output well-organized, readable, and coherent?
- actionability: Does it provide concrete, usable recommendations or next steps?
- consistency: Is the reasoning consistent? Does the conclusion follow the analysis?

Respond ONLY with valid JSON:
{"accuracy": 0.0, "relevance": 0.0, "depth": 0.0, "clarity": 0.0, "actionability": 0.0, "consistency": 0.0}
"""


async def evaluate_output_llm(
    output: AgentOutput,
    task: str,
    http_client=None,
    weights: dict[str, float] | None = None,
) -> EvaluationScore:
    """
    LLM-based evaluation: uses a fast model to score agent output on 6 criteria.
    Falls back to heuristic scoring if LLM evaluation fails.
    """
    if http_client is None:
        return evaluate_output(output, task, weights)

    w = weights or DEFAULT_WEIGHTS

    user_prompt = (
        f"Task given to agent:\n{task}\n\n"
        f"Agent: {output.agent_name}\n"
        f"Agent's output:\n{output.result[:2000]}\n\n"
        "Score this output on the 6 criteria."
    )

    try:
        from app.services.ollama_service import generate
        raw = await generate(
            prompt=user_prompt,
            system_prompt=LLM_EVAL_SYSTEM_PROMPT,
            model="qwen3:4b",  # Fast model for evaluation
            http_client=http_client,
        )

        parsed = _parse_eval_json(raw)
        if parsed.get("parse_error"):
            logger.warning("llm_eval_parse_failed", extra={"agent": output.agent_name})
            return evaluate_output(output, task, weights)

        accuracy = max(0.0, min(1.0, float(parsed.get("accuracy", 0.5))))
        relevance = max(0.0, min(1.0, float(parsed.get("relevance", 0.5))))
        depth = max(0.0, min(1.0, float(parsed.get("depth", 0.5))))
        clarity = max(0.0, min(1.0, float(parsed.get("clarity", 0.5))))
        actionability = max(0.0, min(1.0, float(parsed.get("actionability", 0.5))))
        consistency = max(0.0, min(1.0, float(parsed.get("consistency", 0.5))))

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
            "agent_evaluated_llm",
            extra={"agent": output.agent_name, "total": score.total},
        )
        return score

    except Exception as e:
        logger.warning("llm_eval_failed", extra={"agent": output.agent_name, "error": str(e)})
        return evaluate_output(output, task, weights)


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


def _parse_eval_json(raw: str) -> dict:
    """Parse JSON from LLM evaluation response."""
    text_clean = raw.strip()
    if text_clean.startswith("```"):
        lines = text_clean.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text_clean = "\n".join(lines)
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        start = text_clean.find("{")
        end = text_clean.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text_clean[start:end])
            except json.JSONDecodeError:
                pass
        return {"parse_error": True}
