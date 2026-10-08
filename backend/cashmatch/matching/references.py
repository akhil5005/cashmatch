"""Pulling invoice references out of free-text bank narration.

The narration field is a dumping ground. Across the generated corpus it holds
``INV-00042``, ``inv 42``, ``Invoice No. 42``, ``42/43/44``, a bare ``42``, a
revision-suffixed ``INV-00042-A``, and frequently nothing useful at all. This
module turns whatever is there into normalised candidate tokens; deciding
whether a token actually points at an open item is the matcher's job, and
restricting that lookup to open items is what keeps false positives rare.
"""

from __future__ import annotations

import re

from cashmatch.matching.candidates import ReferenceToken
from cashmatch.matching.config import ReferenceConfig
from cashmatch.normalize import normalize_reference

# Narration tokens are separated by whitespace and by any of these, which
# customers use interchangeably when listing several invoices.
_SPLIT = re.compile(r"[\s,;|&+]+|(?<=\d)/(?=\d)|(?<=[A-Za-z])/(?=[A-Za-z])")

_DIGITS = re.compile(r"\d")

# Words that contain digits but are never invoice references.
_STOPWORDS = frozenset({"NEFT", "RTGS", "IMPS", "UPI", "A/C", "AC", "GST", "TDS"})

# "INVOICE NO. 42", "Inv. No. 1098", "INV #42", "BILL NO 42" -- a label and a
# number that whitespace splitting would otherwise tear apart. The optional
# period after the label matters: "Inv." is one of the four spellings the
# corpus actually uses, and requiring whitespace there loses all of them.
_LABELLED = re.compile(
    r"(?:INV(?:OICE)?|BILL)\.?"  # the label, possibly abbreviated
    r"(?:\s*(?:NO|NUM|NUMBER)\b\.?)?"  # optional "no." / "number"
    r"\s*[:#/.\-]?\s*"  # optional separator
    r"(\d{1,8})"  # the number
    r"(?![\w-])",  # nothing more of the reference follows
    re.IGNORECASE,
)


def extract_references(narration: str, config: ReferenceConfig) -> list[ReferenceToken]:
    """Return the plausible invoice references in a narration, in order.

    Tokens are de-duplicated on their normalised form, so ``INV-00042`` and
    ``inv 42`` appearing in the same narration yield one reference rather
    than two.

    Filtering is deliberately loose here and strict at lookup time. A token
    that survives this function still has to resolve to an actual open item
    before it influences anything, so admitting a few extra candidates costs
    nothing while rejecting a real one costs a match.
    """
    if not config.enabled or not narration:
        return []

    tokens: list[ReferenceToken] = []
    seen: set[str] = set()

    for raw in _SPLIT.split(narration):
        candidate = raw.strip(".,:;-()[]")
        if not candidate or candidate.upper() in _STOPWORDS:
            continue

        digits = "".join(_DIGITS.findall(candidate))
        if not (config.min_digits <= len(digits) <= config.max_digits):
            continue

        has_letters = any(char.isalpha() for char in candidate)
        if not has_letters and not config.allow_bare_numeric:
            continue

        normalized = normalize_reference(candidate)
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        tokens.append(ReferenceToken(raw=candidate, normalized=normalized, digits=digits))

    return tokens


def labelled_references(narration: str, config: ReferenceConfig) -> list[ReferenceToken]:
    r"""Find references whose label and number are separate words.

    Whitespace splitting cannot see that ``INVOICE NO. 42`` is one reference,
    because it arrives as three tokens and the digits end up bare. This
    pattern reads the label, an optional "no.", an optional separator and the
    number as a single unit.

    The trailing guard ``(?![\w-])`` is what keeps ``INV-00042-A`` out: a
    revision suffix makes it a different document, and silently matching it
    to INV-00042 would be exactly the confident-but-wrong behaviour this
    project is built to avoid.
    """
    extra: list[ReferenceToken] = []
    for match in _LABELLED.finditer(narration):
        digits = match.group(1).lstrip("0") or "0"
        if config.min_digits <= len(match.group(1)) <= config.max_digits:
            extra.append(
                ReferenceToken(raw=match.group(0), normalized=f"INV{digits}", digits=digits)
            )
    return extra


def all_references(narration: str, config: ReferenceConfig) -> list[ReferenceToken]:
    """Every distinct reference candidate in a narration."""
    if not config.enabled:
        return []

    tokens = extract_references(narration, config)
    seen = {token.normalized for token in tokens}
    for token in labelled_references(narration, config):
        if token.normalized not in seen:
            seen.add(token.normalized)
            tokens.append(token)
    return tokens
