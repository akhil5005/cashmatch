"""Schema-level guarantees: uniqueness, invariants, cascades, nullability.

These are the rules the rest of the project is allowed to assume. If one of
them breaks, every downstream phase quietly inherits bad data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cashmatch import ENGINE_VERSION
from cashmatch.models import (
    AuditLog,
    CustomerAlias,
    DeductionReason,
    ExtractionStatus,
    InvoiceStatus,
    MatchAllocation,
    MatchDecision,
    MatchResult,
    MatchStrategy,
    Remittance,
    RemittanceSource,
)
from cashmatch.money import rupees_to_paise
from cashmatch.normalize import normalize_party_name

from .conftest import make_invoice, make_transaction

# --- Invoices ---------------------------------------------------------------


def test_invoice_number_is_unique(session: Session, customer) -> None:
    make_invoice(session, customer, number="INV-00042")
    with pytest.raises(IntegrityError):
        make_invoice(session, customer, number="INV-00042")


def test_open_amount_cannot_exceed_invoice_amount(session: Session, customer) -> None:
    with pytest.raises(IntegrityError):
        make_invoice(session, customer, rupees="1000.00", open_rupees="2000.00")


def test_zero_amount_invoice_is_rejected(session: Session, customer) -> None:
    with pytest.raises(IntegrityError):
        make_invoice(session, customer, rupees="0.00")


@pytest.mark.parametrize(
    ("amount", "open_amount", "expected"),
    [
        ("1000.00", "1000.00", InvoiceStatus.OPEN),
        ("1000.00", "400.00", InvoiceStatus.PARTIALLY_PAID),
        ("1000.00", "0.00", InvoiceStatus.PAID),
    ],
)
def test_status_follows_the_open_amount(
    session: Session, customer, amount, open_amount, expected
) -> None:
    invoice = make_invoice(session, customer, rupees=amount, open_rupees=open_amount)
    assert invoice.status_for_open_amount() is expected


def test_is_open_reflects_outstanding_money(session: Session, customer) -> None:
    open_invoice = make_invoice(session, customer, number="INV-1", rupees="500.00")
    assert open_invoice.is_open

    paid = make_invoice(session, customer, number="INV-2", rupees="500.00", open_rupees="0.00")
    paid.status = InvoiceStatus.PAID
    assert not paid.is_open


# --- Customers and aliases --------------------------------------------------


def test_duplicate_alias_for_one_customer_is_rejected(session: Session, customer) -> None:
    for raw in ("ABC TRADERS", "A.B.C. Traders"):
        session.add(
            CustomerAlias(
                customer_id=customer.id,
                alias=raw,
                normalized_alias=normalize_party_name(raw),
            )
        )
    # Both normalise to "abc traders", which is the point of the constraint.
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_a_customer_removes_its_aliases(session: Session, customer) -> None:
    session.add(
        CustomerAlias(customer_id=customer.id, alias="ABC TRADERS", normalized_alias="abc traders")
    )
    session.flush()
    session.delete(customer)
    session.flush()
    assert session.query(CustomerAlias).count() == 0


# --- Bank transactions ------------------------------------------------------


def test_statement_ref_is_unique_so_reuploads_are_idempotent(session: Session) -> None:
    make_transaction(session, ref="UTR123")
    with pytest.raises(IntegrityError):
        make_transaction(session, ref="UTR123")


def test_bank_transaction_has_no_ground_truth_column() -> None:
    """The answer key must live only in data/ground_truth/ on disk.

    If a scenario label ever appeared on this table the matcher could read it
    and every accuracy number in Phase 6 would be worthless.
    """
    from cashmatch.models import BankTransaction

    columns = set(BankTransaction.__table__.columns.keys())
    leaks = {c for c in columns if "scenario" in c or "truth" in c or "expected" in c}
    assert not leaks, f"ground-truth leak into the matcher input table: {leaks}"


# --- Remittances ------------------------------------------------------------


def test_remittance_can_exist_before_its_payment_is_known(session: Session) -> None:
    """Advice arrives by email, money arrives by bank file, and the two are
    not linked on arrival. An unlinked remittance must be storable."""
    advice = Remittance(
        source_type=RemittanceSource.EMAIL,
        raw_text="Payment for INV-00042 and INV-00043, less damage claim.",
    )
    session.add(advice)
    session.flush()
    assert advice.bank_transaction_id is None
    assert advice.extraction_status is ExtractionStatus.PENDING


# --- Match results and allocations -----------------------------------------


def _make_match(session: Session, txn, **kwargs) -> MatchResult:
    result = MatchResult(
        bank_transaction_id=txn.id,
        decision=kwargs.pop("decision", MatchDecision.AUTO_APPLIED),
        confidence=kwargs.pop("confidence", Decimal("0.9500")),
        strategy=kwargs.pop("strategy", MatchStrategy.REFERENCE_EXACT),
        engine_version=ENGINE_VERSION,
        **kwargs,
    )
    session.add(result)
    session.flush()
    return result


def test_allocations_sum_to_the_matched_amount(session: Session, customer) -> None:
    first = make_invoice(session, customer, number="INV-1", rupees="30000.00")
    second = make_invoice(session, customer, number="INV-2", rupees="11850.00")
    txn = make_transaction(session, rupees="41850.00")

    match = _make_match(
        session,
        txn,
        strategy=MatchStrategy.SUBSET_SUM,
        matched_amount_paise=txn.amount_paise,
    )
    for invoice in (first, second):
        session.add(
            MatchAllocation(
                match_result_id=match.id,
                invoice_id=invoice.id,
                allocated_amount_paise=invoice.amount_paise,
            )
        )
    session.flush()
    session.refresh(match)

    assert match.allocated_total_paise == match.matched_amount_paise
    assert match.allocated_total_paise == rupees_to_paise("41850.00")
    assert match.deduction_total_paise == 0


def test_short_pay_splits_into_cash_plus_a_reasoned_deduction(session: Session, customer) -> None:
    """A short-pay is not an underpayment: the customer is asserting a claim.
    Cash applied plus the deduction must still clear the invoice in full."""
    invoice = make_invoice(session, customer, rupees="41850.00")
    txn = make_transaction(session, rupees="39850.00")

    match = _make_match(
        session,
        txn,
        strategy=MatchStrategy.SHORT_PAY,
        matched_amount_paise=txn.amount_paise,
    )
    session.add(
        MatchAllocation(
            match_result_id=match.id,
            invoice_id=invoice.id,
            allocated_amount_paise=rupees_to_paise("39850.00"),
            deduction_amount_paise=rupees_to_paise("2000.00"),
            deduction_reason=DeductionReason.DAMAGE,
            deduction_note="12 cases damaged in transit",
        )
    )
    session.flush()
    session.refresh(match)

    allocation = match.allocations[0]
    assert (
        allocation.allocated_amount_paise + allocation.deduction_amount_paise
        == invoice.open_amount_paise
    )
    assert allocation.deduction_reason is DeductionReason.DAMAGE


def test_an_invoice_cannot_be_allocated_twice_in_one_match(session: Session, customer) -> None:
    invoice = make_invoice(session, customer)
    txn = make_transaction(session)
    match = _make_match(session, txn, matched_amount_paise=txn.amount_paise)
    for _ in range(2):
        session.add(
            MatchAllocation(
                match_result_id=match.id,
                invoice_id=invoice.id,
                allocated_amount_paise=rupees_to_paise("100.00"),
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_confidence_outside_zero_to_one_is_rejected(session: Session) -> None:
    txn = make_transaction(session)
    with pytest.raises(IntegrityError):
        _make_match(session, txn, confidence=Decimal("1.5000"))


def test_deleting_a_match_removes_its_allocations(session: Session, customer) -> None:
    invoice = make_invoice(session, customer)
    txn = make_transaction(session)
    match = _make_match(session, txn, matched_amount_paise=txn.amount_paise)
    session.add(
        MatchAllocation(
            match_result_id=match.id,
            invoice_id=invoice.id,
            allocated_amount_paise=invoice.amount_paise,
        )
    )
    session.flush()
    session.delete(match)
    session.flush()
    assert session.query(MatchAllocation).count() == 0


def test_explanation_json_survives_a_round_trip(session: Session) -> None:
    """Phase 5 writes the signal breakdown here and the review UI reads it
    back. Nested structure must come out exactly as it went in."""
    txn = make_transaction(session)
    explanation = {
        "signals": [
            {"name": "reference_match", "fired": True, "weight": 0.40, "contribution": 0.40},
            {"name": "amount_match", "fired": True, "weight": 0.30, "contribution": 0.30},
        ],
        "candidates_considered": 12,
        "rejected": [{"invoice": "INV-00039", "why": "amount gap exceeds tolerance"}],
    }
    match = _make_match(session, txn, explanation=explanation, reason_text="Exact reference hit.")
    session.expire(match)

    reloaded = session.get(MatchResult, match.id)
    assert reloaded.explanation == explanation
    assert reloaded.engine_version == ENGINE_VERSION


def test_unapplied_cash_is_recorded_when_nothing_matches(session: Session) -> None:
    """Money we cannot place does not vanish. It sits as unapplied cash on
    the customer account, and the amount has to stay visible."""
    txn = make_transaction(session, rupees="7500.00", narration="")
    match = _make_match(
        session,
        txn,
        decision=MatchDecision.UNAPPLIED,
        strategy=MatchStrategy.NONE,
        confidence=Decimal("0"),
        matched_amount_paise=0,
        unapplied_amount_paise=txn.amount_paise,
        reason_text="No open invoice found for this payer within tolerance.",
    )
    assert match.unapplied_amount_paise == rupees_to_paise("7500.00")
    assert match.allocations == []


# --- Audit ------------------------------------------------------------------


def test_audit_entry_outlives_its_subject(session: Session) -> None:
    """The log holds a soft pointer rather than a foreign key, precisely so
    the trail survives deletion of the record it describes."""
    entry = AuditLog(
        entity_type="match_result",
        entity_id=999,
        action="auto_applied",
        actor=f"system:{ENGINE_VERSION}",
        payload_after={"decision": "auto_applied", "confidence": "0.95"},
    )
    session.add(entry)
    session.flush()
    assert session.query(AuditLog).count() == 1
    assert entry.actor.startswith("system:")


# --- Whole-schema invariants ------------------------------------------------


def test_every_money_column_is_an_integer_type() -> None:
    """Rule 1 of the project, asserted mechanically rather than by review."""
    from sqlalchemy import BigInteger, Integer

    from cashmatch.db.base import Base

    offenders = [
        f"{table.name}.{column.name} is {column.type}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith("_paise") and not isinstance(column.type, (BigInteger, Integer))
    ]
    assert not offenders, f"money stored as a non-integer type: {offenders}"


def test_no_float_columns_exist_anywhere() -> None:
    from sqlalchemy import Float

    from cashmatch.db.base import Base

    floats = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]
    assert not floats, f"float columns found: {floats}"


def test_schema_has_the_eight_expected_tables() -> None:
    from cashmatch.db.base import Base

    assert set(Base.metadata.tables) == {
        "customers",
        "customer_aliases",
        "invoices",
        "bank_transactions",
        "remittances",
        "match_results",
        "match_allocations",
        "audit_log",
    }


def test_dates_are_stored_as_dates_not_strings(session: Session, customer) -> None:
    invoice = make_invoice(session, customer)
    assert isinstance(invoice.due_date, date)
    assert isinstance(invoice.invoice_date, date)
