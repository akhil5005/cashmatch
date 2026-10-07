"""Append-only audit trail."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from cashmatch.db.base import Base, TimestampMixin
from cashmatch.db.types import PortableJSON


class AuditLog(Base, TimestampMixin):
    """Who did what, to which record, and what changed.

    Cash application posts real money against real customer accounts, and
    finance systems get audited. When someone asks six months later why
    Rs.4,18,500 landed on INV-00042, this table has to answer: which rule
    fired, which build of the engine, or which analyst overrode it.

    ``entity_type`` + ``entity_id`` is a deliberate soft pointer rather than
    a foreign key -- the log must survive its subject being deleted, and it
    has to cover several tables with one shape.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(nullable=False)

    # e.g. "auto_applied", "review_approved", "reassigned", "alias_learned"
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    # "system:matcher@0.1.0" or "user:akhil" -- the prefix makes automated and
    # human actions trivially separable when reporting.
    actor: Mapped[str] = mapped_column(String(100), nullable=False)

    payload_before: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)
    payload_after: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} on {self.entity_type}:{self.entity_id} by {self.actor}>"
