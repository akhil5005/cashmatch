"""Writing decisions to the database, and optionally posting the cash.

Two steps kept deliberately separate:

**Recording the decision** writes a ``match_results`` row with its
allocations and the full explanation. Nothing about the ledger changes. This
is always safe to re-run.

**Posting the cash** reduces each invoice's open amount and marks the
transaction matched. It changes the customer's balance, so it is opt-in,
guarded against double-application, and audited.

That split is not ceremony. A real first deployment runs in suggest-only
mode for weeks while the client watches the precision number, and an engine
that can only run in post mode cannot be deployed that way at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch import ENGINE_VERSION
from cashmatch.models import AuditLog, BankTransaction, Invoice, MatchAllocation, MatchResult
from cashmatch.models.enums import MatchDecision, TransactionStatus
from cashmatch.scoring.scorer import Scored


@dataclass(slots=True)
class PostingOutcome:
    """What posting one decision did to the ledger."""

    invoices_touched: int = 0
    cash_applied_paise: int = 0
    deductions_booked_paise: int = 0


def clear_previous_results(session: Session, statement_refs: list[str]) -> int:
    """Remove earlier decisions for these transactions.

    Re-running the engine must replace its own previous verdict, not stack a
    second one beside it. Allocations go with it by cascade. Decisions a
    human touched are left alone -- overwriting an analyst's judgement with
    a fresh machine guess would be the worst behaviour available here.
    """
    if not statement_refs:
        return 0

    rows = session.scalars(
        select(MatchResult)
        .join(BankTransaction, MatchResult.bank_transaction_id == BankTransaction.id)
        .where(BankTransaction.statement_ref.in_(statement_refs))
        .where(MatchResult.reviewed_at.is_(None))
    ).all()

    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def record_decision(session: Session, transaction: BankTransaction, scored: Scored) -> MatchResult:
    """Write one decision and its allocations. Does not touch the ledger."""
    candidate = scored.candidate

    result = MatchResult(
        bank_transaction_id=transaction.id,
        decision=scored.decision,
        confidence=scored.confidence_decimal,
        strategy=scored.outcome.strategy,
        matched_amount_paise=candidate.allocated_paise if candidate else 0,
        unapplied_amount_paise=scored.outcome.residual_paise,
        explanation=scored.explanation(),
        reason_text=scored.reason_text,
        engine_version=ENGINE_VERSION,
    )
    session.add(result)
    session.flush()

    if candidate is not None:
        for allocation in candidate.allocations:
            session.add(
                MatchAllocation(
                    match_result_id=result.id,
                    invoice_id=allocation.item.invoice_id,
                    allocated_amount_paise=allocation.allocated_amount_paise,
                    deduction_amount_paise=allocation.deduction_amount_paise,
                    deduction_reason=allocation.deduction_reason,
                    deduction_note=(
                        allocation.deduction_reason.value if allocation.deduction_reason else None
                    ),
                )
            )

    return result


def post_cash(
    session: Session, transaction: BankTransaction, result: MatchResult
) -> PostingOutcome:
    """Apply an auto-applied decision to the ledger.

    Reduces each invoice's open amount, moves its status, and marks the
    transaction. Idempotent by the transaction's own status: a payment
    already matched is never posted twice, which is the whole reason that
    column exists.
    """
    posted = PostingOutcome()

    if result.decision is not MatchDecision.AUTO_APPLIED:
        return posted
    if transaction.status is not TransactionStatus.UNMATCHED:
        # Already posted by an earlier run. Doing it again would clear the
        # invoices twice and corrupt the customer's balance.
        return posted

    for allocation in result.allocations:
        invoice = session.get(Invoice, allocation.invoice_id)
        if invoice is None:
            continue

        cleared = allocation.allocated_amount_paise + allocation.deduction_amount_paise
        invoice.open_amount_paise = max(0, invoice.open_amount_paise - cleared)
        invoice.status = invoice.status_for_open_amount()

        posted.invoices_touched += 1
        posted.cash_applied_paise += allocation.allocated_amount_paise
        posted.deductions_booked_paise += allocation.deduction_amount_paise

    transaction.status = (
        TransactionStatus.MATCHED
        if result.unapplied_amount_paise == 0
        else TransactionStatus.PARTIALLY_APPLIED
    )

    session.add(
        AuditLog(
            entity_type="match_result",
            entity_id=result.id,
            action="cash_posted",
            actor=f"system:{ENGINE_VERSION}",
            payload_after={
                "statement_ref": transaction.statement_ref,
                "confidence": str(result.confidence),
                "strategy": result.strategy.value,
                "invoices": [a.invoice_id for a in result.allocations],
                "cash_applied_paise": posted.cash_applied_paise,
                "deductions_booked_paise": posted.deductions_booked_paise,
            },
        )
    )
    session.flush()
    return posted


def audit_decision(session: Session, transaction: BankTransaction, result: MatchResult) -> None:
    """Record that a decision was reached, whatever the decision was.

    Finance software is audited, and "the system looked at this payment and
    decided not to apply it" is as much a decision as applying it.
    """
    session.add(
        AuditLog(
            entity_type="match_result",
            entity_id=result.id,
            action=f"decided_{result.decision.value}",
            actor=f"system:{ENGINE_VERSION}",
            payload_after={
                "statement_ref": transaction.statement_ref,
                "decision": result.decision.value,
                "confidence": str(result.confidence),
                "strategy": result.strategy.value,
                "reason": result.reason_text,
            },
        )
    )
