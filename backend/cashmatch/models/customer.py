"""Customers and the many ways their names reach us."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashmatch.db.base import Base, TimestampMixin
from cashmatch.db.types import portable_enum
from cashmatch.models.enums import AliasSource

if TYPE_CHECKING:
    from cashmatch.models.invoice import Invoice


class Customer(Base, TimestampMixin):
    """A business that buys from us and therefore owes us money.

    ``normalized_name`` is the matcher's entry point: payer names from the
    bank are normalised the same way, so the common case is an index lookup
    rather than a fuzzy scan over every customer.
    """

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    city: Mapped[str | None] = mapped_column(String(100))

    # Net terms. Drives due dates, and in Phase 3 narrows the date window
    # within which an invoice is a plausible target for a payment.
    payment_terms_days: Mapped[int] = mapped_column(default=30, nullable=False)

    aliases: Mapped[list[CustomerAlias]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", lazy="selectin"
    )
    invoices: Mapped[list[Invoice]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Customer {self.code} {self.legal_name!r}>"


class CustomerAlias(Base, TimestampMixin):
    """One known spelling of a customer's name.

    A separate table rather than a column because the variations are
    open-ended: every remitting bank formats the payer field differently, and
    each new spelling we confirm is another row. Checking aliases before
    reaching for fuzzy matching keeps the common case exact and cheap.
    """

    __tablename__ = "customer_aliases"
    __table_args__ = (
        UniqueConstraint("customer_id", "normalized_alias", name="uq_alias_per_customer"),
        Index("ix_customer_aliases_normalized_alias", "normalized_alias"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[AliasSource] = mapped_column(
        portable_enum(AliasSource), default=AliasSource.ERP, nullable=False
    )

    customer: Mapped[Customer] = relationship(back_populates="aliases")

    def __repr__(self) -> str:
        return f"<CustomerAlias {self.alias!r} -> customer {self.customer_id}>"
