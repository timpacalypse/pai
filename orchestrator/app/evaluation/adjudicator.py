import logging
from enum import Enum
from pydantic import BaseModel

from app.agents.base import AgentOutput
from app.evaluation.scorer import EvaluationScore

logger = logging.getLogger("pai.adjudication")


class AdjudicationStrategy(str, Enum):
    best_score = "best_score"
    weighted = "weighted"
    synthesize = "synthesize"


class AdjudicationResult(BaseModel):
    strategy: AdjudicationStrategy
    winner: str | None = None
    scores: list[EvaluationScore]
    selected_outputs: list[AgentOutput]
    should_synthesize: bool = False


def adjudicate(
    outputs: list[AgentOutput],
    scores: list[EvaluationScore],
    strategy: AdjudicationStrategy = AdjudicationStrategy.best_score,
    synthesis_threshold: float = 0.1,
) -> AdjudicationResult:
    """
    Apply adjudication rules to select the best output(s).

    Strategies:
      - best_score: highest total score wins
      - weighted: top outputs within threshold are combined
      - synthesize: always synthesize the top outputs
    """
    if not scores:
        return AdjudicationResult(
            strategy=strategy, scores=[], selected_outputs=outputs, should_synthesize=False
        )

    # Sort scores descending
    sorted_scores = sorted(scores, key=lambda s: s.total, reverse=True)
    score_map = {s.agent_name: s for s in sorted_scores}
    output_map = {o.agent_name: o for o in outputs}

    if strategy == AdjudicationStrategy.best_score:
        winner = sorted_scores[0].agent_name
        logger.info("adjudication_winner", extra={"winner": winner, "score": sorted_scores[0].total})
        return AdjudicationResult(
            strategy=strategy,
            winner=winner,
            scores=sorted_scores,
            selected_outputs=[output_map[winner]],
            should_synthesize=False,
        )

    elif strategy == AdjudicationStrategy.weighted:
        # Select all outputs within threshold of top score
        top_score = sorted_scores[0].total
        selected = [
            output_map[s.agent_name]
            for s in sorted_scores
            if (top_score - s.total) <= synthesis_threshold and s.agent_name in output_map
        ]
        should_synth = len(selected) > 1
        winner = sorted_scores[0].agent_name if not should_synth else None
        logger.info(
            "adjudication_weighted",
            extra={"selected_count": len(selected), "should_synthesize": should_synth},
        )
        return AdjudicationResult(
            strategy=strategy,
            winner=winner,
            scores=sorted_scores,
            selected_outputs=selected,
            should_synthesize=should_synth,
        )

    elif strategy == AdjudicationStrategy.synthesize:
        # Always synthesize the top 2+
        top_n = min(len(sorted_scores), 3)
        selected = [
            output_map[s.agent_name]
            for s in sorted_scores[:top_n]
            if s.agent_name in output_map
        ]
        logger.info("adjudication_synthesize", extra={"selected_count": len(selected)})
        return AdjudicationResult(
            strategy=strategy,
            winner=None,
            scores=sorted_scores,
            selected_outputs=selected,
            should_synthesize=len(selected) > 1,
        )

    # Fallback
    return AdjudicationResult(
        strategy=strategy,
        scores=sorted_scores,
        selected_outputs=outputs,
        should_synthesize=False,
    )
