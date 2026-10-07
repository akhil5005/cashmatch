"""Recording decisions, and posting cash to the ledger.

Two separate concerns, deliberately. Writing a ``match_results`` row is
always safe and always re-runnable. Reducing an invoice's open amount changes
a customer's balance, and the tests that matter here are the ones proving it
cannot happen twice, cannot happen by accident, and cannot overwrite a
human's judgement.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cashmatch.config import LLMMode, Settings
from cashmatch.db.base import Base
from cashmatch.extraction import run_extraction
from cashmatch.generator import generate_dataset
from cashmatch.models import (
    AuditLog,
    BankTransaction,
    Invoice,
    MatchAllocation,
    MatchResult,
)
from cashmatch.models.enums import InvoiceStatus, MatchDecision, ReviewAction, TransactionStatus
from cashmatch.scoring import run_decisions
from cashmatch.scoring.persistence import clear_previous_results

from .support import load_config
from .test_generator import small_config

CONFIG = load_config()


@pytest.fixture
def populated(tmp_path) -> Iterator[Session]:
    """A generated and extracted dataset, ready to decide on."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    generate_dataset(session, small_config(), tmp_path)
    session.flush()
    run_extraction(session, Settings(llm_mode=LLMMode.MOCK, data_dir=tmp_path), CONFIG)
    session.commit()

    yield session

    session.close()
    engine.dispose()


def _open_total(session: Session) -> int:
    return session.scalar(select(func.sum(Invoice.open_amount_paise)))


# ===========================================================================
# recording
# ===========================================================================


def test_every_transaction_gets_exactly_one_decision(populated: Session) -> None:
    scored, summary = run_decisions(populated, CONFIG)
    populated.commit()

    assert summary.transactions == small_config().volume.payments
    assert populated.scalar(select(func.count()).select_from(MatchResult)) == summary.transactions


def test_the_decision_rates_add_up(populated: Session) -> None:
    _, summary = run_decisions(populated, CONFIG)

    assert sum(summary.by_decision.values()) == summary.transactions
    total = summary.auto_match_rate + summary.review_rate + summary.unapplied_rate
    assert total == pytest.approx(1.0)


def test_allocations_are_written_for_every_decided_candidate(populated: Session) -> None:
    scored, _ = run_decisions(populated, CONFIG)
    populated.commit()

    expected = sum(len(s.candidate.allocations) for s in scored if s.candidate is not None)
    assert populated.scalar(select(func.count()).select_from(MatchAllocation)) == expected


def test_the_stored_confidence_matches_the_scored_one(populated: Session) -> None:
    scored, _ = run_decisions(populated, CONFIG)
    populated.commit()

    by_ref = {s.outcome.statement_ref: s for s in scored}
    rows = populated.execute(
        select(BankTransaction.statement_ref, MatchResult.confidence).join(
            MatchResult, MatchResult.bank_transaction_id == BankTransaction.id
        )
    ).all()

    for statement_ref, confidence in rows:
        assert float(confidence) == pytest.approx(by_ref[statement_ref].confidence, abs=1e-4)


def test_the_explanation_round_trips_through_the_database(populated: Session) -> None:
    """It lands in a JSONB column and the review UI reads it back out."""
    run_decisions(populated, CONFIG)
    populated.commit()

    result = populated.scalar(
        select(MatchResult).where(MatchResult.decision == MatchDecision.AUTO_APPLIED).limit(1)
    )
    assert result is not None

    payload = result.explanation
    assert payload["decision"] == "auto_applied"
    assert payload["signals"]
    assert payload["matching"]["trail"]
    assert payload["applicable_weight"] > 0


def test_every_decision_is_audited(populated: Session) -> None:
    """ "The system looked at this payment and decided not to apply it" is as
    much a decision as applying it, and finance software gets audited."""
    _, summary = run_decisions(populated, CONFIG)
    populated.commit()

    decided = populated.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.like("decided_%"))
    )
    assert decided == summary.transactions


def test_nothing_is_posted_without_being_asked(populated: Session) -> None:
    """The default is suggest-only. A first deployment runs this way for
    weeks while the client watches the precision number."""
    before = _open_total(populated)

    _, summary = run_decisions(populated, CONFIG, post=False)
    populated.commit()

    assert _open_total(populated) == before
    assert summary.posted_invoices == 0
    statuses = {row.status for row in populated.scalars(select(BankTransaction)).all()}
    assert statuses == {TransactionStatus.UNMATCHED}


# ===========================================================================
# re-running
# ===========================================================================


def test_re_running_replaces_its_own_verdict_rather_than_stacking(
    populated: Session,
) -> None:
    run_decisions(populated, CONFIG)
    populated.commit()
    first = populated.scalar(select(func.count()).select_from(MatchResult))

    _, summary = run_decisions(populated, CONFIG)
    populated.commit()

    assert summary.replaced == first
    assert populated.scalar(select(func.count()).select_from(MatchResult)) == first


def test_a_decision_a_human_touched_is_never_overwritten(populated: Session) -> None:
    """Replacing an analyst's judgement with a fresh machine guess is the
    worst behaviour available here."""
    from datetime import UTC, datetime

    run_decisions(populated, CONFIG)
    populated.commit()

    reviewed = populated.scalar(select(MatchResult).limit(1))
    reviewed.reviewed_by = "user:akhil"
    reviewed.reviewed_at = datetime.now(UTC)
    reviewed.review_action = ReviewAction.APPROVED
    reviewed_id = reviewed.id
    populated.commit()

    statement_refs = list(populated.scalars(select(BankTransaction.statement_ref)).all())
    clear_previous_results(populated, statement_refs)
    populated.commit()

    assert populated.get(MatchResult, reviewed_id) is not None


def test_re_running_is_deterministic(populated: Session) -> None:
    first, _ = run_decisions(populated, CONFIG)
    second, _ = run_decisions(populated, CONFIG)

    assert [s.confidence for s in first] == [s.confidence for s in second]
    assert [s.decision for s in first] == [s.decision for s in second]


# ===========================================================================
# posting cash
# ===========================================================================


def test_posting_reduces_the_open_amount_only_for_auto_applied(
    populated: Session,
) -> None:
    before = _open_total(populated)

    scored, summary = run_decisions(populated, CONFIG, post=True)
    populated.commit()

    auto = [s for s in scored if s.decision is MatchDecision.AUTO_APPLIED]
    expected_cleared = sum(
        allocation.allocated_amount_paise + allocation.deduction_amount_paise
        for s in auto
        for allocation in s.candidate.allocations
    )

    assert summary.posted_invoices > 0
    assert before - _open_total(populated) == expected_cleared


def test_posting_moves_the_invoice_status_with_the_balance(populated: Session) -> None:
    run_decisions(populated, CONFIG, post=True)
    populated.commit()

    for invoice in populated.scalars(select(Invoice)).all():
        if invoice.open_amount_paise == 0:
            assert invoice.status is InvoiceStatus.PAID
        elif invoice.open_amount_paise < invoice.amount_paise:
            assert invoice.status is InvoiceStatus.PARTIALLY_PAID
        else:
            assert invoice.status is InvoiceStatus.OPEN


def test_posting_marks_the_transaction_so_it_cannot_happen_twice(
    populated: Session,
) -> None:
    """Idempotency by the transaction's own status. Posting the same payment
    twice would clear its invoices twice and corrupt the balance."""
    run_decisions(populated, CONFIG, post=True)
    populated.commit()
    after_first = _open_total(populated)

    run_decisions(populated, CONFIG, post=True)
    populated.commit()

    assert _open_total(populated) == after_first


def test_a_posted_transaction_is_no_longer_unmatched(populated: Session) -> None:
    run_decisions(populated, CONFIG, post=True)
    populated.commit()

    posted = populated.scalars(
        select(BankTransaction).where(BankTransaction.status != TransactionStatus.UNMATCHED)
    ).all()
    assert posted
    assert all(
        row.status in (TransactionStatus.MATCHED, TransactionStatus.PARTIALLY_APPLIED)
        for row in posted
    )


def test_posting_never_drives_an_invoice_negative(populated: Session) -> None:
    run_decisions(populated, CONFIG, post=True)
    populated.commit()

    assert all(
        0 <= invoice.open_amount_paise <= invoice.amount_paise
        for invoice in populated.scalars(select(Invoice)).all()
    )


def test_posting_is_audited_separately_from_deciding(populated: Session) -> None:
    """Six months later, "why did Rs. 4 lakh land on INV-00042" has to be an
    answerable question."""
    run_decisions(populated, CONFIG, post=True)
    populated.commit()

    posted = populated.scalars(select(AuditLog).where(AuditLog.action == "cash_posted")).all()
    assert posted
    for entry in posted:
        assert entry.actor.startswith("system:matcher@")
        assert entry.payload_after["statement_ref"]
        assert entry.payload_after["invoices"]


def test_the_cash_posted_equals_the_cash_reported(populated: Session) -> None:
    _, summary = run_decisions(populated, CONFIG, post=True)
    populated.commit()

    applied = populated.scalar(
        select(func.sum(MatchAllocation.allocated_amount_paise))
        .join(MatchResult, MatchAllocation.match_result_id == MatchResult.id)
        .where(MatchResult.decision == MatchDecision.AUTO_APPLIED)
    )
    assert summary.posted_cash_paise == applied


def test_an_auto_applied_decision_never_claims_more_than_arrived(
    populated: Session,
) -> None:
    """The invariant that must survive scoring as well as matching."""
    scored, _ = run_decisions(populated, CONFIG)

    for item in scored:
        if item.decision is MatchDecision.AUTO_APPLIED:
            assert item.candidate.allocated_paise <= item.outcome.amount_paise


# ===========================================================================
# the money adds up
# ===========================================================================


def test_the_money_in_the_summary_accounts_for_every_payment(
    populated: Session,
) -> None:
    _, summary = run_decisions(populated, CONFIG)

    total_received = populated.scalar(select(func.sum(BankTransaction.amount_paise)))
    reported = summary.auto_applied_paise + summary.review_paise + summary.unapplied_paise

    assert reported == total_received


def test_confidence_buckets_cover_every_transaction(populated: Session) -> None:
    _, summary = run_decisions(populated, CONFIG)
    assert sum(summary.confidence_buckets.values()) == summary.transactions


def test_auto_applied_decisions_all_clear_the_threshold(populated: Session) -> None:
    scored, _ = run_decisions(populated, CONFIG)

    for item in scored:
        if item.decision is MatchDecision.AUTO_APPLIED:
            assert item.confidence >= CONFIG.scoring.thresholds.auto_apply
        elif item.decision is MatchDecision.NEEDS_REVIEW:
            assert (
                CONFIG.scoring.thresholds.review_floor
                <= item.confidence
                < CONFIG.scoring.thresholds.auto_apply
            )
