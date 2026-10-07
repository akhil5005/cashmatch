"""Canonicalisation of the two messiest fields in a bank statement:
the payer's name and the free-text narration.

Banks do not transmit clean data. The same customer arrives as
``A.B.C. Traders``, ``ABC TRADERS PVT LTD``, ``M/s ABC Traders`` and
``NEFT-ABC TRDRS-MUMBAI`` across four payments. Normalising both sides to a
canonical form turns most of that variation into an *exact* match, which is
cheap and certain. Only what survives normalisation needs fuzzy matching,
which is neither.

Phase 1 defines and tests these functions; Phase 3's matcher consumes them.
"""

from __future__ import annotations

import re

# Legal-form suffixes and bank channel markers. None of these distinguish one
# customer from another, so they are dropped before comparison. Removal is
# token-level, so a company genuinely named "Coca Cola" keeps both words.
NOISE_TOKENS = frozenset(
    {
        # Legal forms
        "pvt",
        "pvt.",
        "private",
        "ltd",
        "ltd.",
        "limited",
        "llp",
        "plc",
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "company",
        "co",
        # Indian honorifics / address noise seen in payer fields
        "messrs",
        "the",
        # Bank channel prefixes that leak into the payer name field
        "neft",
        "rtgs",
        "imps",
        "upi",
        "chq",
        "cheque",
        "cr",
        "by",
        "transfer",
        "payment",
        "pmt",
        "from",
    }
)

# Different spellings of "invoice" that all mean the same thing.
PREFIX_ALIASES = {
    "INVOICE": "INV",
    "INVOICENO": "INV",
    "INVOICENUM": "INV",
    "INVOICENUMBER": "INV",
    "INVNO": "INV",
    "INVNUM": "INV",
    "BILL": "INV",
    "BILLNO": "INV",
}

_MS_PREFIX = re.compile(r"^\s*m\s*/\s*s\.?\s*", re.IGNORECASE)
_PERIODS = re.compile(r"\.(?=\s|$|\w)")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_REFERENCE_SHAPE = re.compile(r"^([A-Z]+)?0*(\d+)$")


def normalize_party_name(raw: str | None) -> str:
    """Reduce a payer or customer name to a comparable canonical form.

    ``"M/s A.B.C. Traders Pvt. Ltd."`` and ``"ABC TRADERS"`` both become
    ``"abc traders"``.

    Periods are deleted rather than replaced with a space, so an initialism
    like ``A.B.C.`` collapses to ``abc`` instead of splitting into three
    one-letter tokens. Every other separator becomes a space.
    """
    if not raw:
        return ""

    text = _MS_PREFIX.sub("", raw)
    text = text.replace(".", "")
    text = _NON_WORD.sub(" ", text.lower()).strip()

    tokens = [t for t in text.split() if t]
    meaningful = [t for t in tokens if t not in NOISE_TOKENS]

    # If stripping noise left nothing, the "noise" was the name. Keep the
    # unstripped form rather than returning an empty string that would match
    # every other emptied name.
    return " ".join(meaningful or tokens)


def normalize_reference(raw: str | None) -> str:
    """Reduce an invoice reference to a canonical form.

    ``"INV-00042"``, ``"inv 42"``, ``"Invoice No. 42"`` and ``"BILL/42"`` all
    become ``"INV42"``. Leading zeros are stripped because a customer typing
    the number by hand will rarely reproduce the ERP's zero padding.

    A reference that doesn't fit the ``<letters><digits>`` shape is returned
    uppercased and stripped of separators, so it is at least comparable.
    """
    if not raw:
        return ""

    compact = re.sub(r"[^A-Za-z0-9]+", "", raw).upper()
    if not compact:
        return ""

    match = _REFERENCE_SHAPE.match(compact)
    if not match:
        return compact

    prefix, digits = match.group(1) or "", match.group(2)
    prefix = PREFIX_ALIASES.get(prefix, prefix)
    return f"{prefix}{digits}"


def normalize_narration(raw: str | None) -> str:
    """Flatten a bank narration to single-spaced uppercase text.

    Unlike the name and reference helpers this keeps every token: the
    narration is searched for references later, so throwing away content here
    would destroy the signal the matcher is looking for.
    """
    if not raw:
        return ""
    return " ".join(raw.upper().split())
