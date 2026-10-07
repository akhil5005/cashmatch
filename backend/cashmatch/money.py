"""Money handling for CashMatch.

The single rule this module exists to enforce: **money is never a float**.

Amounts are stored and computed as integer *paise* (1 rupee = 100 paise).
Integers are exact, so adding up a hundred invoice lines and comparing the
total to a bank credit is an equality check, not an epsilon comparison. That
matters directly: the subset-sum matcher in Phase 3 asks "does this set of
invoices total exactly what arrived?" thousands of times, and a float would
make that question unanswerable.

`Decimal` appears only at the edges, where humans and files speak rupees.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import NewType

# A distinct type so that a plain `int` of rupees can't silently masquerade as
# paise. It is an int at runtime -- zero overhead -- but type checkers and
# readers both see the intent.
Paise = NewType("Paise", int)

PAISE_PER_RUPEE = 100

ZERO = Paise(0)


def rupees_to_paise(value: str | int | Decimal) -> Paise:
    """Convert a rupee amount to integer paise.

    Accepts a string ("4185.00"), an int (4185), or a Decimal. Floats are
    rejected outright: by the time a float reaches this function the precision
    damage has already happened, so silently accepting it would hide the bug.

    Raises:
        TypeError: if given a float.
        ValueError: if the value is unparseable or has sub-paise precision.
    """
    if isinstance(value, float):
        raise TypeError(
            f"Refusing to convert the float {value!r} to money. Floats cannot "
            "represent decimal amounts exactly. Pass a string, int or Decimal "
            'instead -- for example rupees_to_paise("4185.00").'
        )

    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"Could not read {value!r} as a rupee amount. Expected something "
            'like "4185.00", 4185 or Decimal("4185.00").'
        ) from exc

    if not amount.is_finite():
        raise ValueError(f"{value!r} is not a finite amount.")

    # Decimal exponent of -2 means two decimal places. Anything smaller is
    # sub-paise and cannot be represented, so we refuse rather than round.
    if amount.as_tuple().exponent < -2:
        raise ValueError(
            f"{value!r} has more precision than paise allow. The smallest unit "
            "of Indian currency is 1 paise (two decimal places)."
        )

    return Paise(int(amount.scaleb(2)))


def paise_to_rupees(paise: Paise | int) -> Decimal:
    """Convert integer paise back to an exact rupee Decimal."""
    return Decimal(int(paise)).scaleb(-2)


def format_inr(paise: Paise | int, *, symbol: bool = True) -> str:
    """Render paise as a rupee string using Indian digit grouping.

    Indian convention groups the last three digits, then pairs:
    1234567890 paise -> "Rs.1,23,45,678.90", not "Rs.12,345,678.90".
    Getting this right matters for a tool an Indian AR analyst reads all day.
    """
    total = int(paise)
    sign = "-" if total < 0 else ""
    total = abs(total)

    whole, fraction = divmod(total, PAISE_PER_RUPEE)
    grouped = _group_indian(str(whole))
    prefix = "\u20b9" if symbol else ""
    return f"{sign}{prefix}{grouped}.{fraction:02d}"


def _group_indian(digits: str) -> str:
    """Insert commas Indian-style: last group of 3, then groups of 2."""
    if len(digits) <= 3:
        return digits

    last_three = digits[-3:]
    rest = digits[:-3]

    pairs = []
    while len(rest) > 2:
        pairs.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        pairs.insert(0, rest)

    return ",".join([*pairs, last_three])
