"""Rule-based extraction: the fallback that makes the LLM optional.

This is not a degraded mode to be embarrassed about. It handles the common
shapes outright and it is the reason the whole project runs with no API key,
no network and no cost. The LLM earns its place only on documents this cannot
read.

Three decisions shape the parser, and all three came from watching it fail on
the real corpus:

**Money is masked before references are looked for.** An advice document is
full of numbers, and ``10,94,691.97`` shredded into ``10``, ``94`` and
``69197`` was being read as three invoice references. Amounts are located
first and blanked out, so reference detection never sees them.

**Bare numerics are not references here.** A bank narration says
``PMT REF 42/43``; an advice document written by the customer says
``INV-00042``. Allowing bare numbers inside a document buys nothing and costs
a flood of false positives from dates and amounts.

**Deductions are read at document level as well as line level.** A customer
often lists the invoices, then writes "Rs. 4,900 deducted - 12 cases damaged"
on its own line without saying which invoice it belongs to. The claim is real
and the reason is useful even when the attribution is not stated.
"""

from __future__ import annotations

import re

from cashmatch.extraction.reasons import classify_reason, describes_a_deduction
from cashmatch.extraction.schema import (
    ExtractedLine,
    ExtractedRemittance,
    parse_amount,
)
from cashmatch.matching.config import ReferenceConfig
from cashmatch.matching.references import all_references
from cashmatch.normalize import normalize_narration

# A money token. Requires one of three signals so an invoice number is never
# mistaken for an amount: digit grouping, two decimal places, or an explicit
# currency marker.
_MONEY = re.compile(
    r"""
    (?:(?:Rs\.?|INR|₹)\s*)?
    (?:
        \d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?    # grouped: 4,18,500.00 / 418,500
      | \d+\.\d{2}                             # plain with paise: 418500.00
      | (?:(?<=Rs\s)|(?<=Rs\.\s)|(?<=INR\s)|(?<=₹\s))\d+   # bare, but marked
    )
    (?:\s*/-)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Dates masked before reference detection, so "21 Jan 2026" never becomes
# references 21 and 2026.
_DATE = re.compile(
    r"""
      \b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b
    | \b\d{1,2}[\s./-]*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*
      [\s./-]*\d{2,4}\b
    | \b(?:19|20)\d{2}\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Lines that are transport metadata, never content.
_HEADER_LINE = re.compile(r"^\s*(from|to|cc|bcc|sent|date|subject)\s*:", re.IGNORECASE)

_TOTAL_HINT = re.compile(
    r"\b("
    r"total|grand total|net (?:amount|payable|paid)"
    r"|amount (?:remitted|transferred|paid)"
    r"|total (?:remitted|transferred|paid)"
    r"|(?:we\s+)?(?:have\s+)?(?:paid|remitted|transferred)"
    r"|transfer of|payment of|rtgs done|neft done"
    r")\b",
    re.IGNORECASE,
)

_BANK_REF = re.compile(r"\bUTR[A-Z0-9]{8,}\b", re.IGNORECASE)

_QUOTE_PREFIX = re.compile(r"^\s*[>|]+\s*")

_PLACEHOLDER = " \x00 "


def extract_with_regex(text: str, reference_config: ReferenceConfig) -> ExtractedRemittance:
    """Read an advice document with rules only. Never raises.

    A document this cannot parse yields an empty result rather than an error:
    the caller then has nothing to feed the matcher, which is a correct
    outcome, not a failure.
    """
    if not text or not text.strip():
        return ExtractedRemittance()

    # Inside a document, a bare number is a date or an amount far more often
    # than an invoice. References here are always labelled.
    document_refs = reference_config.model_copy(update={"allow_bare_numeric": False})

    lines: list[ExtractedLine] = []
    seen: set[str] = set()
    stated_total: int | None = None
    bank_reference: str | None = None
    document_deduction = 0
    document_reason = None
    document_note = None

    for raw_line in text.splitlines():
        line = _QUOTE_PREFIX.sub("", raw_line).strip()
        if not line:
            continue

        if bank_reference is None:
            match = _BANK_REF.search(line)
            if match:
                bank_reference = match.group(0).upper()

        if _HEADER_LINE.match(line) and not _MONEY.search(line):
            continue

        amounts, masked = _amounts_and_masked(line)
        references = all_references(normalize_narration(_DATE.sub(" ", masked)), document_refs)

        if references:
            lines.extend(_lines_from(line, references, amounts, seen))
            continue

        if not amounts:
            continue

        if describes_a_deduction(line) and document_deduction == 0:
            # "Rs. 4,900 deducted - 12 cases damaged in transit." The claim is
            # real; which invoice bears it is not stated, and the matcher
            # decides that from the open items anyway.
            document_deduction = min(amounts)
            document_reason = classify_reason(line)
            document_note = line[:240]
        elif _TOTAL_HINT.search(line):
            stated_total = max(amounts) if stated_total is None else max(stated_total, *amounts)

    return ExtractedRemittance(
        lines=lines,
        total_paise=stated_total,
        bank_reference=bank_reference,
        document_deduction_paise=document_deduction,
        document_deduction_reason=document_reason,
        document_deduction_note=document_note,
    )


def _amounts_and_masked(line: str) -> tuple[list[int], str]:
    """Pull the money out of a line and blank it out of the remainder.

    Returning the masked text is the point: reference detection must never
    see the digits of an amount.
    """
    amounts: list[int] = []
    masked_parts: list[str] = []
    cursor = 0

    for match in _MONEY.finditer(line):
        value = parse_amount(match.group(0))
        if value is None:
            continue
        amounts.append(value)
        masked_parts.append(line[cursor : match.start()])
        masked_parts.append(_PLACEHOLDER)
        cursor = match.end()

    masked_parts.append(line[cursor:])
    return amounts, "".join(masked_parts)


def _lines_from(text: str, references, amounts: list[int], seen: set[str]) -> list[ExtractedLine]:
    """Build extracted lines from one physical line of the document."""
    gross, deduction, paid = _split_amounts(text, amounts)

    built: list[ExtractedLine] = []
    # Only a line naming exactly one invoice can attribute its amounts.
    # "INV-1 / INV-2 / INV-3 paid 50000" says nothing about the split.
    attributable = len(references) == 1

    for reference in references:
        normalized = reference.normalized
        if normalized in seen:
            continue
        seen.add(normalized)

        reason = classify_reason(text) if (attributable and deduction) else None
        built.append(
            ExtractedLine(
                invoice_reference=reference.raw,
                normalized_reference=normalized,
                gross_amount_paise=gross if attributable else None,
                paid_amount_paise=paid if attributable else None,
                deduction_amount_paise=deduction if attributable else 0,
                deduction_reason=reason,
                deduction_note=text.strip()[:240] if (attributable and deduction) else None,
            )
        )

    return built


def _split_amounts(text: str, amounts: list[int]) -> tuple[int | None, int, int | None]:
    """Work out which amount on a line is gross, which is the claim, which net.

    Prefers arithmetic over keywords. A PDF table row reads
    ``INV-00879  61,765.84  4,900.00  56,865.84`` with no "less" anywhere, but
    the three numbers only relate one way -- and checking that they add up is
    far more reliable than hoping for a keyword.
    """
    if not amounts:
        return None, 0, None

    if len(amounts) == 1:
        return amounts[0], 0, amounts[0]

    if len(amounts) >= 3:
        gross, second, third = amounts[0], amounts[1], amounts[2]
        if gross == second + third:
            deduction, paid = min(second, third), max(second, third)
            return gross, deduction, paid

    if len(amounts) == 2:
        first, second = amounts
        if first == second:
            # "39,810.23  -  39,810.23": gross and net, nothing withheld.
            return first, 0, first
        if describes_a_deduction(text):
            # "2,04,931.43/- | less INR 12,250.00 damage"
            deduction = min(first, second)
            gross = max(first, second)
            return gross, deduction, gross - deduction

    # Ambiguous. Take the largest as the gross and claim nothing else; the
    # matcher re-derives any gap from the open items.
    return max(amounts), 0, None
