"""Extraction: the schema, the rules, and the safety net around the model.

The tests that matter most here are not the ones proving extraction works.
They are the ones proving it **fails safely** -- when the model returns
garbage, when it returns plausible-looking numbers that do not add up, and
when it cannot be reached at all. An extractor that is right most of the time
and silently wrong the rest is worse than no extractor.
"""

from __future__ import annotations

import json

import pytest

from cashmatch.extraction import (
    MockProvider,
    ProviderError,
    Reconciliation,
    ScriptedProvider,
    classify_reason,
    extract_remittance,
    extract_with_regex,
    parse_amount,
)
from cashmatch.extraction.reasons import describes_a_deduction
from cashmatch.extraction.schema import ExtractedLine, ExtractedRemittance
from cashmatch.models.enums import DeductionReason, ExtractionMethod
from cashmatch.money import rupees_to_paise

from .support import load_config

CONFIG = load_config()
REFERENCE_CONFIG = CONFIG.reference


def _regex(text: str) -> ExtractedRemittance:
    return extract_with_regex(text, REFERENCE_CONFIG)


def _pipeline(text: str, provider=None, amount: str | None = None):
    return extract_remittance(
        text,
        provider=provider,
        reference_config=REFERENCE_CONFIG,
        amount_paise=rupees_to_paise(amount) if amount else None,
        tolerance_paise=CONFIG.amount.tolerance_paise,
    )


# ===========================================================================
# amount parsing -- the four conventions the corpus actually uses
# ===========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rs. 4,18,500.00", 41_850_000),  # Indian grouping
        ("418500.00", 41_850_000),  # plain
        ("4,18,500.00/-", 41_850_000),  # trailing slash-dash
        ("INR 418,500.00", 41_850_000),  # Western grouping
        ("₹418500.00", 41_850_000),
        ("15,195.39", 1_519_539),
        ("0.07", 7),
    ],
)
def test_every_money_convention_parses_to_the_same_paise(raw: str, expected: int) -> None:
    assert parse_amount(raw) == expected


def test_sub_paise_precision_is_quantised_not_discarded() -> None:
    """A document with three decimals is still usable; the line is worth more
    than the tenth of a paise."""
    assert parse_amount("100.005") == 10_001 or parse_amount("100.005") == 10_000


@pytest.mark.parametrize("raw", ["", "   ", "not a number", "Rs.", None, "abc.de"])
def test_unparseable_amounts_degrade_the_line_not_the_document(raw) -> None:
    assert parse_amount(raw) is None


def test_a_float_never_reaches_the_money_layer() -> None:
    """The wire schema types amounts as strings precisely so this path does
    not exist. Confirm the conversion is string-based end to end."""
    assert parse_amount("418500.00") == 41_850_000
    assert parse_amount(str(418500.00)) == 41_850_000


# ===========================================================================
# reason classification
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12 cases damaged in transit", DeductionReason.DAMAGE),
        ("breakage claim for detergent consignment", DeductionReason.DAMAGE),
        ("Q1 scheme discount adjusted", DeductionReason.PROMO),
        ("rate difference as per revised price list", DeductionReason.PRICING),
        ("8 cases short received", DeductionReason.SHORT_SHIP),
        ("quantity mismatch on delivery", DeductionReason.SHORT_SHIP),
        ("freight borne by us as per terms", DeductionReason.FREIGHT),
        ("TDS deducted u/s 194Q", DeductionReason.TDS),
        ("less DMG claim", DeductionReason.DAMAGE),
    ],
)
def test_claims_are_classified_from_the_customers_own_words(text, expected) -> None:
    assert classify_reason(text) is expected


def test_unrecognisable_wording_is_not_forced_into_a_category() -> None:
    """None means "no claim described" and UNKNOWN means "a claim we could
    not categorise". A review queue should treat those differently."""
    assert classify_reason("payment for March invoices") is None
    assert classify_reason(None) is None


def test_deduction_language_is_detected_separately_from_its_reason() -> None:
    assert describes_a_deduction("less Rs 12,000 adjusted")
    assert describes_a_deduction("net of claim")
    assert not describes_a_deduction("payment released today")


# ===========================================================================
# the rule-based extractor
# ===========================================================================


def test_an_itemised_email_yields_a_line_per_invoice() -> None:
    payload = _regex(
        """Subject: Payment advice - 06.02.2026

We have remitted Rs. 4,18,500.00 towards the following bills.

  INV-00042    Rs. 3,00,000.00
  INV-00043    Rs. 1,18,500.00

Regards,
Accounts Department"""
    )

    assert payload.references == ["INV42", "INV43"]
    assert payload.total_paise == rupees_to_paise("418500.00")
    assert payload.stated_paid_paise == rupees_to_paise("418500.00")


def test_a_pipe_table_row_splits_gross_from_the_claim() -> None:
    payload = _regex(
        """Invoice        | Amount        | Remarks
----------------------------------------------------
INV-00746      | 2,04,931.43/- | less INR 12,250.00 damage

Total transferred: 192681.43"""
    )

    assert payload.references == ["INV746"]
    line = payload.lines[0]
    assert line.gross_amount_paise == rupees_to_paise("204931.43")
    assert line.deduction_amount_paise == rupees_to_paise("12250.00")
    assert line.deduction_reason is DeductionReason.DAMAGE
    assert payload.total_paise == rupees_to_paise("192681.43")


def test_a_pdf_table_row_is_read_by_arithmetic_not_keywords() -> None:
    """A PDF row says "INV-00879  61,765.84  4,900.00  56,865.84" with no
    "less" anywhere. The three numbers only relate one way, and checking
    that they add up beats hoping for a keyword."""
    payload = _regex(
        """REMITTANCE ADVICE
Invoice            Gross            Deduction        Net paid
INV-00879          61,765.84        4,900.00         56,865.84
Total remitted: 56,865.84"""
    )

    line = payload.lines[0]
    assert line.gross_amount_paise == rupees_to_paise("61765.84")
    assert line.deduction_amount_paise == rupees_to_paise("4900.00")
    assert line.paid_amount_paise == rupees_to_paise("56865.84")


def test_a_dash_for_no_deduction_is_not_mistaken_for_one() -> None:
    payload = _regex(
        """Invoice            Gross            Deduction        Net paid
INV-00802          39,810.23        -                39,810.23"""
    )

    line = payload.lines[0]
    assert line.deduction_amount_paise == 0
    assert line.gross_amount_paise == rupees_to_paise("39810.23")


def test_a_forwarded_thread_is_read_through_its_quote_markers() -> None:
    """The quoted text is still the customer describing their own payment."""
    payload = _regex(
        """Subject: Fwd: Re: payment status

---------- Forwarded message ----------
From: Aryan Maharaj <accounts@example.in>
Date: Wed, 21 Jan 2026

> could you confirm when payment for INV-00410 / INV-00032 / INV-00879 will be released?

Sir, transfer of 10,94,691.97/- has been done from our side.

4900.00 deducted - breakage claim for detergent consignment.

Bank reference: UTR2026012100337"""
    )

    assert payload.references == ["INV410", "INV32", "INV879"]
    assert payload.total_paise == rupees_to_paise("1094691.97")
    assert payload.document_deduction_paise == rupees_to_paise("4900.00")
    assert payload.claimed_reason is DeductionReason.DAMAGE
    assert payload.bank_reference == "UTR2026012100337"


def test_amounts_are_never_mistaken_for_invoice_references() -> None:
    """Regression. "10,94,691.97" was being shredded into references 10, 94
    and 69197, and "Wed, 21 Jan 2026" into 21 and 2026. Across the corpus
    that inflated 723 real references to 2,669."""
    payload = _regex(
        """Date: Wed, 21 Jan 2026
Sir, transfer of 10,94,691.97/- has been done against INV-00410."""
    )

    assert payload.references == ["INV410"]


def test_a_multi_invoice_line_does_not_invent_a_split() -> None:
    """ "INV-1 / INV-2 / INV-3 paid 50000" says nothing about who got what."""
    payload = _regex("paid Rs. 50,000.00 against INV-00001, INV-00002, INV-00003")

    assert len(payload.lines) == 3
    assert all(line.gross_amount_paise is None for line in payload.lines)


def test_a_document_with_nothing_in_it_yields_nothing() -> None:
    assert _regex("").is_empty
    assert _regex("Dear Sir, kindly confirm receipt. Regards.").is_empty


def test_the_same_invoice_mentioned_twice_yields_one_line() -> None:
    payload = _regex(
        """INV-00042    Rs. 1,000.00
As discussed, this covers INV-00042."""
    )
    assert payload.references == ["INV42"]


# ===========================================================================
# the pipeline: validate, retry, fall back, cross-check
# ===========================================================================


def test_mock_mode_needs_no_key_and_no_network() -> None:
    """The default. A cold clone of the repo must extract successfully."""
    result = _pipeline(
        "We have paid Rs. 15,195.39 today against Inv. No. 1098.",
        provider=MockProvider(REFERENCE_CONFIG),
    )

    assert result.method is ExtractionMethod.MOCK
    assert result.payload.references == ["INV1098"]
    assert result.attempts == 1


def test_no_provider_goes_straight_to_the_rules() -> None:
    result = _pipeline("INV-00042  Rs. 1,000.00", provider=None)
    assert result.method is ExtractionMethod.REGEX_FALLBACK


def test_malformed_json_is_retried_once_and_then_accepted() -> None:
    good = json.dumps({"lines": [{"invoice_reference": "INV-00042"}]})
    provider = ScriptedProvider(["this is not json at all", good])

    result = _pipeline("INV-00042 Rs. 1,000.00", provider=provider)

    assert provider.calls == 2
    assert result.attempts == 2
    assert result.method is ExtractionMethod.LLM
    assert result.payload.references == ["INV42"]
    assert "Accepted on retry" in (result.error or "")


def test_the_retry_prompt_carries_the_validation_error_back() -> None:
    """A blind second call usually reproduces the first mistake. Feeding the
    error back is what makes the retry worth spending."""
    good = json.dumps({"lines": [{"invoice_reference": "INV-00042"}]})

    class Recorder(ScriptedProvider):
        prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return super().complete(prompt)

    recorder = Recorder(["{ broken", good])
    _pipeline("INV-00042 Rs. 1,000.00", provider=recorder)

    assert len(recorder.prompts) == 2
    assert "Correction required" in recorder.prompts[1]
    assert "not valid JSON" in recorder.prompts[1]


def test_two_bad_answers_fall_back_to_the_rules() -> None:
    provider = ScriptedProvider(["nonsense", "still nonsense"])

    result = _pipeline("INV-00042  Rs. 1,000.00", provider=provider)

    assert provider.calls == 2
    assert result.method is ExtractionMethod.REGEX_FALLBACK
    assert result.payload.references == ["INV42"]
    assert "failed schema validation twice" in (result.error or "")


def test_an_unreachable_provider_falls_back_immediately() -> None:
    """Observed for real against Gemini returning 503 under load. Retrying a
    network failure inside a batch run just doubles the wait."""
    provider = ScriptedProvider(["unused"], raise_after=0)

    result = _pipeline("INV-00042  Rs. 1,000.00", provider=provider)

    assert result.method is ExtractionMethod.REGEX_FALLBACK
    assert result.payload.references == ["INV42"]
    assert "Provider unavailable" in (result.error or "")


def test_a_markdown_fenced_answer_is_still_accepted() -> None:
    """Models wrap JSON in fences often enough to strip rather than fail on."""
    fenced = '```json\n{"lines": [{"invoice_reference": "INV-00042"}]}\n```'
    result = _pipeline("x", provider=ScriptedProvider([fenced]))

    assert result.method is ExtractionMethod.LLM
    assert result.payload.references == ["INV42"]


def test_a_json_array_at_the_top_level_is_rejected() -> None:
    provider = ScriptedProvider(['[{"invoice_reference": "INV-00042"}]'] * 2)
    result = _pipeline("INV-00042 Rs. 1,000.00", provider=provider)
    assert result.method is ExtractionMethod.REGEX_FALLBACK


def test_a_line_with_no_usable_reference_is_dropped() -> None:
    """A reference the matcher cannot look up contributes nothing and would
    only inflate the apparent quality of the extraction."""
    answer = json.dumps(
        {"lines": [{"invoice_reference": "???"}, {"invoice_reference": "INV-00042"}]}
    )
    result = _pipeline("x", provider=ScriptedProvider([answer]))

    assert result.payload.references == ["INV42"]


def test_a_line_claiming_to_overpay_an_invoice_is_dropped() -> None:
    """Paid above gross is not credible; the line goes, the document stays."""
    answer = json.dumps(
        {
            "lines": [
                {
                    "invoice_reference": "INV-00042",
                    "gross_amount": "1000.00",
                    "paid_amount": "9000.00",
                },
                {"invoice_reference": "INV-00043", "gross_amount": "500.00"},
            ]
        }
    )
    result = _pipeline("x", provider=ScriptedProvider([answer]))

    assert result.payload.references == ["INV43"]


def test_gross_and_deduction_imply_the_net_paid() -> None:
    answer = json.dumps(
        {
            "lines": [
                {
                    "invoice_reference": "INV-00042",
                    "gross_amount": "61765.84",
                    "deduction_amount": "4900.00",
                    "deduction_reason": "12 cases damaged in transit",
                }
            ]
        }
    )
    result = _pipeline("x", provider=ScriptedProvider([answer]))

    line = result.payload.lines[0]
    assert line.paid_amount_paise == rupees_to_paise("56865.84")
    assert line.deduction_reason is DeductionReason.DAMAGE


def test_a_claim_with_unrecognisable_wording_becomes_unknown_not_dropped() -> None:
    answer = json.dumps(
        {
            "lines": [
                {
                    "invoice_reference": "INV-00042",
                    "gross_amount": "1000.00",
                    "deduction_amount": "100.00",
                    "deduction_reason": "as per our records",
                }
            ]
        }
    )
    result = _pipeline("x", provider=ScriptedProvider([answer]))

    assert result.payload.lines[0].deduction_reason is DeductionReason.UNKNOWN


# ===========================================================================
# the cross-check -- the guard that makes a model safe to use here
# ===========================================================================


def test_an_extraction_that_ties_to_the_credit_is_trusted() -> None:
    result = _pipeline(
        "INV-00042  Rs. 1,000.00", provider=MockProvider(REFERENCE_CONFIG), amount="1000.00"
    )

    assert result.payload.reconciliation is Reconciliation.TIES
    assert result.payload.is_trustworthy
    assert result.payload.reconciliation_gap_paise == 0


def test_a_hallucinated_amount_fails_the_cross_check() -> None:
    """The guard that matters. A model that invents a figure produces lines
    that do not add up to a real bank credit, and this is where that shows."""
    answer = json.dumps(
        {"lines": [{"invoice_reference": "INV-00042", "gross_amount": "999999.00"}]}
    )
    result = _pipeline("x", provider=ScriptedProvider([answer]), amount="1000.00")

    assert result.payload.reconciliation is Reconciliation.DOES_NOT_TIE
    assert not result.payload.is_trustworthy
    # The references survive: they are often right even when the maths is not.
    assert result.payload.references == ["INV42"]


def test_rounding_inside_tolerance_still_counts_as_tying() -> None:
    answer = json.dumps({"lines": [{"invoice_reference": "INV-00042", "gross_amount": "1000.50"}]})
    result = _pipeline("x", provider=ScriptedProvider([answer]), amount="1000.00")

    assert result.payload.reconciliation is Reconciliation.TIES


def test_without_a_transaction_there_is_nothing_to_check_against() -> None:
    result = _pipeline("INV-00042  Rs. 1,000.00", provider=MockProvider(REFERENCE_CONFIG))

    assert result.payload.reconciliation is Reconciliation.NOT_CHECKED
    assert not result.payload.is_trustworthy


def test_a_document_level_claim_is_netted_off_the_stated_total() -> None:
    """ "INV-1 1000.00" plus "100.00 deducted - damage" means 900 was paid."""
    payload = _regex(
        """INV-00042    Rs. 1,000.00

Rs. 100.00 deducted - 2 cases damaged in transit."""
    )

    assert payload.stated_paid_paise == rupees_to_paise("900.00")
    assert payload.deduction_total_paise == rupees_to_paise("100.00")


def test_an_empty_document_is_reported_as_such() -> None:
    result = _pipeline("", provider=MockProvider(REFERENCE_CONFIG))

    assert result.method is ExtractionMethod.NONE
    assert not result.succeeded
    assert "empty" in (result.error or "")


# ===========================================================================
# determinism and serialisation
# ===========================================================================


def test_extraction_is_reproducible() -> None:
    text = "INV-00042 Rs. 1,000.00\nINV-00043 Rs. 2,000.00"
    first = _pipeline(text, provider=MockProvider(REFERENCE_CONFIG)).as_dict()
    second = _pipeline(text, provider=MockProvider(REFERENCE_CONFIG)).as_dict()

    assert first == second


def test_the_result_serialises_to_what_gets_stored() -> None:
    result = _pipeline("INV-00042 Rs. 1,000.00", provider=MockProvider(REFERENCE_CONFIG))
    payload = result.as_dict()

    assert payload["method"] == "mock"
    assert payload["payload"]["lines"][0]["normalized_reference"] == "INV42"
    # Must survive a JSON round-trip: this goes into a JSONB column.
    assert json.loads(json.dumps(payload)) == payload


def test_every_stored_amount_is_an_integer() -> None:
    result = _pipeline(
        "INV-00042 Rs. 1,000.50", provider=MockProvider(REFERENCE_CONFIG), amount="1000.50"
    )
    body = result.as_dict()["payload"]

    for line in body["lines"]:
        for key in ("gross_amount_paise", "paid_amount_paise", "deduction_amount_paise"):
            assert line[key] is None or isinstance(line[key], int)
    assert body["total_paise"] is None or isinstance(body["total_paise"], int)


def test_the_validated_model_rejects_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="surprise"):
        ExtractedRemittance(lines=[], surprise="nope")


def test_effective_paid_reconciles_both_ways_of_writing_a_line() -> None:
    from_gross = ExtractedLine(
        invoice_reference="INV-1",
        normalized_reference="INV1",
        gross_amount_paise=100_000,
        deduction_amount_paise=10_000,
    )
    from_net = ExtractedLine(
        invoice_reference="INV-1",
        normalized_reference="INV1",
        paid_amount_paise=90_000,
        deduction_amount_paise=10_000,
    )

    assert from_gross.effective_paid_paise == from_net.effective_paid_paise == 90_000


# ===========================================================================
# the live provider -- constructed, not called
# ===========================================================================


def test_live_mode_without_a_key_fails_with_a_useful_message() -> None:
    from cashmatch.extraction import GeminiProvider

    with pytest.raises(ProviderError, match="needs a GEMINI_API_KEY"):
        GeminiProvider("", "gemini-3.8-flash")


def test_provider_selection_follows_llm_mode() -> None:
    from cashmatch.config import LLMMode, Settings
    from cashmatch.extraction import build_provider

    mock = build_provider(Settings(llm_mode=LLMMode.MOCK), CONFIG)
    assert mock is not None and mock.name == "mock"

    assert build_provider(Settings(llm_mode=LLMMode.OFF), CONFIG) is None
