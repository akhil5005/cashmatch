"""Request and response shapes for the API.

One rule governs everything here: **money crosses the wire as integer paise,
with a formatted string beside it.** A JSON number for an amount invites a
float on the other side, and a UI that does arithmetic on floats will
eventually show a customer a balance that is one paise wrong and impossible
to explain. The integer is authoritative; the string is for display.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashmatch.models.enums import (
    DeductionReason,
    MatchDecision,
    MatchStrategy,
    ReviewAction,
)
from cashmatch.money import format_inr


class Money(BaseModel):
    """An amount, twice: exact for arithmetic, formatted for people."""

    paise: int
    display: str

    @classmethod
    def of(cls, paise: int) -> Money:
        return cls(paise=paise, display=format_inr(paise))


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


class AllocationOut(BaseModel):
    invoice_id: int
    invoice_number: str
    customer_name: str | None = None
    due_date: date | None = None
    invoice_amount: Money | None = None
    open_amount: Money | None = None
    allocated: Money
    deduction: Money
    deduction_reason: DeductionReason | None = None


class ResultSummary(BaseModel):
    """One row in the review queue."""

    id: int
    statement_ref: str
    value_date: date
    amount: Money
    payer_name: str
    narration: str
    customer_name: str | None = None

    decision: MatchDecision
    confidence: float
    strategy: MatchStrategy
    reason_text: str
    matched_amount: Money
    unapplied_amount: Money
    invoice_count: int
    invoice_numbers: list[str]
    has_deduction: bool

    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_action: ReviewAction | None = None
    created_at: datetime


class SignalOut(BaseModel):
    name: str
    fired: bool
    applicable: bool = True
    value: float = 0.0
    weight: float = 0.0
    contribution: float = 0.0
    detail: str = ""


class PenaltyOut(BaseModel):
    name: str
    multiplier: float
    detail: str


class ResultDetail(ResultSummary):
    """One review item, with everything needed to judge it.

    The explanation is the point. A reviewer who cannot see why the engine
    proposed this has to redo the analysis from scratch, which erases the
    saving that routing to review was supposed to deliver.
    """

    allocations: list[AllocationOut] = Field(default_factory=list)
    signals: list[SignalOut] = Field(default_factory=list)
    penalties: list[PenaltyOut] = Field(default_factory=list)
    base_score: float = 0.0
    applicable_weight: float = 0.0
    excluded_signals: list[str] = Field(default_factory=list)
    #: Every pipeline step that ran, firing or not.
    trail: list[dict[str, Any]] = Field(default_factory=list)
    remittance_text: str | None = None
    engine_version: str = ""


class ResultPage(BaseModel):
    items: list[ResultSummary]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class DecisionCount(BaseModel):
    decision: MatchDecision
    count: int
    value: Money


class MetricsOut(BaseModel):
    """What the dashboard shows."""

    transactions: int
    decided: int
    auto_match_rate: float
    review_count: int
    unapplied_count: int
    unapplied_value: Money
    auto_applied_value: Money
    total_value: Money
    by_decision: list[DecisionCount] = Field(default_factory=list)
    by_strategy: dict[str, int] = Field(default_factory=dict)

    open_receivables: Money
    open_invoice_count: int
    customers: int

    #: Only available when a ground-truth file is present, which means a
    #: generated demo dataset. Production has no answer key, and a precision
    #: figure invented without one would be a lie.
    precision: float | None = None
    precision_basis: str | None = None

    reviewed_by_humans: int = 0


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    """Who is acting, and optionally why."""

    reviewed_by: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    #: Approving posts the cash by default. A client running in suggest-only
    #: mode can turn that off without losing the audit trail.
    post_cash: bool = True


class ReassignLine(BaseModel):
    invoice_id: int
    #: Amounts arrive as strings for the same reason the LLM schema uses
    #: strings: a JSON number is a float waiting to happen.
    allocated_amount: str
    deduction_amount: str = "0"
    deduction_reason: DeductionReason | None = None


class ReassignRequest(ReviewRequest):
    """A human overriding the engine with their own allocation."""

    allocations: list[ReassignLine] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_invoices(self) -> Self:
        seen = [line.invoice_id for line in self.allocations]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "The same invoice appears twice in this allocation. Combine the lines "
                "into one instead."
            )
        return self


class ReviewResponse(BaseModel):
    result: ResultDetail
    cash_posted: bool
    invoices_updated: int
    alias_learned: str | None = None
    message: str


class UploadResponse(BaseModel):
    filename: str
    rows_read: int
    created: int
    skipped: int
    #: Row-level problems, so a user can fix their file rather than guess.
    errors: list[str] = Field(default_factory=list)
    message: str


class PipelineResponse(BaseModel):
    transactions: int
    by_decision: dict[str, int] = Field(default_factory=dict)
    auto_match_rate: float = 0.0
    elapsed_seconds: float = 0.0
    posted: bool = False
    message: str


class InvoiceOut(BaseModel):
    """An open item, for the reassign picker."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    invoice_number: str
    customer_id: int
    customer_name: str | None = None
    invoice_date: date
    due_date: date
    amount: Money
    open_amount: Money


class CustomerOut(BaseModel):
    id: int
    code: str
    legal_name: str
    city: str | None = None
    open_invoice_count: int = 0
    open_amount: Money
