"""Incoming bank credits -- the money that actually arrived."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashmatch.db.base import Base, TimestampMixin
from cashmatch.db.types import MoneyColumn, portable_enum
from cashmatch.models.enums import TransactionStatus

if TYPE_CHECKING:
    from cashmatch.models.match import MatchResult
    from cashmatch.models.remittance import Remittance


class BankTransaction(Base, TimestampMixin):
    """One credit line from a bank statement.

    This is all the system is initially given: a date, an amount, whatever
    the bank printed as the payer, and a free-text narration. Nothing here
    says which invoices are being settled -- establishing that is the entire
    job.

    Note what is deliberately *absent*: there is no scenario or ground-truth
    column. The answer key for the synthetic data lives in a file on disk
    (``data/ground_truth/``), never in the database, so the matcher is
    structurally incapable of reading it.
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        Index("ix_bank_transactions_status_date", "status", "value_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The bank's own transaction identifier (UTR for NEFT/RTGS in India).
    # Unique, so re-uploading the same statement file is idempotent instead
    # of duplicating every payment.
    statement_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    value_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    amount_paise: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    payer_name_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    payer_name_normalized: Mapped[str] = mapped_column(String(255), index=True, nullable=False)

    # The reference field. Sometimes lists invoice numbers, sometimes holds
    # the customer's internal payment ID, often holds nothing useful at all.
    narration: Mapped[str] = mapped_column(Text, default="", nullable=False)
    normalized_narration: Mapped[str] = mapped_column(Text, default="", nullable=False)

    bank_account: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[TransactionStatus] = mapped_column(
        portable_enum(TransactionStatus), default=TransactionStatus.UNMATCHED, nullable=False
    )

    remittances: Mapped[list[Remittance]] = relationship(back_populates="bank_transaction")
    match_results: Mapped[list[MatchResult]] = relationship(
        back_populates="bank_transaction", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<BankTransaction {self.statement_ref} {self.amount_paise}p "
            f"from {self.payer_name_raw!r}>"
        )
