"""Normalisation turns name and reference variation into exact matches."""

from __future__ import annotations

import pytest

from cashmatch.normalize import normalize_narration, normalize_party_name, normalize_reference


@pytest.mark.parametrize(
    "variant",
    [
        "A.B.C. Traders",
        "ABC Traders Pvt Ltd",
        "ABC TRADERS PRIVATE LIMITED",
        "M/s ABC Traders",
        "M/s. A.B.C. Traders Pvt. Ltd.",
        "  abc   traders  ",
        "NEFT ABC TRADERS",
        "ABC-TRADERS",
    ],
)
def test_payer_name_variants_collapse_to_one_form(variant: str) -> None:
    """Every spelling the brief calls out must land on the same key, so the
    matcher can use an index lookup instead of a fuzzy scan."""
    assert normalize_party_name(variant) == "abc traders"


def test_distinct_customers_stay_distinct() -> None:
    assert normalize_party_name("ABC Traders") != normalize_party_name("ABD Traders")


def test_a_name_made_entirely_of_noise_words_is_not_emptied() -> None:
    # Stripping every token would make this match any other emptied name.
    assert normalize_party_name("The Company Ltd") == "the company ltd"


def test_empty_and_none_are_safe() -> None:
    assert normalize_party_name(None) == ""
    assert normalize_party_name("") == ""


@pytest.mark.parametrize(
    "variant",
    [
        "INV-00042",
        "inv 42",
        "INV/42",
        "Invoice No. 42",
        "invoice-00042",
        "BILL 42",
        "inv_0042",
    ],
)
def test_reference_variants_collapse_to_one_form(variant: str) -> None:
    assert normalize_reference(variant) == "INV42"


def test_leading_zeros_do_not_create_false_distinctions() -> None:
    assert normalize_reference("INV-00042") == normalize_reference("INV42")


def test_different_invoice_numbers_stay_different() -> None:
    assert normalize_reference("INV-00042") != normalize_reference("INV-00043")


def test_bare_number_keeps_its_digits() -> None:
    assert normalize_reference("00042") == "42"


def test_unrecognised_shape_is_still_comparable() -> None:
    assert normalize_reference("PO/2026/AB-12/X") == "PO2026AB12X"


def test_narration_keeps_every_token() -> None:
    # Unlike names, narration content is searched later, so nothing is dropped.
    assert (
        normalize_narration("  neft   inv-42  inv-43 \n ded dmg ") == "NEFT INV-42 INV-43 DED DMG"
    )
