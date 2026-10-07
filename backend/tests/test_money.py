"""Money must be exact. These tests are the guard rail for that."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cashmatch.money import format_inr, paise_to_rupees, rupees_to_paise


@pytest.mark.parametrize(
    ("rupees", "paise"),
    [
        ("0.00", 0),
        ("1.00", 100),
        ("0.01", 1),
        ("41850.00", 4_185_000),
        ("418500.50", 41_850_050),
        ("-250.75", -25_075),
        (4185, 418_500),
        (Decimal("99999999.99"), 9_999_999_999),
    ],
)
def test_rupees_to_paise(rupees, paise) -> None:
    assert rupees_to_paise(rupees) == paise


@pytest.mark.parametrize("rupees", ["0.00", "41850.00", "0.07", "-250.75"])
def test_round_trip_is_lossless(rupees: str) -> None:
    assert paise_to_rupees(rupees_to_paise(rupees)) == Decimal(rupees)


def test_floats_are_rejected_outright() -> None:
    # 0.1 + 0.2 != 0.3 in binary floating point. Accepting a float here would
    # let that error into the ledger, so the type itself is refused.
    with pytest.raises(TypeError, match="Refusing to convert the float"):
        rupees_to_paise(4185.00)


def test_sub_paise_precision_is_refused_not_rounded() -> None:
    with pytest.raises(ValueError, match="more precision than paise allow"):
        rupees_to_paise("100.005")


def test_unparseable_input_gives_a_useful_message() -> None:
    with pytest.raises(ValueError, match="Could not read"):
        rupees_to_paise("Rs. 4,185")


def test_summing_many_amounts_stays_exact() -> None:
    """The subset-sum matcher depends on this: a hundred invoice lines must
    total to an exact equality, not an approximate one."""
    lines = [rupees_to_paise("0.07")] * 100
    assert sum(lines) == rupees_to_paise("7.00")


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (0, "\u20b90.00"),
        (100, "\u20b91.00"),
        (99_900, "\u20b9999.00"),
        (100_000, "\u20b91,000.00"),
        (4_185_000, "\u20b941,850.00"),
        (41_850_050, "\u20b94,18,500.50"),
        (1_234_567_890, "\u20b91,23,45,678.90"),
        (-25_075, "-\u20b9250.75"),
    ],
)
def test_indian_digit_grouping(paise: int, expected: str) -> None:
    assert format_inr(paise) == expected


def test_format_without_symbol() -> None:
    assert format_inr(41_850_050, symbol=False) == "4,18,500.50"
