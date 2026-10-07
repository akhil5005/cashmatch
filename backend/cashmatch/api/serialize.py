"""Turning database rows into API shapes.

Kept apart from the routers so the mapping is testable without HTTP, and so
a router stays short enough to read in one go.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.api.schemas import (
    AllocationOut,
    CustomerOut,
    InvoiceOut,
    Money,
    PenaltyOut,
    ResultDetail,
    ResultSummary,
    SignalOut,
)
from cashmatch.models import Customer, Invoice, MatchResult, Remittance


def summarise(result: MatchResult) -> ResultSummary:
    """One row for the review queue."""
    txn = result.bank_transaction
    allocations = sorted(result.allocations, key=lambda a: a.invoice_id)

    return ResultSummary(
        id=result.id,
        statement_ref=txn.statement_ref,
        value_date=txn.value_date,
        amount=Money.of(txn.amount_paise),
        payer_name=txn.payer_name_raw,
        narration=txn.narration,
        customer_name=_customer_name(result),
        decision=result.decision,
        confidence=float(result.confidence),
        strategy=result.strategy,
        reason_text=result.reason_text,
        matched_amount=Money.of(result.matched_amount_paise),
        unapplied_amount=Money.of(result.unapplied_amount_paise),
        invoice_count=len(allocations),
        invoice_numbers=[a.invoice.invoice_number for a in allocations],
        has_deduction=any(a.deduction_amount_paise for a in allocations),
        reviewed_by=result.reviewed_by,
        reviewed_at=result.reviewed_at,
        review_action=result.review_action,
        created_at=result.created_at,
    )


def detail(session: Session, result: MatchResult) -> ResultDetail:
    """Everything a reviewer needs to judge one item without leaving the page."""
    payload: dict[str, Any] = result.explanation or {}
    base = summarise(result).model_dump()

    return ResultDetail(
        **base,
        allocations=[
            _allocation(a) for a in sorted(result.allocations, key=lambda x: x.invoice_id)
        ],
        signals=[_signal(s) for s in payload.get("signals", [])],
        penalties=[
            PenaltyOut(
                name=p.get("name", ""),
                multiplier=float(p.get("multiplier", 1.0)),
                detail=p.get("detail", ""),
            )
            for p in payload.get("penalties", [])
        ],
        base_score=float(payload.get("base_score", 0.0)),
        applicable_weight=float(payload.get("applicable_weight", 0.0)),
        excluded_signals=list(payload.get("excluded_signals", [])),
        trail=list((payload.get("matching") or {}).get("trail", [])),
        remittance_text=_remittance_text(session, result),
        engine_version=result.engine_version,
    )


def _signal(raw: dict[str, Any]) -> SignalOut:
    return SignalOut(
        name=raw.get("name", ""),
        fired=bool(raw.get("fired")),
        applicable=bool(raw.get("applicable", True)),
        value=float(raw.get("value", 0.0)),
        weight=float(raw.get("weight", 0.0)),
        contribution=float(raw.get("contribution", 0.0)),
        detail=raw.get("detail", ""),
    )


def _allocation(allocation) -> AllocationOut:
    invoice = allocation.invoice
    return AllocationOut(
        invoice_id=allocation.invoice_id,
        invoice_number=invoice.invoice_number,
        customer_name=invoice.customer.legal_name if invoice.customer else None,
        due_date=invoice.due_date,
        invoice_amount=Money.of(invoice.amount_paise),
        open_amount=Money.of(invoice.open_amount_paise),
        allocated=Money.of(allocation.allocated_amount_paise),
        deduction=Money.of(allocation.deduction_amount_paise),
        deduction_reason=allocation.deduction_reason,
    )


def _customer_name(result: MatchResult) -> str | None:
    """Whose payment this is, taken from the invoices it settles.

    The transaction itself only carries a payer *string*; the customer is
    whatever the matcher resolved, and the allocations are where that
    resolution is recorded.
    """
    for allocation in result.allocations:
        invoice = allocation.invoice
        if invoice is not None and invoice.customer is not None:
            return invoice.customer.legal_name
    return None


def _remittance_text(session: Session, result: MatchResult) -> str | None:
    """The advice document, so a reviewer can read what the customer said."""
    advice = session.scalar(
        select(Remittance)
        .where(Remittance.bank_transaction_id == result.bank_transaction_id)
        .order_by(Remittance.id)
        .limit(1)
    )
    return advice.raw_text if advice else None


def invoice_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        invoice_number=invoice.invoice_number,
        customer_id=invoice.customer_id,
        customer_name=invoice.customer.legal_name if invoice.customer else None,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        amount=Money.of(invoice.amount_paise),
        open_amount=Money.of(invoice.open_amount_paise),
    )


def customer_out(customer: Customer, open_count: int, open_paise: int) -> CustomerOut:
    return CustomerOut(
        id=customer.id,
        code=customer.code,
        legal_name=customer.legal_name,
        city=customer.city,
        open_invoice_count=open_count,
        open_amount=Money.of(open_paise),
    )
