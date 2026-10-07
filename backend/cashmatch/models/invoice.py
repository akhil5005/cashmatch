"""Invoices -- the open items a payment has to be matched against."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashmatch.db.base import Base, TimestampMixin, UpdatedAtMixin
from cashmatch.db.types import MoneyColumn, portable_enum
from cashmatch.models.enums import InvoiceStatus

if TYPE_CHECKING:
    from cashmatch.models.customer import Customer

OPEN_STATUSES = (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID)


class Invoice(Base, TimestampMixin, UpdatedAtMixin):
    """A bill issued to a customer.

    The field that matters most is ``open_amount_paise``: the part still
    unpaid. In Order-to-Cash language an invoice with a non-zero open amount
    is an **open item**, and the whole matching problem is "which open items
    does this bank credit settle?".

    ``amount_paise`` stays fixed at the original value; only the open amount
    moves as cash is applied. Keeping both means a partially paid invoice
    still knows what it was originally worth.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint("open_amount_paise >= 0", name="open_amount_non_negative"),
        CheckConstraint("open_amount_paise <= amount_paise", name="open_amount_within_amount"),
        # The matcher's hottest query: open items for one customer.
        Index("ix_invoices_customer_status", "customer_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    # "INV-00042" and "inv 42" both normalise to "INV42", so a typo'd
    # reference on a bank narration still lands on the right row.
    normalized_number: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )

    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)

    amount_paise: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    open_amount_paise: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        portable_enum(InvoiceStatus), default=InvoiceStatus.OPEN, nullable=False
    )

    # Customers frequently quote their own purchase-order number instead of
    # our invoice number, so it is a secondary reference the matcher can use.
    po_number: Mapped[str | None] = mapped_column(String(64), index=True)

    customer: Mapped[Customer] = relationship(back_populates="invoices")

    @property
    def is_open(self) -> bool:
        """True when the invoice still has money outstanding."""
        return self.status in OPEN_STATUSES and self.open_amount_paise > 0

    def status_for_open_amount(self) -> InvoiceStatus:
        """The status implied by the current open amount.

        Kept as a plain method rather than a database trigger: the rule is
        then visible in Python, unit-testable, and identical on SQLite and
        Postgres. Callers that move money are responsible for applying it.
        """
        if self.open_amount_paise == 0:
            return InvoiceStatus.PAID
        if self.open_amount_paise < self.amount_paise:
            return InvoiceStatus.PARTIALLY_PAID
        return InvoiceStatus.OPEN

    def __repr__(self) -> str:
        return (
            f"<Invoice {self.invoice_number} open={self.open_amount_paise}p "
            f"of {self.amount_paise}p {self.status}>"
        )
