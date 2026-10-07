"""Mapping free-text claim wording onto reason codes.

Customers do not write ``short_ship``. They write "12 cases short received",
"quantity mismatch on delivery", or just "qty". Classifying that into a code
is what lets the claim be routed to the right team -- logistics for a damage
claim, trade marketing for a scheme credit, pricing for a rate difference.

Shared by both extractors on purpose. The LLM returns the document's own
words and this normalises them, so an LLM result and a regex result produce
the same vocabulary and Phase 6 can compare them.
"""

from __future__ import annotations

import re

from cashmatch.models.enums import DeductionReason

# Ordered most specific first: "short supply" must beat a bare "supply", and
# "rate difference" must not be caught by a generic money word.
_PATTERNS: tuple[tuple[DeductionReason, tuple[str, ...]], ...] = (
    (
        DeductionReason.DAMAGE,
        ("damage", "damaged", "breakage", "broken", "torn", "leak", "spoil", "dmg"),
    ),
    (
        DeductionReason.SHORT_SHIP,
        (
            "short received",
            "short supply",
            "short ship",
            "shortage",
            "short qty",
            "quantity mismatch",
            "qty mismatch",
            "cases short",
            "not received",
        ),
    ),
    (
        DeductionReason.PROMO,
        ("scheme", "promo", "promotion", "offer", "festival", "trade discount", "rebate"),
    ),
    (
        DeductionReason.PRICING,
        (
            "rate diff",
            "rate difference",
            "price diff",
            "pricing",
            "price protection",
            "old rate",
            "revised price",
            "mrp diff",
        ),
    ),
    (
        DeductionReason.FREIGHT,
        ("freight", "transport", "octroi", "cartage", "frt", "delivery charge"),
    ),
    (
        DeductionReason.TDS,
        ("tds", "194q", "194", "withholding", "tax deducted", "wht"),
    ),
)

_WORD_BOUNDARY = {"dmg", "frt", "tds", "194", "wht", "qty"}


def classify_reason(text: str | None) -> DeductionReason | None:
    """Best reason code for a free-text claim description.

    Returns None when the text says nothing about a claim at all, and
    ``UNKNOWN`` only when a deduction is clearly described but its category
    is not recognisable. The distinction matters: None means "no claim
    here", UNKNOWN means "a claim we could not categorise", and a review
    queue should treat those differently.
    """
    if not text:
        return None

    lowered = text.lower()
    for reason, keywords in _PATTERNS:
        for keyword in keywords:
            if keyword in _WORD_BOUNDARY:
                if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                    return reason
            elif keyword in lowered:
                return reason

    return None


def describes_a_deduction(text: str | None) -> bool:
    """True when the text is talking about money being withheld at all."""
    if not text:
        return False
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "less",
            "deduct",
            "deducted",
            "deduction",
            "adjust",
            "adjusted",
            "claim",
            "net of",
            "withheld",
            "short",
            "credit note",
        )
    )
