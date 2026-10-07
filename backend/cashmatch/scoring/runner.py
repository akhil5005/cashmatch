"""Match, score, decide, record -- the full pass over a statement."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.matching.config import MatchingConfig
from cashmatch.matching.runner import run_matching
from cashmatch.models import BankTransaction
from cashmatch.models.enums import MatchDecision
from cashmatch.scoring.persistence import (
    audit_decision,
    clear_previous_results,
    post_cash,
    record_decision,
)
from cashmatch.scoring.scorer import ConfidenceScorer, Scored


@dataclass(slots=True)
class DecisionSummary:
    """What a scoring run decided, and what it cost."""

    transactions: int = 0
    by_decision: Counter = field(default_factory=Counter)
    by_strategy: Counter = field(default_factory=Counter)
    auto_applied_paise: int = 0
    review_paise: int = 0
    unapplied_paise: int = 0
    deductions_detected: int = 0
    posted_invoices: int = 0
    posted_cash_paise: int = 0
    replaced: int = 0
    elapsed_s: float = 0.0
    confidence_buckets: Counter = field(default_factory=Counter)

    @property
    def auto_match_rate(self) -> float:
        """Share of payments cleared with no human involvement.

        The headline number -- but only half of the pair that matters.
        Without precision beside it, it says nothing: a system that
        auto-applies everything scores 100% here and is useless. Phase 6
        supplies the other half.
        """
        if not self.transactions:
            return 0.0
        return self.by_decision[MatchDecision.AUTO_APPLIED.value] / self.transactions

    @property
    def review_rate(self) -> float:
        if not self.transactions:
            return 0.0
        return self.by_decision[MatchDecision.NEEDS_REVIEW.value] / self.transactions

    @property
    def unapplied_rate(self) -> float:
        if not self.transactions:
            return 0.0
        return self.by_decision[MatchDecision.UNAPPLIED.value] / self.transactions


def score_outcomes(outcomes, config: MatchingConfig) -> list[Scored]:
    """Score a list of match outcomes without touching the database.

    Separated from the persistence pass so Phase 6 can sweep thresholds over
    the same outcomes repeatedly without re-running the search or writing
    anything.
    """
    scorer = ConfidenceScorer(config.scoring)
    return [scorer.score(outcome) for outcome in outcomes]


def run_decisions(
    session: Session,
    config: MatchingConfig,
    *,
    limit: int | None = None,
    statement_ref: str | None = None,
    use_advice: bool = True,
    post: bool = False,
) -> tuple[list[Scored], DecisionSummary]:
    """Match every transaction, score it, record the decision.

    Args:
        post: also apply the cash for auto-applied decisions. Off by
            default: recording a decision is always safe, changing a
            customer's balance is not.
    """
    outcomes, _ = run_matching(
        session, config, limit=limit, statement_ref=statement_ref, use_advice=use_advice
    )
    scored_all = score_outcomes(outcomes, config)

    by_ref = {
        row.statement_ref: row
        for row in session.scalars(
            select(BankTransaction).where(
                BankTransaction.statement_ref.in_([s.outcome.statement_ref for s in scored_all])
            )
        ).all()
    }

    summary = DecisionSummary(transactions=len(scored_all))
    summary.replaced = clear_previous_results(session, list(by_ref))

    started = time.perf_counter()
    for scored in scored_all:
        transaction = by_ref.get(scored.outcome.statement_ref)
        if transaction is None:
            continue

        result = record_decision(session, transaction, scored)
        audit_decision(session, transaction, result)
        _tally(summary, scored)

        if post:
            posted = post_cash(session, transaction, result)
            summary.posted_invoices += posted.invoices_touched
            summary.posted_cash_paise += posted.cash_applied_paise

    session.flush()
    summary.elapsed_s = time.perf_counter() - started
    return scored_all, summary


def _tally(summary: DecisionSummary, scored: Scored) -> None:
    summary.by_decision[scored.decision.value] += 1
    summary.by_strategy[scored.outcome.strategy.value] += 1
    summary.confidence_buckets[_bucket(scored.confidence)] += 1

    amount = scored.outcome.amount_paise
    if scored.decision is MatchDecision.AUTO_APPLIED:
        summary.auto_applied_paise += amount
    elif scored.decision is MatchDecision.NEEDS_REVIEW:
        summary.review_paise += amount
    else:
        summary.unapplied_paise += amount

    candidate = scored.candidate
    if candidate is not None and candidate.deduction_paise:
        summary.deductions_detected += 1


def _bucket(confidence: float) -> str:
    """Ten-point bands, for seeing where the mass of the distribution sits."""
    if confidence <= 0:
        return "0.00"
    lower = min(int(confidence * 10) / 10, 0.9)
    return f"{lower:.1f}-{lower + 0.1:.1f}"
