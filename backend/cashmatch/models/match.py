"""Match decisions and the invoice-level allocations that implement them."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashmatch.db.base import Base, TimestampMixin
from cashmatch.db.types import MoneyColumn, PortableJSON, portable_enum
from cashmatch.models.enums import (
    DeductionReason,
    MatchDecision,
    MatchStrategy,
    ReviewAction,
)

if TYPE_CHECKING:
    from cashmatch.models.bank_transaction import BankTransaction
    from cashmatch.models.invoice import Invoice


class MatchResult(Base, TimestampMixin):
    """One decision about one bank transaction.

    Every field here exists to answer a question an auditor or an analyst
    will eventually ask:

    * ``decision``    -- what did the system do?
    * ``confidence``  -- how sure was it?
    * ``strategy``    -- which rule got it there?
    * ``explanation`` -- which signals fired, with what weight?
    * ``reason_text`` -- say that in one sentence a human can read.
    * ``engine_version`` -- which build of the matcher decided this?

    An unexplainable automated decision is unusable in finance, so
    explainability is a schema-level commitment rather than a nice-to-have.
    """

    __tablename__ = "match_results"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
        CheckConstraint("matched_amount_paise >= 0", name="matched_amount_non_negative"),
        Index("ix_match_results_decision_created", "decision", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="CASCADE"), index=True, nullable=False
    )

    decision: Mapped[MatchDecision] = mapped_column(portable_enum(MatchDecision), nullable=False)
    # Confidence is a ratio, not money, so fixed-point Decimal is the right
    # type here. Numeric(5,4) gives 0.0000 - 1.0000 with no float anywhere.
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("0"), nullable=False)
    strategy: Mapped[MatchStrategy] = mapped_column(
        portable_enum(MatchStrategy), default=MatchStrategy.NONE, nullable=False
    )

    matched_amount_paise: Mapped[int] = mapped_column(MoneyColumn, default=0, nullable=False)
    # Money the payment could not place. In AR this is "unapplied cash": it
    # sits on the customer's account inflating their apparent balance until
    # someone works out where it belongs.
    unapplied_amount_paise: Mapped[int] = mapped_column(MoneyColumn, default=0, nullable=False)

    explanation: Mapped[dict[str, Any]] = mapped_column(PortableJSON, default=dict, nullable=False)
    reason_text: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- Human review trail (Phase 7) --------------------------------------
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_action: Mapped[ReviewAction | None] = mapped_column(portable_enum(ReviewAction))
    review_note: Mapped[str | None] = mapped_column(Text)

    bank_transaction: Mapped[BankTransaction] = relationship(back_populates="match_results")
    allocations: Mapped[list[MatchAllocation]] = relationship(
        back_populates="match_result", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def allocated_total_paise(self) -> int:
        """Sum of cash allocated across lines. Should equal
        ``matched_amount_paise``; the test suite asserts it does."""
        return sum(a.allocated_amount_paise for a in self.allocations)

    @property
    def deduction_total_paise(self) -> int:
        """Sum of short-paid amounts across lines."""
        return sum(a.deduction_amount_paise for a in self.allocations)

    def __repr__(self) -> str:
        return (
            f"<MatchResult txn={self.bank_transaction_id} {self.decision} "
            f"conf={self.confidence} via {self.strategy}>"
        )


class MatchAllocation(Base, TimestampMixin):
    """One line of a decision: this much of the payment clears that invoice.

    A payment covering three invoices produces three allocations. Splitting
    at line level is what makes partial payments, bundled payments and
    short-pays all representable with one structure.
    """

    __tablename__ = "match_allocations"
    __table_args__ = (
        UniqueConstraint("match_result_id", "invoice_id", name="uq_allocation_per_invoice"),
        CheckConstraint("allocated_amount_paise >= 0", name="allocated_non_negative"),
        CheckConstraint("deduction_amount_paise >= 0", name="deduction_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_result_id: Mapped[int] = mapped_column(
        ForeignKey("match_results.id", ondelete="CASCADE"), index=True, nullable=False
    )
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True, nullable=False
    )

    allocated_amount_paise: Mapped[int] = mapped_column(MoneyColumn, nullable=False)

    # The gap between what the invoice asked for and what the customer paid,
    # when that gap is a deliberate claim rather than an underpayment.
    deduction_amount_paise: Mapped[int] = mapped_column(MoneyColumn, default=0, nullable=False)
    deduction_reason: Mapped[DeductionReason | None] = mapped_column(portable_enum(DeductionReason))
    deduction_note: Mapped[str | None] = mapped_column(String(255))

    match_result: Mapped[MatchResult] = relationship(back_populates="allocations")
    invoice: Mapped[Invoice] = relationship()

    def __repr__(self) -> str:
        return (
            f"<MatchAllocation inv={self.invoice_id} "
            f"applied={self.allocated_amount_paise}p "
            f"deducted={self.deduction_amount_paise}p>"
        )
