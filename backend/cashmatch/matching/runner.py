"""Running the cascade over a whole statement, and reporting what it did.

What this module reports is **engine diagnostics**, not accuracy. It can say
"612 of 800 payments produced a candidate set, 31 of them ambiguous"; it
cannot say whether those candidates were correct, because that would require
the ground-truth file and the matcher is not allowed anywhere near it. Phase 6
does the scoring.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.matching.book import OpenItemBook
from cashmatch.matching.candidates import MatchOutcome
from cashmatch.matching.config import MatchingConfig
from cashmatch.matching.engine import MatchingEngine, TransactionInput
from cashmatch.models import BankTransaction
from cashmatch.models.enums import TransactionStatus


@dataclass(slots=True)
class BatchSummary:
    """Aggregate diagnostics for one matching run."""

    transactions: int = 0
    with_candidates: int = 0
    ambiguous: int = 0
    no_candidates: int = 0
    by_strategy: Counter = field(default_factory=Counter)
    by_identification: Counter = field(default_factory=Counter)
    elapsed_s: float = 0.0
    nodes_visited: int = 0
    largest_pool: int = 0
    budget_exhaustions: int = 0
    #: Extracted advice documents linked to a payment and usable.
    advice_available: int = 0

    @property
    def candidate_rate(self) -> float:
        """Share of payments the engine could propose an allocation for.

        Not the auto-match rate. Some of these will be ambiguous and some
        will be wrong; thresholding and scoring are Phase 5's job.
        """
        return self.with_candidates / self.transactions if self.transactions else 0.0


def load_transactions(
    session: Session, *, limit: int | None = None, statement_ref: str | None = None
) -> list[TransactionInput]:
    """Read unmatched bank credits in a stable order."""
    query = select(BankTransaction).order_by(
        BankTransaction.value_date, BankTransaction.statement_ref
    )
    if statement_ref:
        query = query.where(BankTransaction.statement_ref == statement_ref)
    else:
        query = query.where(BankTransaction.status == TransactionStatus.UNMATCHED)
    if limit:
        query = query.limit(limit)

    return [
        TransactionInput(
            statement_ref=row.statement_ref,
            amount_paise=row.amount_paise,
            value_date=row.value_date,
            # Normalisation (step 1) happens at ingest and is stored on the
            # row, so the hot path never re-runs a regex.
            payer_name_normalized=row.payer_name_normalized,
            normalized_narration=row.normalized_narration,
        )
        for row in session.scalars(query).all()
    ]


def run_matching(
    session: Session,
    config: MatchingConfig,
    *,
    limit: int | None = None,
    statement_ref: str | None = None,
    use_advice: bool = True,
) -> tuple[list[MatchOutcome], BatchSummary]:
    """Match every unmatched transaction and return outcomes plus diagnostics.

    Nothing is written to the database. The cascade produces candidates; the
    decision about what to do with them belongs to Phase 5.

    Args:
        use_advice: feed extracted remittance advice into the cascade. Set
            False to measure the engine without it, which is how the value
            of Phase 4 gets quantified rather than asserted.
    """
    book = OpenItemBook.load(session)

    advice = None
    if use_advice:
        from cashmatch.extraction.advice import AdviceIndex

        advice = AdviceIndex.load(session)

    engine = MatchingEngine(book, config, advice)
    transactions = load_transactions(session, limit=limit, statement_ref=statement_ref)

    summary = BatchSummary(transactions=len(transactions))
    summary.advice_available = len(advice) if advice is not None else 0
    outcomes: list[MatchOutcome] = []

    started = time.perf_counter()
    for txn in transactions:
        outcome = engine.match(txn)
        outcomes.append(outcome)

        summary.by_identification[outcome.customer.method.value] += 1
        if outcome.has_candidates:
            summary.with_candidates += 1
            summary.by_strategy[outcome.strategy.value] += 1
            if outcome.is_ambiguous:
                summary.ambiguous += 1
        else:
            summary.no_candidates += 1

        for key in ("subset_sum", "short_pay"):
            search = outcome.diagnostics.get(key)
            if not search:
                continue
            summary.nodes_visited += search["nodes_visited"]
            summary.largest_pool = max(summary.largest_pool, search["pool_size"])
            if search["budget_exhausted"]:
                summary.budget_exhaustions += 1

    summary.elapsed_s = time.perf_counter() - started
    return outcomes, summary
