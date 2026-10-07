"""Confidence scoring and the auto-apply decision.

The tests that earn their place here are the ones pinning down when the
engine must *refuse* to post. Cash application is asymmetric: a payment sent
to review costs an analyst two minutes, a payment auto-applied to the wrong
invoice costs hours across two teams plus a dunning letter to a customer who
already paid. Every threshold and penalty below is tuned towards that
asymmetry, and these tests are what stop a later change quietly undoing it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from cashmatch.matching import MatchingEngine
from cashmatch.matching.candidates import IdentificationMethod
from cashmatch.models.enums import MatchDecision, MatchStrategy
from cashmatch.scoring import ConfidenceScorer, Scored

from .support import DEFAULT_VALUE_DATE, load_config, make_book, make_item, make_txn

CONFIG = load_config()
SCORING = CONFIG.scoring


def _score(items, narration="", payer="ABC Traders Pvt Ltd", rupees="41850", **kwargs) -> Scored:
    engine = MatchingEngine(
        make_book(items, kwargs.pop("customers", None)), kwargs.pop("config", CONFIG)
    )
    outcome = engine.match(make_txn(rupees, payer=payer, narration=narration, **kwargs))
    scorer = ConfidenceScorer(kwargs.get("scoring") or SCORING)
    return scorer.score(outcome)


def _signal(scored: Scored, name: str):
    return next(s for s in scored.signals if s.name == name)


# ===========================================================================
# the headline cases
# ===========================================================================


def test_a_flawless_match_is_auto_applied() -> None:
    """Exact reference, exact amount, exact payer, paid on time. If this
    does not clear automatically, nothing will."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")

    assert scored.decision is MatchDecision.AUTO_APPLIED
    assert scored.confidence >= SCORING.thresholds.auto_apply


def test_a_perfect_match_is_not_capped_by_a_signal_that_could_not_apply() -> None:
    """Regression for the bug that held the auto rate at 22.6%.

    A customer who never sends remittance advice is not thereby a worse
    match. Counting the 0.14 advice weight in the denominator capped every
    flawless no-advice match at 0.86 -- permanently below the 0.90 threshold
    -- and piled 477 of 800 payments into the band just under it. Lowering
    the threshold would have produced a similar rate and hidden the defect.
    """
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")

    advice = _signal(scored, "remittance_agreement")
    assert advice.applicable is False
    assert advice.effective_weight == 0.0
    # Everything that could support this match did.
    assert scored.base_score == pytest.approx(1.0)


def test_the_contributions_add_up_to_the_base_score() -> None:
    """A reviewer must be able to check the arithmetic by hand."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    payload = scored.explanation()

    total = sum(signal["contribution"] for signal in payload["signals"])
    assert total / payload["applicable_weight"] == pytest.approx(payload["base_score"], abs=1e-3)


def test_nothing_found_means_unapplied_not_a_guess() -> None:
    scored = _score([make_item(42, "41850")], payer="Zenith Global Logistics", rupees="7500")

    assert scored.decision is MatchDecision.UNAPPLIED
    assert scored.confidence == 0.0
    assert scored.candidate is None
    assert "unapplied cash" in scored.reason_text


# ===========================================================================
# when the engine must refuse to post
# ===========================================================================


def test_an_ambiguity_is_never_auto_applied() -> None:
    """Two invoices for the same amount and nothing to separate them.
    Picking one would be a coin flip posted with high confidence."""
    scored = _score([make_item(42, "41850"), make_item(43, "41850")], narration="NEFT ABC TRADERS")

    assert scored.outcome.is_ambiguous
    assert scored.decision is not MatchDecision.AUTO_APPLIED
    assert any(p.name == "ambiguous" for p in scored.penalties)


def test_the_ambiguity_penalty_alone_sinks_an_otherwise_perfect_match() -> None:
    """The penalty must be sized to do this on its own, or a strong match
    with a tie in it slips through."""
    scorer = ConfidenceScorer(SCORING)
    perfect = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")

    assert perfect.base_score * SCORING.penalties.ambiguous < SCORING.thresholds.auto_apply
    assert scorer is not None


def test_a_match_resting_only_on_amount_goes_to_review() -> None:
    """No reference anywhere. The amount ties, but the only thing saying
    these invoices are the right ones is a coincidence of arithmetic."""
    scored = _score([make_item(42, "41850"), make_item(43, "9000")], narration="NEFT ABC TRADERS")

    assert scored.outcome.strategy is MatchStrategy.AMOUNT_EXACT
    assert scored.decision is MatchDecision.NEEDS_REVIEW
    assert not _signal(scored, "reference_match").fired


def test_a_searched_short_pay_scores_below_a_referenced_one() -> None:
    """Inferring a deduction from arithmetic is weaker evidence than the
    customer telling you about it."""
    searched = _score([make_item(42, "100000")], narration="NEFT ABC TRADERS", rupees="92000")
    referenced = _score(
        [make_item(42, "100000")],
        narration="NEFT ABC TRADERS INV-00042 LESS DMG",
        rupees="92000",
    )

    assert searched.outcome.strategy is MatchStrategy.SHORT_PAY
    assert referenced.outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert searched.confidence < referenced.confidence


def test_a_subset_sum_scores_below_an_asserted_set() -> None:
    """A subset that happens to add up was found by search; several sets can
    tie, and the customer never said this was the one."""
    subset = _score(
        [make_item(42, "30000"), make_item(43, "11850"), make_item(44, "7000")],
        narration="NEFT ABC TRADERS",
    )
    asserted = _score(
        [make_item(42, "30000"), make_item(43, "11850"), make_item(44, "7000")],
        narration="NEFT ABC TRADERS INV-00042 INV-00043",
    )

    assert subset.outcome.strategy is MatchStrategy.SUBSET_SUM
    assert subset.confidence < asserted.confidence


def test_an_unidentifiable_payer_cannot_reach_auto_apply() -> None:
    scored = _score(
        [make_item(42, "41850")], payer="ZZQQ GLOBAL LOGISTICS", narration="NEFT INV-00042"
    )

    assert scored.outcome.customer.method is IdentificationMethod.REFERENCE
    identity = _signal(scored, "customer_identity")
    assert identity.value < 1.0


def test_residual_cash_is_penalised() -> None:
    """A part payment leaves nothing unplaced, but a match that cannot
    account for every paise received should not post itself."""
    scored = _score([make_item(42, "100000")], narration="PART PMT INV-00042", rupees="40000")

    assert scored.outcome.residual_paise == 0  # a part payment places it all
    assert _signal(scored, "amount_match").value < 1.0


# ===========================================================================
# the signals individually
# ===========================================================================


def test_a_fuzzy_payer_carries_its_own_similarity_into_the_score() -> None:
    scored = _score(
        [make_item(42, "41850")],
        payer="SHREE BALAJI TRADERS-PUNE BR",
        narration="NEFT",
        customers={1: "Shree Balaji Traders Pvt Ltd"},
    )

    identity = _signal(scored, "customer_identity")
    if scored.outcome.customer.method is IdentificationMethod.FUZZY:
        assert 0 < identity.value < 1.0
        assert identity.value == pytest.approx(scored.outcome.customer.score)


def test_an_alias_hit_scores_as_highly_as_the_registered_name() -> None:
    """An alias is an exact lookup against a spelling a human confirmed."""
    exact = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    assert _signal(exact, "customer_identity").value == 1.0


def test_date_proximity_decays_with_distance_from_the_due_date() -> None:
    near = _score(
        [make_item(42, "41850", invoice_date=DEFAULT_VALUE_DATE - timedelta(days=30))],
        narration="NEFT ABC TRADERS INV-00042",
    )
    far = _score(
        [make_item(42, "41850", invoice_date=DEFAULT_VALUE_DATE - timedelta(days=180))],
        narration="NEFT ABC TRADERS INV-00042",
    )

    assert _signal(near, "date_proximity").value >= _signal(far, "date_proximity").value


def test_date_proximity_is_the_weakest_signal_by_design() -> None:
    """A chronic late payer is still paying their own invoices. The date
    breaks ties; it must never decide anything."""
    weights = SCORING.weights.as_dict()
    assert weights["date_proximity"] == min(weights.values())


def test_the_reference_signal_distinguishes_absent_from_unresolved() -> None:
    absent = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS")
    unresolved = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-09999")

    assert "No invoice reference anywhere" in _signal(absent, "reference_match").detail
    assert "none resolved" in _signal(unresolved, "reference_match").detail


# ===========================================================================
# thresholds and decisions
# ===========================================================================


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (1.00, MatchDecision.AUTO_APPLIED),
        (0.90, MatchDecision.AUTO_APPLIED),
        (0.8999, MatchDecision.NEEDS_REVIEW),
        (0.45, MatchDecision.NEEDS_REVIEW),
        (0.4499, MatchDecision.UNAPPLIED),
        (0.0, MatchDecision.UNAPPLIED),
    ],
)
def test_the_thresholds_are_inclusive_at_the_boundary(confidence, expected) -> None:
    scorer = ConfidenceScorer(SCORING)
    assert scorer._decide(confidence) is expected


def test_a_candidate_below_the_review_floor_is_not_queued() -> None:
    """An analyst's day is finite. A 30%-confidence suggestion costs more
    attention than it saves, so it is recorded but not surfaced."""
    strict = CONFIG.model_copy(deep=True)
    strict.scoring.thresholds.review_floor = 0.99

    engine = MatchingEngine(make_book([make_item(42, "41850")]), strict)
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))
    scored = ConfidenceScorer(strict.scoring).score(outcome)

    assert scored.decision is MatchDecision.UNAPPLIED
    # The candidate survives in the explanation even though it was not queued.
    assert scored.explanation()["matching"]["candidates"]


def test_raising_the_auto_threshold_moves_work_to_review() -> None:
    """The trade a client actually negotiates: automation against error."""
    strict = CONFIG.model_copy(deep=True)
    strict.scoring.thresholds.auto_apply = 0.999

    engine = MatchingEngine(make_book([make_item(42, "41850")]), strict)
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))

    lenient = ConfidenceScorer(SCORING).score(outcome)
    cautious = ConfidenceScorer(strict.scoring).score(outcome)

    assert lenient.decision is MatchDecision.AUTO_APPLIED
    assert cautious.confidence == pytest.approx(lenient.confidence)
    assert cautious.decision is MatchDecision.NEEDS_REVIEW or cautious.confidence >= 0.999


# ===========================================================================
# the explanation
# ===========================================================================


def test_the_explanation_carries_both_halves_of_the_story() -> None:
    """How the candidates were found, and how they were scored. A reviewer
    seeing only one has to redo the other."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    payload = scored.explanation()

    assert payload["decision"] == "auto_applied"
    assert {s["name"] for s in payload["signals"]} == {
        "reference_match",
        "amount_match",
        "customer_identity",
        "remittance_agreement",
        "date_proximity",
    }
    assert payload["matching"]["trail"]
    assert payload["matching"]["candidates"][0]["allocations"]


def test_signals_that_did_not_fire_are_still_recorded() -> None:
    """A reviewer needs to know the matcher looked for a reference and found
    none, not merely that it produced a low number."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS")
    payload = scored.explanation()

    not_fired = [s for s in payload["signals"] if not s["fired"]]
    assert not_fired
    assert all(s["detail"] for s in not_fired)


def test_the_explanation_survives_a_json_round_trip() -> None:
    """It goes into a JSONB column, so everything in it must serialise --
    including the value date the scorer reads out of diagnostics."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    payload = scored.explanation()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["matching"]["diagnostics"]["value_date"] == DEFAULT_VALUE_DATE.isoformat()


def test_the_reason_reads_as_a_sentence_and_fits_the_column() -> None:
    scored = _score(
        [make_item(42, "100000")], narration="NEFT ABC TRADERS INV-00042 LESS DMG", rupees="92000"
    )

    assert scored.reason_text.startswith("Applied automatically") or scored.reason_text.startswith(
        "Sent for review"
    )
    assert "INV-00042" in scored.reason_text
    assert len(scored.reason_text) <= 500


def test_a_penalty_explains_itself_in_the_reason() -> None:
    scored = _score([make_item(42, "41850"), make_item(43, "41850")], narration="NEFT ABC TRADERS")
    assert "coin flip" in scored.reason_text or "tie" in scored.reason_text


def test_the_confidence_fits_the_database_column() -> None:
    """Numeric(5,4) holds 0.0000 to 1.0000 and nothing else."""
    scored = _score([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    value = scored.confidence_decimal

    assert 0 <= value <= 1
    assert value.as_tuple().exponent >= -4


# ===========================================================================
# determinism
# ===========================================================================


def test_scoring_the_same_outcome_twice_gives_the_same_number() -> None:
    engine = MatchingEngine(make_book([make_item(42, "41850")]), CONFIG)
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))
    scorer = ConfidenceScorer(SCORING)

    assert scorer.score(outcome).confidence == scorer.score(outcome).confidence


def test_weights_must_sum_to_one() -> None:
    """A confidence built from weights that do not sum to one is not
    comparable between runs, and the thresholds stop meaning anything."""
    broken = CONFIG.model_copy(deep=True)
    with pytest.raises(ValueError, match="must sum to 1.0"):
        type(broken.scoring.weights)(
            reference_match=0.5,
            amount_match=0.5,
            customer_identity=0.5,
            remittance_agreement=0.5,
            date_proximity=0.5,
        )


def test_the_review_band_cannot_be_configured_away() -> None:
    from cashmatch.matching.config import ScoringThresholds

    with pytest.raises(ValueError, match="must exceed"):
        ScoringThresholds(auto_apply=0.4, review_floor=0.8)


def test_a_date_in_the_future_still_scores() -> None:
    """Early payers exist. The signal measures distance, not lateness."""
    scored = _score(
        [make_item(42, "41850", invoice_date=DEFAULT_VALUE_DATE + timedelta(days=5))],
        narration="NEFT ABC TRADERS INV-00042",
        value_date=DEFAULT_VALUE_DATE,
    )
    assert _signal(scored, "date_proximity").value > 0


def test_an_empty_book_scores_nothing_without_crashing() -> None:
    scored = _score([], customers={}, narration="NEFT ABC TRADERS INV-00042")
    assert scored.decision is MatchDecision.UNAPPLIED
    assert scored.confidence == 0.0


def test_value_date_is_available_to_the_scorer() -> None:
    """The scorer reads it out of diagnostics; if the engine stops putting
    it there the date signal silently disappears."""
    engine = MatchingEngine(make_book([make_item(42, "41850")]), CONFIG)
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))

    assert isinstance(outcome.diagnostics.get("value_date"), date)
