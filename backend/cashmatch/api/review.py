"""What happens when a human acts on a review item.

Three actions, and the difference between them is what moves in the ledger:

``approve``    the suggestion was right -- post it.
``reject``     the suggestion was wrong -- post nothing, the money stays
               unapplied, and the item leaves the queue.
``reassign``   the suggestion was wrong but the human knows the answer --
               replace the allocation with theirs and post that.

Every one of them stamps the row with who acted and when, and writes an
audit entry. A human decision that cannot be traced back to a person is
worse than no decision, because it looks like the machine did it.

There is also a feedback loop here, and it is the one designed into the
schema in Phase 1: when an analyst confirms a match whose payer spelling the
system did not recognise, that spelling becomes a **learned alias**. The next
payment from the same spelling resolves by index lookup instead of fuzzy
matching. The review queue is not just a safety net -- it is how the system
gets better.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch import ENGINE_VERSION
from cashmatch.api.schemas import ReassignRequest, ReviewRequest
from cashmatch.models import (
    AuditLog,
    Customer,
    CustomerAlias,
    Invoice,
    MatchAllocation,
    MatchResult,
)
from cashmatch.models.enums import (
    AliasSource,
    MatchDecision,
    MatchStrategy,
    ReviewAction,
    TransactionStatus,
)
from cashmatch.money import rupees_to_paise
from cashmatch.normalize import normalize_party_name


class ReviewError(ValueError):
    """The action cannot be applied, with a reason a user can act on."""


@dataclass(slots=True)
class ReviewOutcome:
    result: MatchResult
    cash_posted: bool = False
    invoices_updated: int = 0
    alias_learned: str | None = None
    message: str = ""


def approve(session: Session, result: MatchResult, request: ReviewRequest) -> ReviewOutcome:
    """Accept the engine's suggestion and, by default, post the cash."""
    _guard_unreviewed(result)
    if not result.allocations:
        raise ReviewError(
            "There is nothing to approve: this payment has no suggested allocation. "
            "Use reassign to enter the invoices yourself."
        )

    posted = _post(session, result) if request.post_cash else 0
    _stamp(result, request, ReviewAction.APPROVED, MatchDecision.MANUALLY_APPLIED)
    alias = _learn_alias(session, result)

    _audit(session, result, "review_approved", request.reviewed_by, {"posted": bool(posted)})

    return ReviewOutcome(
        result=result,
        cash_posted=bool(posted),
        invoices_updated=posted,
        alias_learned=alias,
        message=(
            f"Approved. Cash applied to {posted} invoice(s)."
            if posted
            else "Approved. No cash was posted (suggest-only)."
        ),
    )


def reject(session: Session, result: MatchResult, request: ReviewRequest) -> ReviewOutcome:
    """Reject the suggestion. Nothing is posted; the money stays unapplied."""
    _guard_unreviewed(result)

    for allocation in list(result.allocations):
        session.delete(allocation)
    result.allocations = []
    result.matched_amount_paise = 0
    result.unapplied_amount_paise = result.bank_transaction.amount_paise

    _stamp(result, request, ReviewAction.REJECTED, MatchDecision.UNAPPLIED)
    result.strategy = MatchStrategy.NONE
    _audit(session, result, "review_rejected", request.reviewed_by, {})

    return ReviewOutcome(
        result=result,
        message=(
            "Rejected. The suggestion was discarded and the payment remains as unapplied cash."
        ),
    )


def reassign(session: Session, result: MatchResult, request: ReassignRequest) -> ReviewOutcome:
    """Replace the engine's allocation with one the human supplied."""
    _guard_unreviewed(result)

    payment = result.bank_transaction.amount_paise
    lines = _validate_reassignment(session, request, payment)

    for allocation in list(result.allocations):
        session.delete(allocation)
    session.flush()

    total = 0
    for invoice, allocated, deduction, reason in lines:
        session.add(
            MatchAllocation(
                match_result_id=result.id,
                invoice_id=invoice.id,
                allocated_amount_paise=allocated,
                deduction_amount_paise=deduction,
                deduction_reason=reason,
                deduction_note=f"set by {request.reviewed_by}" if deduction else None,
            )
        )
        total += allocated

    result.matched_amount_paise = total
    result.unapplied_amount_paise = payment - total
    result.strategy = MatchStrategy.MANUAL
    _stamp(result, request, ReviewAction.REASSIGNED, MatchDecision.MANUALLY_APPLIED)
    session.flush()
    session.refresh(result)

    posted = _post(session, result) if request.post_cash else 0
    alias = _learn_alias(session, result)
    _audit(
        session,
        result,
        "review_reassigned",
        request.reviewed_by,
        {"invoices": [line[0].invoice_number for line in lines], "posted": bool(posted)},
    )

    return ReviewOutcome(
        result=result,
        cash_posted=bool(posted),
        invoices_updated=posted,
        alias_learned=alias,
        message=f"Reassigned to {len(lines)} invoice(s)." + (" Cash applied." if posted else ""),
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _guard_unreviewed(result: MatchResult) -> None:
    if result.reviewed_at is not None:
        raise ReviewError(
            f"This item was already reviewed by {result.reviewed_by} on "
            f"{result.reviewed_at:%d %b %Y}. Re-run matching to produce a fresh "
            "suggestion if it needs revisiting."
        )


def _validate_reassignment(
    session: Session, request: ReassignRequest, payment_paise: int
) -> list[tuple[Invoice, int, int, object]]:
    """Check a human's allocation before it touches the ledger.

    People make arithmetic mistakes, and a UI can be used wrongly. Every
    failure here produces a message that says what is wrong and what the
    numbers actually are, because "400: Bad Request" helps nobody.
    """
    lines: list[tuple[Invoice, int, int, object]] = []
    total_applied = 0

    for entry in request.allocations:
        invoice = session.get(Invoice, entry.invoice_id)
        if invoice is None:
            raise ReviewError(f"Invoice {entry.invoice_id} does not exist.")

        try:
            allocated = rupees_to_paise(entry.allocated_amount)
            deduction = rupees_to_paise(entry.deduction_amount)
        except (TypeError, ValueError) as exc:
            raise ReviewError(
                f"{invoice.invoice_number}: could not read the amount. {exc}"
            ) from exc

        if allocated <= 0:
            raise ReviewError(
                f"{invoice.invoice_number}: the applied amount must be more than zero. "
                "Remove the line instead of allocating nothing to it."
            )
        if allocated + deduction > invoice.open_amount_paise:
            raise ReviewError(
                f"{invoice.invoice_number}: applying "
                f"{_rupees(allocated)} plus a {_rupees(deduction)} deduction would clear "
                f"more than the {_rupees(invoice.open_amount_paise)} still open on it."
            )

        total_applied += allocated
        lines.append((invoice, allocated, deduction, entry.deduction_reason))

    if total_applied > payment_paise:
        raise ReviewError(
            f"The allocation applies {_rupees(total_applied)} but only "
            f"{_rupees(payment_paise)} was received. Cash that did not arrive cannot "
            "be applied."
        )

    return lines


def _post(session: Session, result: MatchResult) -> int:
    """Apply the cash. Idempotent by the transaction's status."""
    transaction = result.bank_transaction
    if transaction.status is not TransactionStatus.UNMATCHED:
        return 0

    touched = 0
    for allocation in result.allocations:
        invoice = session.get(Invoice, allocation.invoice_id)
        if invoice is None:
            continue
        cleared = allocation.allocated_amount_paise + allocation.deduction_amount_paise
        invoice.open_amount_paise = max(0, invoice.open_amount_paise - cleared)
        invoice.status = invoice.status_for_open_amount()
        touched += 1

    transaction.status = (
        TransactionStatus.MATCHED
        if result.unapplied_amount_paise == 0
        else TransactionStatus.PARTIALLY_APPLIED
    )
    return touched


def _learn_alias(session: Session, result: MatchResult) -> str | None:
    """Teach the system the payer spelling it did not recognise.

    The feedback loop the schema was built for in Phase 1. A human has just
    confirmed that this spelling belongs to this customer; recording it means
    the next payment from the same spelling resolves by index lookup rather
    than fuzzy matching, and never reaches the queue at all.
    """
    transaction = result.bank_transaction
    normalized = transaction.payer_name_normalized
    if not normalized or not result.allocations:
        return None

    customer_id = result.allocations[0].invoice.customer_id

    already_known = session.scalar(
        select(Customer.id).where(
            Customer.id == customer_id, Customer.normalized_name == normalized
        )
    ) or session.scalar(
        select(CustomerAlias.id).where(CustomerAlias.normalized_alias == normalized)
    )
    if already_known:
        return None

    session.add(
        CustomerAlias(
            customer_id=customer_id,
            alias=transaction.payer_name_raw,
            normalized_alias=normalized,
            source=AliasSource.LEARNED,
        )
    )
    session.flush()
    return transaction.payer_name_raw


def _stamp(
    result: MatchResult,
    request: ReviewRequest,
    action: ReviewAction,
    decision: MatchDecision,
) -> None:
    result.reviewed_by = request.reviewed_by
    result.reviewed_at = datetime.now(UTC)
    result.review_action = action
    result.review_note = request.note
    result.decision = decision


def _audit(session: Session, result: MatchResult, action: str, actor: str, extra: dict) -> None:
    session.add(
        AuditLog(
            entity_type="match_result",
            entity_id=result.id,
            action=action,
            actor=f"user:{actor}",
            payload_after={
                "statement_ref": result.bank_transaction.statement_ref,
                "decision": result.decision.value,
                "confidence": str(result.confidence),
                "engine_version": ENGINE_VERSION,
                **extra,
            },
        )
    )


def _rupees(paise: int) -> str:
    from cashmatch.money import format_inr

    return format_inr(paise)


def normalized_payer(name: str) -> str:
    return normalize_party_name(name)
