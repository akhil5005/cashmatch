"""Aggregating comparisons into the numbers a client actually asks for.

Two of these matter far more than the rest, and they only mean anything as a
pair:

**Auto-match rate** -- how much work disappears.
**Precision of auto-applied matches** -- how often the work that disappeared
was done correctly.

Either one alone is meaningless. A system that auto-applies everything hits
100% auto-match rate and is useless; one that auto-applies nothing hits 100%
precision and is also useless. The whole engineering question is how far the
first can be pushed while holding the second above the bar finance accepts.

Everything here is reported both by count and by value, because a wrong
match on a Rs. 9 lakh payment and one on Rs. 9,000 are not the same event,
and a count-only report hides that completely.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from cashmatch.evaluation.comparison import Comparison, Verdict
from cashmatch.models.enums import MatchDecision


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


@dataclass(slots=True)
class ScenarioMetrics:
    """How the engine did on one scenario type.

    Reported separately because an averaged number hides exactly the thing
    worth knowing: a weak spot in short-pay detection disappears into a
    headline dominated by easy exact matches.
    """

    scenario: str
    total: int = 0
    auto_applied: int = 0
    needs_review: int = 0
    unapplied: int = 0
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    auto_wrong: int = 0
    invoices_right_amounts_wrong: int = 0
    reason_scored: int = 0
    reason_correct: int = 0

    @property
    def accuracy(self) -> float:
        """Share decided correctly, whatever the decision was."""
        return _rate(self.correct, self.total)

    @property
    def auto_rate(self) -> float:
        return _rate(self.auto_applied, self.total)

    @property
    def auto_precision(self) -> float:
        return _rate(self.auto_applied - self.auto_wrong, self.auto_applied)

    @property
    def reason_accuracy(self) -> float:
        return _rate(self.reason_correct, self.reason_scored)


@dataclass(slots=True)
class EvaluationReport:
    """The whole evaluation, ready to render."""

    total: int = 0
    total_value_paise: int = 0

    by_decision: Counter = field(default_factory=Counter)
    by_verdict: Counter = field(default_factory=Counter)
    by_scenario: dict[str, ScenarioMetrics] = field(default_factory=dict)
    by_strategy: dict[str, ScenarioMetrics] = field(default_factory=dict)

    auto_applied: int = 0
    auto_correct: int = 0
    auto_wrong: int = 0
    auto_value_paise: int = 0
    auto_wrong_value_paise: int = 0

    review: int = 0
    review_correct: int = 0
    unapplied: int = 0
    unapplied_correct: int = 0
    missed: int = 0

    reason_scored: int = 0
    reason_correct: int = 0

    #: Wrongly auto-applied payments, worst by value first.
    worst_errors: list[Comparison] = field(default_factory=list)

    config_digest: str = ""
    seed: int = 0
    thresholds: tuple[float, float] = (0.0, 0.0)

    # --- the headline pair ------------------------------------------------

    @property
    def auto_match_rate(self) -> float:
        """Share of payments cleared with no human involvement."""
        return _rate(self.auto_applied, self.total)

    @property
    def auto_precision(self) -> float:
        """Of the payments auto-applied, how many were right.

        The number that decides deployability. A high auto-match rate with
        poor precision is worse than no automation at all, because the
        errors are now invisible until a customer complains.
        """
        return _rate(self.auto_correct, self.auto_applied)

    @property
    def value_precision(self) -> float:
        """Precision weighted by money, not by count.

        A wrong match on a Rs. 9 lakh payment is not the same event as one
        on Rs. 9,000, and finance will ask about the rupees.
        """
        return _rate(self.auto_value_paise - self.auto_wrong_value_paise, self.auto_value_paise)

    # --- the rest ---------------------------------------------------------

    @property
    def review_rate(self) -> float:
        return _rate(self.review, self.total)

    @property
    def unapplied_rate(self) -> float:
        return _rate(self.unapplied, self.total)

    @property
    def overall_accuracy(self) -> float:
        """Share of all payments the engine got right, including the ones it
        correctly declined to touch."""
        return _rate(
            self.by_verdict[Verdict.CORRECT] + self.by_verdict[Verdict.CORRECTLY_UNAPPLIED],
            self.total,
        )

    @property
    def reason_accuracy(self) -> float:
        return _rate(self.reason_correct, self.reason_scored)

    @property
    def recoverable_rate(self) -> float:
        """Correct matches currently sitting in the review queue.

        Head-room: work a human will do that the engine already got right,
        and would automate if the threshold moved.
        """
        return _rate(self.review_correct, self.total)


def build_report(comparisons: list[Comparison]) -> EvaluationReport:
    """Aggregate per-payment verdicts into the report."""
    report = EvaluationReport(total=len(comparisons))

    scenarios: dict[str, ScenarioMetrics] = defaultdict(lambda: ScenarioMetrics(""))
    strategies: dict[str, ScenarioMetrics] = defaultdict(lambda: ScenarioMetrics(""))

    for item in comparisons:
        report.total_value_paise += item.amount_paise
        report.by_decision[item.decision.value] += 1
        report.by_verdict[item.verdict] += 1

        scenario = scenarios[item.scenario]
        scenario.scenario = item.scenario
        strategy = strategies[item.strategy]
        strategy.scenario = item.strategy

        for bucket in (scenario, strategy):
            _tally(bucket, item)

        if item.decision is MatchDecision.AUTO_APPLIED:
            report.auto_applied += 1
            report.auto_value_paise += item.amount_paise
            if item.is_correct:
                report.auto_correct += 1
            else:
                report.auto_wrong += 1
                report.auto_wrong_value_paise += item.amount_paise
        elif item.decision is MatchDecision.NEEDS_REVIEW:
            report.review += 1
            report.review_correct += item.is_correct
        else:
            report.unapplied += 1
            report.unapplied_correct += item.is_correct

        if item.verdict is Verdict.MISSED:
            report.missed += 1

        if item.reason_correct is not None:
            report.reason_scored += 1
            report.reason_correct += item.reason_correct

    report.by_scenario = dict(sorted(scenarios.items()))
    report.by_strategy = dict(sorted(strategies.items()))
    report.worst_errors = sorted(
        (item for item in comparisons if item.is_costly),
        key=lambda item: item.amount_paise,
        reverse=True,
    )[:10]

    return report


def _tally(bucket: ScenarioMetrics, item: Comparison) -> None:
    bucket.total += 1

    if item.decision is MatchDecision.AUTO_APPLIED:
        bucket.auto_applied += 1
        if not item.is_correct:
            bucket.auto_wrong += 1
    elif item.decision is MatchDecision.NEEDS_REVIEW:
        bucket.needs_review += 1
    else:
        bucket.unapplied += 1

    if item.is_correct:
        bucket.correct += 1
    elif item.verdict is Verdict.MISSED:
        bucket.missed += 1
    else:
        bucket.wrong += 1
        if item.invoices_correct:
            bucket.invoices_right_amounts_wrong += 1

    if item.reason_correct is not None:
        bucket.reason_scored += 1
        bucket.reason_correct += item.reason_correct
