"""Phase 5 -- confidence scoring and the auto-apply decision.

Each signal the matcher recorded scores in [0, 1]; a weighted sum combines
them; penalties multiply the result. Two configurable thresholds then split
the confidence into auto-apply, human review, and unapplied cash.

The arithmetic is simple on purpose. A controller can be walked through the
number in a minute, and a client who wants fewer errors moves one threshold
and sees the trade immediately -- which is exactly what Phase 6 measures.
"""

from cashmatch.scoring.persistence import (
    PostingOutcome,
    audit_decision,
    clear_previous_results,
    post_cash,
    record_decision,
)
from cashmatch.scoring.runner import DecisionSummary, run_decisions, score_outcomes
from cashmatch.scoring.scorer import (
    AppliedPenalty,
    ConfidenceScorer,
    Scored,
    ScoredSignal,
)

__all__ = [
    "AppliedPenalty",
    "ConfidenceScorer",
    "DecisionSummary",
    "PostingOutcome",
    "Scored",
    "ScoredSignal",
    "audit_decision",
    "clear_previous_results",
    "post_cash",
    "record_decision",
    "run_decisions",
    "score_outcomes",
]
