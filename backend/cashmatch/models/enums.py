"""Every status and category enum in the domain, in one place.

Stored as strings (see ``db.types.portable_enum``), so a raw SQL query shows
``'needs_review'`` rather than an opaque integer.
"""

from __future__ import annotations

from enum import StrEnum


class InvoiceStatus(StrEnum):
    """Lifecycle of an invoice from the receivables side.

    ``OPEN`` and ``PARTIALLY_PAID`` are the "open items" -- the set the
    matcher searches. ``PAID`` and ``WRITTEN_OFF`` are closed and excluded.
    """

    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    WRITTEN_OFF = "written_off"


class AliasSource(StrEnum):
    """Where a customer name variant came from.

    ``LEARNED`` is the feedback hook: when an analyst confirms a review item,
    the payer spelling that confused us becomes a known alias.
    """

    ERP = "erp"
    LEARNED = "learned"
    MANUAL = "manual"


class TransactionStatus(StrEnum):
    """Processing state of an incoming bank credit."""

    UNMATCHED = "unmatched"
    MATCHED = "matched"
    PARTIALLY_APPLIED = "partially_applied"
    IGNORED = "ignored"


class RemittanceSource(StrEnum):
    """Channel the remittance advice arrived through."""

    EMAIL = "email"
    PDF = "pdf"
    CSV = "csv"
    MANUAL = "manual"


class RemittanceLinkSource(StrEnum):
    """How the advice got attached to a bank transaction.

    ``EXPLICIT`` means the advice itself carried the bank reference.
    ``INFERRED`` means we guessed from payer and amount -- a weaker signal
    that the confidence score must account for.
    """

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    UNLINKED = "unlinked"


class ExtractionStatus(StrEnum):
    """Progress of Phase 4's structured extraction over the raw advice."""

    PENDING = "pending"
    EXTRACTED = "extracted"
    FAILED = "failed"


class ExtractionMethod(StrEnum):
    """Which extractor produced the payload. Recorded because an LLM result
    and a regex result do not deserve the same confidence."""

    NONE = "none"
    LLM = "llm"
    REGEX_FALLBACK = "regex_fallback"
    MOCK = "mock"


class MatchDecision(StrEnum):
    """The system's verdict on a payment.

    ``AUTO_APPLIED``    confident enough to post cash with no human.
    ``NEEDS_REVIEW``    plausible candidates, not confident enough.
    ``UNAPPLIED``       no credible candidate. The money sits as unapplied cash.
    ``REJECTED``        a human looked at a suggestion and said no.
    ``MANUALLY_APPLIED`` a human chose the allocation themselves.
    """

    AUTO_APPLIED = "auto_applied"
    NEEDS_REVIEW = "needs_review"
    UNAPPLIED = "unapplied"
    REJECTED = "rejected"
    MANUALLY_APPLIED = "manually_applied"


class MatchStrategy(StrEnum):
    """Which step of the Phase 3 pipeline produced the candidate set.

    Recorded per result so evaluation can report accuracy per strategy, not
    just overall -- if subset-sum is the one making mistakes, that shows up.
    """

    NONE = "none"
    REFERENCE_EXACT = "reference_exact"
    AMOUNT_EXACT = "amount_exact"
    SUBSET_SUM = "subset_sum"
    SHORT_PAY = "short_pay"
    REMITTANCE_GUIDED = "remittance_guided"
    MANUAL = "manual"


class DeductionReason(StrEnum):
    """Why a customer paid less than the invoice.

    In Order-to-Cash these are *deductions* (or "chargebacks"). They are not
    bad debt -- the customer is asserting a claim. Classifying the reason is
    what lets the claim be routed to the right team.
    """

    DAMAGE = "damage"
    PROMO = "promo"
    PRICING = "pricing"
    FREIGHT = "freight"
    TDS = "tds"
    SHORT_SHIP = "short_ship"
    UNKNOWN = "unknown"


class ReviewAction(StrEnum):
    """What the human did with a review item."""

    APPROVED = "approved"
    REJECTED = "rejected"
    REASSIGNED = "reassigned"
