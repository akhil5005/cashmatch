"""Remittance advice -- the customer telling us what they paid for."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cashmatch.db.base import Base, TimestampMixin, utcnow
from cashmatch.db.types import PortableJSON, portable_enum
from cashmatch.models.enums import (
    ExtractionMethod,
    ExtractionStatus,
    RemittanceLinkSource,
    RemittanceSource,
)

if TYPE_CHECKING:
    from cashmatch.models.bank_transaction import BankTransaction


class Remittance(Base, TimestampMixin):
    """A payment advice: an email, a PDF, or a spreadsheet saying
    "this transfer covers invoices X, Y and Z, less a damage claim".

    The awkward part of the business problem is that advice travels on a
    different road from the money. It arrives by email to a shared inbox,
    maybe a day early, maybe three days late, often with no bank reference
    tying it to the transfer. So ``bank_transaction_id`` is **nullable by
    design** -- pairing advice with payment is itself part of the work, not a
    precondition for storing it.
    """

    __tablename__ = "remittances"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_type: Mapped[RemittanceSource] = mapped_column(
        portable_enum(RemittanceSource), nullable=False
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transactions.id", ondelete="SET NULL"), index=True
    )
    link_source: Mapped[RemittanceLinkSource] = mapped_column(
        portable_enum(RemittanceLinkSource),
        default=RemittanceLinkSource.UNLINKED,
        nullable=False,
    )

    # --- Filled by Phase 4's extractor -------------------------------------
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        portable_enum(ExtractionStatus), default=ExtractionStatus.PENDING, nullable=False
    )
    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        portable_enum(ExtractionMethod), default=ExtractionMethod.NONE, nullable=False
    )
    # The validated Pydantic model dumped to JSON. Stored rather than
    # recomputed so an extraction can be audited and replayed without
    # re-calling the LLM.
    extracted_payload: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)
    extraction_error: Mapped[str | None] = mapped_column(Text)

    bank_transaction: Mapped[BankTransaction | None] = relationship(back_populates="remittances")

    def __repr__(self) -> str:
        return f"<Remittance {self.id} {self.source_type} txn={self.bank_transaction_id}>"
