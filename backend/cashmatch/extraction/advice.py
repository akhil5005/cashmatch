"""The bridge from Phase 4 back into the Phase 3 cascade.

Extracted advice is a *stronger* signal than the bank narration: the customer
wrote it deliberately, it names invoices in full, and it usually states the
split. So it goes in front of the narration-reference step in the cascade.

Only advice already linked to a transaction is fed in. Pairing an unlinked
advice document with the payment it describes is a separate matching problem
in its own right -- out of scope here, and flagged at the end of Phase 4
rather than quietly half-solved.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch.extraction.schema import ExtractedRemittance, Reconciliation
from cashmatch.models import BankTransaction, Remittance
from cashmatch.models.enums import ExtractionStatus


@dataclass(slots=True, frozen=True)
class LinkedAdvice:
    """One extracted advice document, attached to a bank credit."""

    remittance_id: int
    source_filename: str | None
    method: str
    payload: ExtractedRemittance

    @property
    def references(self) -> list[str]:
        return self.payload.references

    @property
    def reconciled(self) -> bool:
        return self.payload.reconciliation is Reconciliation.TIES


class AdviceIndex:
    """Extracted advice, keyed by the bank reference it belongs to."""

    def __init__(self, by_statement_ref: dict[str, list[LinkedAdvice]] | None = None) -> None:
        self._by_ref: dict[str, list[LinkedAdvice]] = by_statement_ref or {}

    @classmethod
    def load(cls, session: Session) -> AdviceIndex:
        """Read every successfully extracted, linked advice document."""
        rows = session.execute(
            select(Remittance, BankTransaction.statement_ref)
            .join(BankTransaction, Remittance.bank_transaction_id == BankTransaction.id)
            .where(Remittance.extraction_status == ExtractionStatus.EXTRACTED)
            .order_by(Remittance.id)
        ).all()

        index: dict[str, list[LinkedAdvice]] = {}
        for remittance, statement_ref in rows:
            advice = _from_row(remittance)
            if advice is not None:
                index.setdefault(statement_ref, []).append(advice)

        return cls(index)

    @classmethod
    def empty(cls) -> AdviceIndex:
        return cls({})

    def for_transaction(self, statement_ref: str) -> list[LinkedAdvice]:
        return self._by_ref.get(statement_ref, [])

    def __len__(self) -> int:
        return sum(len(items) for items in self._by_ref.values())

    def add(self, statement_ref: str, advice: LinkedAdvice) -> None:
        """Register one advice document. Used by tests building an index."""
        self._by_ref.setdefault(statement_ref, []).append(advice)


def _from_row(remittance: Remittance) -> LinkedAdvice | None:
    """Rebuild the validated payload stored on the row.

    A payload written by an older build may no longer satisfy the current
    schema. Skipping it is right: a half-understood extraction must not
    influence where money goes. Re-running ``cashmatch extract --force``
    regenerates it.
    """
    stored = remittance.extracted_payload or {}
    body = stored.get("payload")
    if not body:
        return None

    try:
        payload = ExtractedRemittance.model_validate(body)
    except Exception:
        return None

    if payload.is_empty:
        return None

    return LinkedAdvice(
        remittance_id=remittance.id,
        source_filename=remittance.source_filename,
        method=str(stored.get("method", "unknown")),
        payload=payload,
    )
