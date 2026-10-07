"""Domain objects the matching engine produces.

Phase 3 deliberately stops short of a *decision*. It answers "which invoices
could this payment be settling, and by what reasoning?" -- not "should we post
it automatically?". Scoring those signals into a confidence, thresholding it
and persisting the result is Phase 5's job, and keeping the two apart means
the thresholds can be re-tuned without re-running the search.

Everything here is a plain dataclass so the engine stays pure and testable
without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from cashmatch.models.enums import DeductionReason, MatchStrategy


class IdentificationMethod(StrEnum):
    """How the payer was resolved to a customer, strongest first."""

    EXACT = "exact"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    #: Recovered from an invoice reference in the narration rather than the name.
    REFERENCE = "reference"
    NONE = "none"
    #: Several customers scored alike. Refusing to pick is the correct answer.
    AMBIGUOUS = "ambiguous"


@dataclass(slots=True, frozen=True)
class OpenItem:
    """One open invoice, flattened for the matcher's hot path."""

    invoice_id: int
    invoice_number: str
    normalized_number: str
    customer_id: int
    invoice_date: date
    due_date: date
    open_amount_paise: int


@dataclass(slots=True, frozen=True)
class ReferenceToken:
    """A fragment of narration that might be an invoice reference."""

    raw: str
    normalized: str
    digits: str


@dataclass(slots=True)
class CustomerIdentification:
    """The outcome of step 2 of the pipeline."""

    customer_id: int | None
    method: IdentificationMethod
    score: float
    matched_on: str | None = None
    runner_up: str | None = None
    runner_up_score: float | None = None
    considered: int = 0

    @property
    def resolved(self) -> bool:
        return self.customer_id is not None


@dataclass(slots=True)
class Signal:
    """One piece of evidence, recorded whether it fired or not.

    The ones that did *not* fire matter as much as the ones that did: a
    reviewer needs to know the matcher looked for a reference and found none,
    rather than wondering whether it looked at all.
    """

    name: str
    fired: bool
    detail: str
    raw: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fired": self.fired, "detail": self.detail, "raw": self.raw}


@dataclass(slots=True)
class CandidateAllocation:
    """Proposed application of cash to one invoice."""

    item: OpenItem
    allocated_amount_paise: int
    deduction_amount_paise: int = 0
    deduction_reason: DeductionReason | None = None
    #: True only when a document named *this* invoice as carrying the claim.
    #: A document-level "Rs. 4,900 deducted" with no invoice named does not
    #: count: the reason is known, the attribution is still the matcher's
    #: guess, and scoring has to be able to tell those apart.
    bearer_asserted: bool = False

    @property
    def settles_in_full(self) -> bool:
        return (
            self.allocated_amount_paise + self.deduction_amount_paise == self.item.open_amount_paise
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoice_number": self.item.invoice_number,
            "allocated_amount_paise": self.allocated_amount_paise,
            "deduction_amount_paise": self.deduction_amount_paise,
            "deduction_reason": (self.deduction_reason.value if self.deduction_reason else None),
            "bearer_asserted": self.bearer_asserted,
        }


@dataclass(slots=True)
class MatchCandidate:
    """One way the payment could be applied, and how we got there."""

    strategy: MatchStrategy
    allocations: list[CandidateAllocation]
    signals: list[Signal] = field(default_factory=list)
    note: str = ""

    @property
    def allocated_paise(self) -> int:
        return sum(a.allocated_amount_paise for a in self.allocations)

    @property
    def deduction_paise(self) -> int:
        return sum(a.deduction_amount_paise for a in self.allocations)

    @property
    def invoice_numbers(self) -> list[str]:
        return [a.item.invoice_number for a in self.allocations]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "allocations": [a.as_dict() for a in self.allocations],
            "allocated_amount_paise": self.allocated_paise,
            "deduction_amount_paise": self.deduction_paise,
            "note": self.note,
            "signals": [s.as_dict() for s in self.signals],
        }


@dataclass(slots=True)
class MatchOutcome:
    """Everything the engine concluded about one bank transaction."""

    statement_ref: str
    amount_paise: int
    customer: CustomerIdentification
    references: list[ReferenceToken] = field(default_factory=list)
    candidates: list[MatchCandidate] = field(default_factory=list)
    #: Every pipeline step that ran, in order, firing or not.
    trail: list[Signal] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    @property
    def is_ambiguous(self) -> bool:
        """More than one way to apply the money, with nothing to separate them."""
        return len(self.candidates) > 1

    @property
    def strategy(self) -> MatchStrategy:
        return self.candidates[0].strategy if self.candidates else MatchStrategy.NONE

    @property
    def residual_paise(self) -> int:
        """Money the leading candidate could not place."""
        if not self.candidates:
            return self.amount_paise
        return self.amount_paise - self.candidates[0].allocated_paise

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form. Becomes the seed of Phase 5's explanation JSON."""
        return {
            "statement_ref": self.statement_ref,
            "amount_paise": self.amount_paise,
            "customer": {
                "customer_id": self.customer.customer_id,
                "method": self.customer.method.value,
                "score": round(self.customer.score, 4),
                "matched_on": self.customer.matched_on,
                "runner_up": self.customer.runner_up,
                "runner_up_score": (
                    round(self.customer.runner_up_score, 4)
                    if self.customer.runner_up_score is not None
                    else None
                ),
            },
            "references": [token.normalized for token in self.references],
            "strategy": self.strategy.value,
            "ambiguous": self.is_ambiguous,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "trail": [signal.as_dict() for signal in self.trail],
            # Diagnostics carry a date, which JSONB will not take raw. This
            # payload goes into a database column, so everything in it has
            # to survive json.dumps.
            "diagnostics": {
                key: (value.isoformat() if hasattr(value, "isoformat") else value)
                for key, value in self.diagnostics.items()
            },
        }
