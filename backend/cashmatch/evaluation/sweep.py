"""The threshold sweep: automation against error, as a curve.

A single accuracy number invites the wrong conversation. "The system is 94%
accurate" tells a controller nothing actionable, because it hides the only
decision they actually get to make: **where to put the line.**

The sweep makes that decision visible. Every row is a setting the client
could choose; the columns are what they would get for it. A conservative
controller and an aggressive shared-services lead will legitimately pick
different rows, and neither is wrong.

Cheap to compute, because confidence does not depend on the threshold -- only
the *decision* does. Score once, then re-decide at each cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass

from cashmatch.evaluation.comparison import Comparison, compare
from cashmatch.generator.ground_truth import TruthPayment
from cashmatch.matching.config import ScoringConfig
from cashmatch.models.enums import MatchDecision
from cashmatch.scoring.scorer import ConfidenceScorer, Scored


@dataclass(slots=True)
class SweepRow:
    """What one auto-apply threshold would deliver."""

    threshold: float
    auto_applied: int
    auto_correct: int
    auto_wrong: int
    review: int
    unapplied: int
    total: int
    auto_value_paise: int
    auto_wrong_value_paise: int

    @property
    def auto_rate(self) -> float:
        return self.auto_applied / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        return self.auto_correct / self.auto_applied if self.auto_applied else 1.0

    @property
    def error_rate(self) -> float:
        """Wrong auto-applications as a share of *all* payments.

        The number to quote to a controller. "0.4% precision loss" sounds
        abstract; "three payments a thousand go to the wrong invoice with
        nobody looking" does not.
        """
        return self.auto_wrong / self.total if self.total else 0.0

    @property
    def review_rate(self) -> float:
        return self.review / self.total if self.total else 0.0

    @property
    def value_at_risk_paise(self) -> int:
        """Money auto-applied to the wrong invoice at this setting."""
        return self.auto_wrong_value_paise


def sweep_thresholds(
    scored: list[Scored],
    truth: dict[str, TruthPayment],
    config: ScoringConfig,
    *,
    thresholds: list[float] | None = None,
    tolerance_paise: int = 100,
) -> list[SweepRow]:
    """Re-decide every payment at each cutoff and measure the result.

    The confidence of each payment is fixed; moving the threshold only moves
    the line between auto-apply and review. So this re-runs neither the
    search nor the scorer.
    """
    if thresholds is None:
        thresholds = [round(0.50 + step * 0.05, 2) for step in range(11)]

    # Comparisons are computed once per payment under the hypothetical that
    # it was auto-applied; the verdict does not depend on the decision, only
    # on whether the proposed allocation was right.
    judged: list[tuple[Scored, Comparison]] = []
    for item in scored:
        entry = truth.get(item.outcome.statement_ref)
        if entry is None:
            continue
        judged.append((item, compare(item, entry, tolerance_paise)))

    rows: list[SweepRow] = []
    floor = config.thresholds.review_floor

    for threshold in thresholds:
        row = SweepRow(
            threshold=threshold,
            auto_applied=0,
            auto_correct=0,
            auto_wrong=0,
            review=0,
            unapplied=0,
            total=len(judged),
            auto_value_paise=0,
            auto_wrong_value_paise=0,
        )

        for item, comparison in judged:
            decision = _decide(item, threshold, floor)

            if decision is MatchDecision.AUTO_APPLIED:
                row.auto_applied += 1
                row.auto_value_paise += comparison.amount_paise
                if comparison.is_correct:
                    row.auto_correct += 1
                else:
                    row.auto_wrong += 1
                    row.auto_wrong_value_paise += comparison.amount_paise
            elif decision is MatchDecision.NEEDS_REVIEW:
                row.review += 1
            else:
                row.unapplied += 1

        rows.append(row)

    return rows


def _decide(scored: Scored, auto_threshold: float, review_floor: float) -> MatchDecision:
    """What this payment would have been decided at a different cutoff."""
    if scored.candidate is None:
        return MatchDecision.UNAPPLIED
    if scored.confidence >= auto_threshold:
        return MatchDecision.AUTO_APPLIED
    if scored.confidence >= review_floor:
        return MatchDecision.NEEDS_REVIEW
    return MatchDecision.UNAPPLIED


def recommend(rows: list[SweepRow], *, min_precision: float = 0.99) -> SweepRow | None:
    """The most automation available at or above a precision floor.

    Deliberately parameterised rather than hard-coded: the floor is the
    client's risk appetite, not the engineer's. What this function can say
    is "given your floor, here is the most work you can stop doing".
    """
    eligible = [row for row in rows if row.auto_applied and row.precision >= min_precision]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row.auto_rate, -row.threshold))


def rescore(outcomes, config: ScoringConfig) -> list[Scored]:
    """Score match outcomes under a different weighting.

    Used to measure what a signal is worth: zero its weight, re-score, and
    compare. Confidence changes here, so unlike the threshold sweep this
    really does have to run the scorer again -- but still not the search.
    """
    scorer = ConfidenceScorer(config)
    return [scorer.score(outcome) for outcome in outcomes]
