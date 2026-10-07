"""Steps 1-3 and the settlement classifier, tested in isolation.

Reference extraction, customer identification and settlement shape are the
three places the engine can quietly go wrong without crashing, so each gets
tested against the exact variations the generated corpus contains.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from cashmatch.matching import (
    IdentificationMethod,
    MatchingConfig,
    SettlementShape,
    all_references,
    deduction_band,
    extract_references,
    identify_customer,
    settle,
)
from cashmatch.matching.settlement import claim_bearer, is_plausible_deduction
from cashmatch.money import rupees_to_paise
from cashmatch.normalize import normalize_narration, normalize_party_name

from .support import MATCHING_CONFIG, load_config, make_book, make_item

CONFIG = load_config()


# ===========================================================================
# config validation
# ===========================================================================


def _write_config(tmp_path, raw) -> str:
    path = tmp_path / "matching.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


@pytest.fixture(scope="module")
def shipped_raw() -> dict:
    return yaml.safe_load(MATCHING_CONFIG.read_text(encoding="utf-8"))


def test_shipped_matching_config_is_valid() -> None:
    config = MatchingConfig.from_yaml(MATCHING_CONFIG)
    assert config.subset_sum.max_combination_size >= 2
    assert config.amount.tolerance_paise >= 0


def test_unknown_fuzzy_scorer_is_rejected(tmp_path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["customer_identification"]["fuzzy_scorer"] = "vibes"

    with pytest.raises(ValueError, match="not supported"):
        MatchingConfig.from_yaml(_write_config(tmp_path, raw))


def test_a_typo_in_a_matching_key_is_an_error(tmp_path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["amount"]["tolerence_paise"] = 100

    with pytest.raises(ValueError, match="tolerence_paise"):
        MatchingConfig.from_yaml(_write_config(tmp_path, raw))


def test_inverted_deduction_band_is_rejected(tmp_path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["short_pay"]["min_deduction_pct"] = 20.0

    with pytest.raises(ValueError, match="must exceed"):
        MatchingConfig.from_yaml(_write_config(tmp_path, raw))


def test_missing_matching_config_says_where_it_looked(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="backend/config/matching.yml"):
        MatchingConfig.from_yaml(tmp_path / "absent.yml")


# ===========================================================================
# step 3 input: reference extraction
# ===========================================================================


def _refs(narration: str) -> list[str]:
    return [t.normalized for t in all_references(normalize_narration(narration), CONFIG.reference)]


@pytest.mark.parametrize(
    "narration",
    [
        "NEFT ABC TRADERS INV-00042",
        "NEFT ABC TRADERS inv 42",
        "RTGS CR INVOICE NO. 42",
        "PAYMENT AGAINST INV/42 - ABC TRADERS",
        "NEFT ABC TRADERS INV 0042",
        "BILL 42 PAID",
    ],
)
def test_every_reference_spelling_normalises_to_one_token(narration: str) -> None:
    """The point of normalisation: six narrations, one lookup key."""
    assert "INV42" in _refs(narration)


def test_several_references_are_all_extracted() -> None:
    assert _refs("NEFT ABC TRADERS INV-00042 INV-00043 INV-00044") == [
        "INV42",
        "INV43",
        "INV44",
    ]


def test_slash_separated_numbers_are_split() -> None:
    """Customers write "PMT REF 42/43/44" constantly."""
    assert _refs("NEFT ABC TRADERS PMT REF 42/43/44") == ["42", "43", "44"]


def test_a_repeated_reference_yields_one_token() -> None:
    assert _refs("INV-00042 AND INV-00042") == ["INV42"]


def test_a_spelled_out_label_and_a_bare_number_both_surface() -> None:
    """Both forms are offered; the lookup de-duplicates them onto one
    invoice, so handing over two candidates here costs nothing."""
    assert _refs("INV-00042 AND INV 42") == ["INV42", "42"]


def test_a_narration_with_no_reference_yields_nothing() -> None:
    assert _refs("NEFT ABC TRADERS PVT LTD") == []
    assert _refs("BULK PAYMENT MARCH") == []
    assert _refs("") == []


def test_channel_markers_are_not_mistaken_for_references() -> None:
    assert _refs("NEFT RTGS IMPS UPI CREDIT") == []


def test_overly_long_digit_runs_are_rejected() -> None:
    """A 16-digit account number is not an invoice reference."""
    assert _refs("NEFT A/C 50200012345678 ABC TRADERS") == []


def test_single_digits_are_rejected_by_the_floor() -> None:
    assert _refs("NEFT ABC TRADERS INV 7") == []


def test_bare_numeric_can_be_switched_off() -> None:
    strict = CONFIG.reference.model_copy(update={"allow_bare_numeric": False})
    tokens = extract_references(normalize_narration("PMT REF 42 43"), strict)
    assert tokens == []


@pytest.mark.parametrize(
    "narration",
    ["INVOICE NO. 42", "INV NO 42", "BILL NO. 42", "INV #42", "INVOICE NUMBER 42"],
)
def test_a_label_split_across_words_is_still_one_reference(narration: str) -> None:
    """Whitespace splitting leaves the number bare; the labelled pattern
    reads label and number as a unit."""
    assert "INV42" in _refs(f"NEFT ABC TRADERS {narration}")


def test_a_revision_suffix_does_not_resolve_to_the_base_invoice() -> None:
    """INV-00042-A is a different document reference. Quietly matching it to
    INV-00042 would be exactly the confident-but-wrong behaviour to avoid, so
    the token stays distinct and the payment goes to review."""
    tokens = _refs("NEFT ABC TRADERS INV-00042-A")
    assert "INV42" not in tokens
    assert tokens == ["INV00042A"]


# ===========================================================================
# step 2: customer identification
# ===========================================================================

CUSTOMERS = {
    1: "Shree Balaji Traders Pvt Ltd",
    2: "Annapurna Distributors",
    3: "Gupta & Sons Trading Company",
}
ALIASES = {"SHREE BALAJI TRDRS P LTD": 1}


def _identify(payer: str, *, config=None):
    book = make_book(customers=CUSTOMERS, aliases=ALIASES)
    return identify_customer(
        normalize_party_name(payer), book, config or CONFIG.customer_identification
    )


@pytest.mark.parametrize(
    "payer",
    [
        "Shree Balaji Traders Pvt Ltd",
        "SHREE BALAJI TRADERS PVT LTD",
        "M/s Shree Balaji Traders",
        "NEFT SHREE BALAJI TRADERS",
        "Shree Balaji Traders Private Limited",
    ],
)
def test_clean_variants_resolve_exactly(payer: str) -> None:
    """These never reach rapidfuzz, which is the whole point of normalising."""
    result = _identify(payer)
    assert result.customer_id == 1
    assert result.method is IdentificationMethod.EXACT
    assert result.score == 1.0


def test_a_known_alias_resolves_without_fuzzy_matching() -> None:
    result = _identify("SHREE BALAJI TRDRS P LTD")
    assert result.customer_id == 1
    assert result.method is IdentificationMethod.ALIAS


def test_an_unseen_abbreviation_falls_through_to_fuzzy() -> None:
    result = _identify("SHREE BALAJI TRADERS-PUNE BR")
    assert result.customer_id == 1
    assert result.method is IdentificationMethod.FUZZY
    assert 0 < result.score <= 1.0


def test_a_stranger_resolves_to_nobody() -> None:
    result = _identify("Zenith Global Logistics Incorporated")
    assert result.customer_id is None
    assert result.method is IdentificationMethod.NONE


def test_an_empty_payer_name_is_safe() -> None:
    result = _identify("")
    assert result.customer_id is None
    assert result.method is IdentificationMethod.NONE


def test_two_near_identical_customers_produce_an_ambiguity_not_a_guess() -> None:
    """The guard that matters most. Posting money against the wrong account
    because two names scored 90 and 89 is exactly the failure to avoid."""
    book = make_book(customers={1: "Konark Stores Pune", 2: "Konark Stores Pura"})
    result = identify_customer(
        normalize_party_name("KONARK STORES PUNA"), book, CONFIG.customer_identification
    )

    assert result.customer_id is None
    assert result.method is IdentificationMethod.AMBIGUOUS
    assert result.runner_up is not None


def test_two_aliases_of_one_customer_are_agreement_not_ambiguity() -> None:
    """The runner-up check only fires across *different* customers."""
    book = make_book(
        customers={1: "Shree Balaji Traders Pvt Ltd", 2: "Konark Agencies"},
        aliases={"SHREE BALAJI TRADERS BOMBAY": 1, "SHREE BALAJI TRDRS": 1},
    )
    result = identify_customer(
        normalize_party_name("SHREE BALAJI TRADERS BOMBA"), book, CONFIG.customer_identification
    )

    assert result.customer_id == 1
    assert result.method is IdentificationMethod.FUZZY


def test_raising_the_score_floor_rejects_a_weak_match() -> None:
    strict = CONFIG.customer_identification.model_copy(update={"fuzzy_min_score": 99.5})
    result = _identify("SHREE BALAJI TRADERS-PUNE BR", config=strict)

    assert result.customer_id is None
    assert result.matched_on is not None  # it still reports what it nearly matched


def test_an_empty_book_resolves_nothing() -> None:
    result = identify_customer(
        "abc traders", make_book(customers={}), CONFIG.customer_identification
    )
    assert result.customer_id is None


# ===========================================================================
# settlement shapes
# ===========================================================================


def _settle(items, rupees: str, **kwargs):
    return settle(items, rupees_to_paise(rupees), CONFIG.amount, CONFIG.short_pay, **kwargs)


def test_matching_totals_settle_exactly() -> None:
    result = _settle([make_item(1, "5000")], "5000")

    assert result is not None
    assert result.shape is SettlementShape.EXACT
    assert result.allocated_paise == rupees_to_paise("5000")
    assert result.deduction_paise == 0


def test_bank_rounding_is_absorbed_by_the_tolerance() -> None:
    items = [make_item(1, "5000.99")]
    result = _settle(items, "5000.00")

    assert result is not None
    assert result.shape is SettlementShape.EXACT


def test_the_tolerance_never_applies_cash_that_did_not_arrive() -> None:
    """Regression for UTR2026062000112.

    Five invoices totalled 31 paise more than the payment -- inside the 100
    paise tolerance, so the set matched, and every invoice was being
    allocated in full. That applies 31 paise of cash the bank never sent.
    The tolerance is there so rounding does not cost a match, not so the
    ledger can invent money.
    """
    items = [make_item(1, "30000.00"), make_item(2, "11850.31")]
    result = _settle(items, "41850.00")

    assert result is not None
    assert result.shape is SettlementShape.EXACT
    # The applied cash ties exactly to the credit received...
    assert result.allocated_paise == rupees_to_paise("41850.00")
    # ...and the difference is written off against the smaller invoice.
    written_off = [a for a in result.allocations if a.deduction_amount_paise]
    assert [a.item.invoice_number for a in written_off] == ["INV-00002"]
    assert written_off[0].deduction_amount_paise == 31
    # Each line still clears its invoice completely.
    assert all(a.settles_in_full for a in result.allocations)
    assert "rounding difference" in result.note


def test_a_shortfall_inside_the_tolerance_needs_no_write_off() -> None:
    """The invoices clear in full; the few extra paise show up as residual
    on the outcome rather than being forced onto a line."""
    items = [make_item(1, "5000.00")]
    result = _settle(items, "5000.60")

    assert result is not None
    assert result.shape is SettlementShape.EXACT
    assert result.deduction_paise == 0
    assert result.allocated_paise == rupees_to_paise("5000.00")


def test_one_invoice_underpaid_is_a_part_payment() -> None:
    result = _settle([make_item(1, "10000")], "4000")

    assert result is not None
    assert result.shape is SettlementShape.PARTIAL
    assert result.allocated_paise == rupees_to_paise("4000")
    assert result.deduction_paise == 0


def test_part_payment_can_be_disallowed_by_the_caller() -> None:
    assert _settle([make_item(1, "10000")], "4000", allow_partial=False) is None


def test_a_credible_gap_becomes_a_short_pay() -> None:
    result = _settle([make_item(1, "10000")], "9200")

    assert result is not None
    assert result.shape is SettlementShape.SHORT_PAY
    assert result.allocated_paise == rupees_to_paise("9200")
    assert result.deduction_paise == rupees_to_paise("800")


def test_an_implausibly_large_gap_is_not_a_short_pay() -> None:
    """Half the invoice missing is not a damage claim, it is a part payment."""
    result = _settle([make_item(1, "10000")], "5000")
    assert result is not None
    assert result.shape is SettlementShape.PARTIAL


def test_the_claim_lands_on_the_smallest_invoice_that_can_bear_it() -> None:
    items = [make_item(1, "100000"), make_item(2, "20000"), make_item(3, "5000")]
    # Rs. 500 short of Rs. 1,25,000 -- 10% of the smallest invoice, which is
    # where a damage claim would actually sit.
    result = _settle(items, "124500")

    assert result is not None
    assert result.shape is SettlementShape.SHORT_PAY
    claimed = [a for a in result.allocations if a.deduction_amount_paise]
    assert [a.item.invoice_number for a in claimed] == ["INV-00003"]


def test_a_gap_larger_than_every_invoice_is_not_a_deduction() -> None:
    items = [make_item(1, "5000"), make_item(2, "4000")]
    # Gap of Rs. 6,000 exceeds both invoices, so nothing can bear the claim.
    assert claim_bearer(items, rupees_to_paise("6000")) is None


def test_multi_invoice_sets_never_settle_as_part_payments() -> None:
    items = [make_item(1, "10000"), make_item(2, "10000")]
    assert _settle(items, "4000") is None


def test_an_empty_set_settles_nothing() -> None:
    assert _settle([], "1000") is None


def test_a_zero_payment_settles_nothing() -> None:
    assert _settle([make_item(1, "1000")], "0") is None


# --- the deduction-basis regression ----------------------------------------


def test_a_small_claim_inside_a_large_remittance_is_still_a_short_pay() -> None:
    """Regression for the bug found on UTR2026012100337.

    Three invoices totalling Rs. 10,99,591.97 were settled with
    Rs. 10,94,691.97 -- a Rs. 4,900 damage claim against the Rs. 56,865
    invoice in the set. Measured against that invoice the claim is 8.6% and
    entirely routine; measured against the remittance total it is 0.45% and
    was being dismissed as rounding. The payment then fell through to a
    combinatorial search that produced three wrong readings.
    """
    items = [
        make_item(410, "896179.18"),
        make_item(32, "141646.95"),
        make_item(879, "61765.84"),
    ]
    result = _settle(items, "1094691.97")

    assert result is not None
    assert result.shape is SettlementShape.SHORT_PAY
    claimed = [a for a in result.allocations if a.deduction_amount_paise]
    assert len(claimed) == 1
    assert claimed[0].item.invoice_number == "INV-00879"
    assert claimed[0].deduction_amount_paise == rupees_to_paise("4900")
    assert result.allocated_paise == rupees_to_paise("1094691.97")


def test_plausibility_is_measured_against_the_bearer_not_the_total() -> None:
    small = make_item(1, "56865.84")
    gap = rupees_to_paise("4900")

    assert is_plausible_deduction(gap, small, CONFIG.short_pay)
    # The same gap against a far larger invoice is below the floor.
    assert not is_plausible_deduction(gap, make_item(2, "9000000"), CONFIG.short_pay)


def test_a_deduction_may_not_exceed_the_invoice_it_sits_on() -> None:
    item = make_item(1, "1000")
    assert not is_plausible_deduction(rupees_to_paise("1000"), item, CONFIG.short_pay)
    assert not is_plausible_deduction(rupees_to_paise("1500"), item, CONFIG.short_pay)


def test_the_absolute_ceiling_overrides_the_percentage() -> None:
    capped = CONFIG.short_pay.model_copy(update={"max_deduction_paise": 100_000})
    item = make_item(1, "900000")
    # 5% of Rs. 9,00,000 is Rs. 45,000 -- inside the percentage band, but
    # above a Rs. 1,000 absolute ceiling.
    assert not is_plausible_deduction(rupees_to_paise("45000"), item, capped)


def test_the_search_band_starts_strictly_above_the_payment() -> None:
    """A total equal to the payment is an exact match, not a zero deduction."""
    amount = rupees_to_paise("10000")
    low, high = deduction_band(amount, CONFIG.short_pay)

    assert low == amount + 1
    assert high > low


def test_the_search_band_is_wide_enough_for_the_largest_credible_claim() -> None:
    """The band must contain every total a plausible deduction could produce,
    because the precise per-invoice test runs afterwards."""
    amount = rupees_to_paise("100000")
    low, high = deduction_band(amount, CONFIG.short_pay)
    # A 15% claim on an invoice of T leaves amount = 0.85T, so T = amount/0.85.
    assert high >= int(amount / 0.85) - 1
    assert low <= amount + 1
