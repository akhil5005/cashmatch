"""Customer master data, and the many ways a bank will spell it.

The split that matters here is between two kinds of name variation:

*clean variants*
    Spellings that ``normalize_party_name`` collapses onto the registered
    name. "M/s SHREE BALAJI TRADERS PVT LTD" and "Shree Balaji Traders" both
    reduce to ``shree balaji traders``. The matcher should resolve these with
    an index lookup and no fuzzy scoring at all.

*hard variants*
    Spellings normalisation cannot rescue: abbreviations ("TRDRS"), appended
    branch cities, bank-field truncation, a dropped honorific. These are what
    the ``payer_name_variant`` scenario uses, and the only way through them is
    fuzzy matching.

Keeping the two separate is what lets Phase 6 report honestly on how much
work normalisation does versus how much fuzzy matching does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from random import Random

from cashmatch.generator.vocabulary import (
    CHANNELS,
    CITIES,
    LEGAL_FORMS,
    NAME_PREFIXES,
    NAME_STEMS,
    NAME_SUFFIXES,
)
from cashmatch.normalize import normalize_party_name

# Abbreviations real bank narrations use for trade suffixes.
_SUFFIX_ABBREVIATIONS = {
    "Traders": "TRDRS",
    "Distributors": "DISTR",
    "Agencies": "AGNCS",
    "Enterprises": "ENTP",
    "Trading Company": "TRDG CO",
    "Sales Corporation": "SALES CORPN",
    "Marketing": "MKTG",
    "Stores": "STR",
    "& Sons": "AND SONS",
    "Sales": "SLS",
}

# Banks commonly truncate the beneficiary name field.
_TRUNCATION_LENGTH = 24


@dataclass(slots=True)
class PartyProfile:
    """One customer, with everything the generator needs to know about them."""

    code: str
    legal_name: str
    normalized_name: str
    city: str
    payment_terms_days: int
    stem: str
    suffix: str
    registered_aliases: list[str] = field(default_factory=list)


def _compose_name(rng: Random) -> tuple[str, str, str]:
    """Return (legal_name, stem, suffix) for a plausible trade name."""
    stem = rng.choice(NAME_STEMS)
    suffix = rng.choice(NAME_SUFFIXES)
    legal_form = rng.choice(LEGAL_FORMS)

    parts: list[str] = []
    # Not every trade name carries an honorific prefix.
    if rng.random() < 0.45:
        parts.append(rng.choice(NAME_PREFIXES))
    parts.extend([stem, suffix])
    if legal_form:
        parts.append(legal_form)

    return " ".join(parts), stem, suffix


def build_customers(rng: Random, count: int, cfg) -> list[PartyProfile]:
    """Generate `count` customers with distinct normalised names.

    Distinctness is enforced rather than hoped for: two customers whose names
    normalise identically would make correct matching impossible, so the
    accuracy numbers measured later would be meaningless.
    """
    profiles: list[PartyProfile] = []
    seen: set[str] = set()
    attempts = 0
    max_attempts = count * 60

    while len(profiles) < count:
        attempts += 1
        if attempts > max_attempts:
            raise RuntimeError(
                f"Could not generate {count} customers with distinct normalised "
                f"names after {max_attempts} attempts (got {len(profiles)}). The "
                "name vocabulary is too small for this customer count; add stems "
                "to cashmatch/generator/vocabulary.py."
            )

        legal_name, stem, suffix = _compose_name(rng)
        normalized = normalize_party_name(legal_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        profile = PartyProfile(
            code=f"CUST-{len(profiles) + 1:04d}",
            legal_name=legal_name,
            normalized_name=normalized,
            city=rng.choice(CITIES),
            payment_terms_days=rng.choice(cfg.invoice.payment_terms_days),
            stem=stem,
            suffix=suffix,
        )
        profile.registered_aliases = _registered_aliases(rng, profile, cfg)
        profiles.append(profile)

    return profiles


def _registered_aliases(rng: Random, profile: PartyProfile, cfg) -> list[str]:
    """Alternate spellings already on file in the ERP for this customer.

    Only some customers have them. The rest are the reason fuzzy matching
    exists.
    """
    if rng.random() * 100 >= cfg.customer.registered_alias_share_pct:
        return []

    wanted = rng.randint(1, max(1, cfg.customer.max_registered_aliases))
    aliases: list[str] = []
    seen = {profile.normalized_name}

    for _ in range(wanted * 3):
        if len(aliases) >= wanted:
            break
        candidate = hard_payer_variant(rng, profile)
        key = normalize_party_name(candidate)
        if key and key not in seen:
            seen.add(key)
            aliases.append(candidate)

    return aliases


def clean_payer_variant(rng: Random, profile: PartyProfile) -> str:
    """A payer spelling that normalisation collapses onto the registered name.

    Every branch here is covered by a test asserting the normalised form is
    unchanged, because a "clean" variant that is secretly hard would corrupt
    the scenario labels.
    """
    name = profile.legal_name
    choice = rng.randint(0, 4)

    if choice == 0:
        return name.upper()
    if choice == 1:
        return f"M/s {name}"
    if choice == 2:
        return f"{rng.choice(CHANNELS)} {name.upper()}"
    if choice == 3:
        # Legal form spelled out or abbreviated differently; both are noise.
        swapped = name.replace("Pvt Ltd", "Private Limited").replace("Private Limited", "Pvt. Ltd.")
        return swapped if swapped != name else f"{name} PVT LTD"
    return " ".join(name.upper().split())


def hard_payer_variant(rng: Random, profile: PartyProfile) -> str:
    """A payer spelling normalisation cannot rescue.

    These are what the bank actually sends when its beneficiary field is too
    short, or when the remitting branch types from memory.
    """
    name = profile.legal_name
    choice = rng.randint(0, 4)

    if choice == 0:
        # Abbreviated trade suffix: "TRADERS" -> "TRDRS".
        abbreviated = _SUFFIX_ABBREVIATIONS.get(profile.suffix, profile.suffix.upper())
        return name.upper().replace(profile.suffix.upper(), abbreviated)
    if choice == 1:
        # Branch city glued on, as many bank systems do.
        return f"{name.upper()}-{profile.city.upper()}"
    if choice == 2:
        # Field truncation.
        return name.upper()[:_TRUNCATION_LENGTH].strip()
    if choice == 3:
        # Honorific dropped; "Shree Balaji Traders" becomes "Balaji Traders".
        words = name.split()
        if words and words[0] in NAME_PREFIXES:
            return " ".join(words[1:]).upper()
        return f"{profile.stem} {profile.suffix}".upper()
    # "P LTD" rather than "PVT LTD" survives noise-stripping as a stray token.
    return f"{profile.stem.upper()} {profile.suffix.upper()} P LTD"


def build_unknown_payer(rng: Random, known_keys: set[str]) -> str:
    """A payer name belonging to no customer on file.

    Used by the ``no_matching_invoice`` scenario, where the right answer is
    for the money to end up as unapplied cash. The name is guaranteed not to
    collide with a real customer, so the scenario label is unambiguous.
    """
    for _ in range(200):
        legal_name, _, _ = _compose_name(rng)
        if normalize_party_name(legal_name) not in known_keys:
            return legal_name.upper()

    raise RuntimeError(
        "Could not generate a payer name that does not collide with an existing "
        "customer. The name vocabulary is too small for this customer count."
    )
