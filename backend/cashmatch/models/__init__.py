"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogenerate and ``create_all`` both rely on.
"""

from cashmatch.db.base import Base
from cashmatch.models.audit import AuditLog
from cashmatch.models.bank_transaction import BankTransaction
from cashmatch.models.customer import Customer, CustomerAlias
from cashmatch.models.enums import (
    AliasSource,
    DeductionReason,
    ExtractionMethod,
    ExtractionStatus,
    InvoiceStatus,
    MatchDecision,
    MatchStrategy,
    RemittanceLinkSource,
    RemittanceSource,
    ReviewAction,
    TransactionStatus,
)
from cashmatch.models.invoice import OPEN_STATUSES, Invoice
from cashmatch.models.match import MatchAllocation, MatchResult
from cashmatch.models.remittance import Remittance

__all__ = [
    "OPEN_STATUSES",
    "AliasSource",
    "AuditLog",
    "BankTransaction",
    "Base",
    "Customer",
    "CustomerAlias",
    "DeductionReason",
    "ExtractionMethod",
    "ExtractionStatus",
    "Invoice",
    "InvoiceStatus",
    "MatchAllocation",
    "MatchDecision",
    "MatchResult",
    "MatchStrategy",
    "Remittance",
    "RemittanceLinkSource",
    "RemittanceSource",
    "ReviewAction",
    "TransactionStatus",
]
