"""Measuring the engine against the answer key.

The integrity of every number in the report rests on one property: the
matcher never sees the ground truth. Several tests below exist only to pin
that down, because a leak here would not crash anything -- it would just
quietly produce excellent results that mean nothing.

The rest pin down what "correct" means, because every metric inherits that
definition.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cashmatch.config import LLMMode, Settings
from cashmatch.db.base import Base
from cashmatch.evaluation import (
    GroundTruthMissingError,
    Verdict,
    build_report,
    compare,
    evaluate,
    recommend,
    render_markdown,
    render_text,
    sweep_thresholds,
    write_markdown,
)
from cashmatch.extraction import run_extraction
from cashmatch.generator import generate_dataset
from cashmatch.generator.engine import load_ground_truth
from cashmatch.generator.ground_truth import TruthAllocation, TruthPayment
from cashmatch.matching import MatchingEngine
from cashmatch.models.enums import DeductionReason, MatchDecision
from cashmatch.money import rupees_to_paise
from cashmatch.scoring import ConfidenceScorer

from .support import load_config, make_book, make_item, make_txn
from .test_generator import small_config

CONFIG = load_config()


# ===========================================================================
# what "correct" means
# ===========================================================================


def _scored(items, narration="", rupees="41850", **kwargs):
    engine = MatchingEngine(make_book(items), CONFIG)
    outcome = engine.match(make_txn(rupees, narration=narration, **kwargs))
    return ConfidenceScorer(CONFIG.scoring).score(outcome)


def _truth(scenario, *allocations, amount="41850", ref="UTR-TEST-0001") -> TruthPayment:
    return TruthPayment(
        statement_ref=ref,
        scenario=scenario,
        customer_code="CUST-0001",
        amount_paise=rupees_to_paise(amount),
        value_date="2026-03-15",
        allocations=list(allocations),
    )


def _allocation(number: int, rupees: str, deduction: str = "0", reason=None):
    return TruthAllocation(
        invoice_number=f"INV-{number:05d}",
        allocated_amount_paise=rupees_to_paise(rupees),
        deduction_amount_paise=rupees_to_paise(deduction),
        deduction_reason=reason,
    )


def test_the_right_invoices_for_the_right_amounts_is_correct() -> None:
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    result = compare(scored, _truth("exact_single", _allocation(42, "41850")))

    assert result.verdict is Verdict.CORRECT
    assert result.is_correct


def test_the_right_invoices_for_the_wrong_amounts_is_wrong() -> None:
    """A partially right allocation is still a wrong ledger entry.

    The engine cleared INV-00042 in full; the payment really only settled
    part of it. Right invoice, wrong number, wrong ledger.
    """
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    result = compare(scored, _truth("partial_payment", _allocation(42, "30000"), amount="30000"))

    assert result.verdict is Verdict.WRONG
    assert result.invoices_correct is True  # the near miss is recorded apart


def test_a_subset_of_the_right_invoices_is_wrong_not_partial_credit() -> None:
    """Two of three right leaves one invoice open that should be closed and
    closes one that should not be. There is no partial credit in a ledger."""
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    result = compare(
        scored,
        _truth(
            "bundled_multi",
            _allocation(42, "41850"),
            _allocation(43, "9000"),
            amount="50850",
        ),
    )

    assert result.verdict is Verdict.WRONG
    assert result.invoices_correct is False


def test_proposing_nothing_when_something_was_due_is_a_miss() -> None:
    scored = _scored([make_item(42, "41850")], payer="Nobody At All", rupees="7500")
    result = compare(scored, _truth("exact_single", _allocation(42, "7500"), amount="7500"))

    assert result.verdict is Verdict.MISSED


def test_proposing_nothing_when_nothing_was_due_is_a_win() -> None:
    scored = _scored([make_item(42, "41850")], payer="Nobody At All", rupees="7500")
    result = compare(scored, _truth("no_matching_invoice", amount="7500"))

    assert result.verdict is Verdict.CORRECTLY_UNAPPLIED
    assert result.is_correct


def test_inventing_a_match_for_unmatchable_money_is_wrong() -> None:
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    result = compare(scored, _truth("no_matching_invoice"))

    assert result.verdict is Verdict.WRONG


def test_bank_rounding_inside_tolerance_still_counts_as_correct() -> None:
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    truth = _truth("exact_single", _allocation(42, "41850"))
    truth.allocations[0].allocated_amount_paise -= 50

    assert compare(scored, truth, tolerance_paise=100).verdict is Verdict.CORRECT


def test_only_a_wrong_auto_application_counts_as_costly() -> None:
    """Everything else is either right, or in a queue where a human catches
    it. Conflating the two hides the only failure that actually hurts.

    An amount-only match with no reference lands in the review band, so even
    when it is wrong a human sees it before any money moves.
    """
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS")
    result = compare(scored, _truth("missing_reference", _allocation(99, "41850")))

    assert result.verdict is Verdict.WRONG
    assert result.decision is MatchDecision.NEEDS_REVIEW
    assert result.is_costly is False


# --- reason codes are scored separately ------------------------------------


def test_a_gap_detected_but_never_explained_does_not_count_as_classified() -> None:
    """UNKNOWN is an honest answer, not a correct one."""
    scored = _scored(
        [make_item(42, "100000")], narration="NEFT ABC TRADERS INV-00042", rupees="92000"
    )
    truth = _truth(
        "short_pay_deduction",
        _allocation(42, "92000", "8000", DeductionReason.DAMAGE),
        amount="92000",
    )
    result = compare(scored, truth)

    assert result.verdict is Verdict.CORRECT
    assert result.reason_correct is False


def test_a_payment_with_no_claim_is_not_scored_for_reason_accuracy() -> None:
    scored = _scored([make_item(42, "41850")], narration="NEFT ABC TRADERS INV-00042")
    result = compare(scored, _truth("exact_single", _allocation(42, "41850")))

    assert result.reason_correct is None


# ===========================================================================
# the report
# ===========================================================================


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple]:
    """A full generate → extract → match → score → evaluate pass."""
    output = tmp_path_factory.mktemp("evaluation")
    sql_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(sql_engine)
    session = sessionmaker(bind=sql_engine)()

    generate_dataset(session, small_config(), output)
    session.flush()
    run_extraction(session, Settings(llm_mode=LLMMode.MOCK, data_dir=output), CONFIG)
    session.commit()

    report, rows, comparisons = evaluate(session, CONFIG, output)
    yield report, rows, comparisons, session, output

    session.close()
    sql_engine.dispose()


def test_every_payment_is_judged(evaluated) -> None:
    report, _, comparisons, _, _ = evaluated
    assert report.total == small_config().volume.payments
    assert len(comparisons) == report.total


def test_the_decision_counts_reconcile(evaluated) -> None:
    report, _, _, _, _ = evaluated
    assert report.auto_applied + report.review + report.unapplied == report.total
    assert report.auto_correct + report.auto_wrong == report.auto_applied


def test_the_rates_sum_to_one(evaluated) -> None:
    report, _, _, _, _ = evaluated
    total = report.auto_match_rate + report.review_rate + report.unapplied_rate
    assert total == pytest.approx(1.0)


def test_no_payment_is_auto_applied_to_the_wrong_invoice(evaluated) -> None:
    """The headline guarantee. If this ever fails, the engine is posting
    money to invoices it does not settle, and the auto-match rate stops
    being an achievement."""
    report, _, _, _, _ = evaluated
    assert report.auto_wrong == 0
    assert report.auto_precision == 1.0


def test_a_multi_invoice_short_pay_is_never_auto_applied_on_a_guess(
    evaluated,
) -> None:
    """Regression for all 19 errors in the first evaluation.

    Each was a short-pay across several invoices where the right set was
    identified and the claim was attributed to the wrong invoice within it.
    The matcher's "smallest that can absorb the gap" rule is a reasonable
    prior and sometimes simply not what happened. When nothing asserts the
    bearer, the guess belongs in review.
    """
    _, _, comparisons, _, _ = evaluated
    short_pay_errors = [
        item for item in comparisons if item.is_costly and item.scenario == "short_pay_deduction"
    ]
    assert short_pay_errors == []


def test_every_scenario_appears_in_the_breakdown(evaluated) -> None:
    """An averaged number hides the weak spot. Per-scenario is the point."""
    from cashmatch.generator import ScenarioLabel

    report, _, _, _, _ = evaluated
    assert set(report.by_scenario) == {label.value for label in ScenarioLabel}


def test_the_easy_scenarios_are_solved(evaluated) -> None:
    report, _, _, _, _ = evaluated
    for name in ("exact_single", "typo_reference", "partial_payment"):
        assert report.by_scenario[name].accuracy == 1.0, name


def test_unmatchable_payments_are_correctly_left_alone(evaluated) -> None:
    report, _, _, _, _ = evaluated
    metrics = report.by_scenario["no_matching_invoice"]
    assert metrics.auto_applied == 0
    assert metrics.accuracy == 1.0


def test_value_weighted_precision_is_reported(evaluated) -> None:
    """A wrong match on Rs. 9 lakh is not the same event as one on
    Rs. 9,000, and a count-only report hides that completely."""
    report, _, _, _, _ = evaluated
    assert report.total_value_paise > 0
    assert 0 <= report.value_precision <= 1


def test_the_report_records_what_it_measured(evaluated) -> None:
    """An accuracy number traceable to neither a dataset nor a threshold is
    not a result."""
    report, _, _, _, _ = evaluated
    assert report.seed == small_config().seed
    assert report.config_digest.startswith("sha256:")
    assert report.thresholds == (
        CONFIG.scoring.thresholds.auto_apply,
        CONFIG.scoring.thresholds.review_floor,
    )


# ===========================================================================
# the sweep
# ===========================================================================


def test_the_sweep_covers_every_threshold_asked_for(evaluated) -> None:
    report, rows, _, _, _ = evaluated
    assert len(rows) == 11
    assert all(row.total == report.total for row in rows)


def test_raising_the_threshold_never_increases_automation(evaluated) -> None:
    """The curve has to be monotone, or the sweep is measuring noise."""
    _, rows, _, _, _ = evaluated
    rates = [row.auto_rate for row in rows]
    assert rates == sorted(rates, reverse=True)


def test_a_threshold_of_one_automates_least(evaluated) -> None:
    _, rows, _, _, _ = evaluated
    assert rows[-1].threshold == 1.0
    assert rows[-1].auto_rate <= rows[0].auto_rate


def test_the_sweep_agrees_with_the_report_at_the_live_threshold(evaluated) -> None:
    """If these disagree, one of them is lying."""
    report, rows, _, _, _ = evaluated
    live = next(r for r in rows if abs(r.threshold - report.thresholds[0]) < 1e-9)

    assert live.auto_applied == report.auto_applied
    assert live.auto_wrong == report.auto_wrong


def test_the_recommendation_respects_the_precision_floor(evaluated) -> None:
    _, rows, _, _, _ = evaluated
    best = recommend(rows, min_precision=0.99)

    assert best is not None
    assert best.precision >= 0.99


def test_an_impossible_precision_floor_recommends_nothing(evaluated) -> None:
    """Better to say "not available" than to return the least-bad row and
    let someone read it as an endorsement."""
    _, rows, _, _, _ = evaluated
    assert recommend(rows, min_precision=1.0001) is None


def test_the_sweep_does_not_rerun_the_search(evaluated) -> None:
    """Confidence does not depend on the threshold, only the decision does.
    Sweeping has to be cheap or nobody will do it."""
    import time

    _, _, _, session, output = evaluated
    from cashmatch.matching.runner import run_matching
    from cashmatch.scoring import score_outcomes

    outcomes, _unused = run_matching(session, CONFIG)
    scored = score_outcomes(outcomes, CONFIG)
    truth = load_ground_truth(output).by_statement_ref()

    started = time.perf_counter()
    sweep_thresholds(scored, truth, CONFIG.scoring)
    assert time.perf_counter() - started < 5.0


# ===========================================================================
# the answer key is never visible to the matcher
# ===========================================================================


def test_the_matcher_cannot_reach_the_answer_key(evaluated) -> None:
    """Structural, not conventional: the truth lives in a file, and the
    matcher only ever reads the database."""
    from cashmatch.matching.book import OpenItemBook

    _, _, _, session, _ = evaluated
    book = OpenItemBook.load(session)

    serialised = repr(book.items[:50]) + repr(book.by_customer_name)
    for word in ("scenario", "truth", "expected", "exact_single", "short_pay_deduction"):
        assert word not in serialised


def test_evaluation_writes_nothing_to_the_database(evaluated) -> None:
    from sqlalchemy import func, select

    from cashmatch.models import Invoice, MatchResult

    _, _, _, session, output = evaluated
    before = session.scalar(select(func.sum(Invoice.open_amount_paise)))

    evaluate(session, CONFIG, output)

    assert session.scalar(select(func.sum(Invoice.open_amount_paise))) == before
    assert session.scalar(select(func.count()).select_from(MatchResult)) == 0


def test_a_missing_answer_key_is_an_error_not_a_perfect_score(tmp_path) -> None:
    """Silently scoring against nothing would report flawless results."""
    sql_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(sql_engine)
    session = sessionmaker(bind=sql_engine)()

    with pytest.raises(GroundTruthMissingError, match="No answer key found"):
        evaluate(session, CONFIG, tmp_path)

    session.close()


def test_a_stale_answer_key_is_detected(evaluated, tmp_path) -> None:
    """Database and answer key from different seeds would silently score
    most payments as missing."""
    _, _, _, session, _ = evaluated

    other = tmp_path / "other"
    engine2 = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine2)
    session2 = sessionmaker(bind=engine2)()
    generate_dataset(session2, small_config(seed=99), other)
    session2.commit()

    with pytest.raises(GroundTruthMissingError, match="out of step"):
        evaluate(session, CONFIG, other)

    session2.close()
    engine2.dispose()


# ===========================================================================
# rendering
# ===========================================================================


def test_the_text_report_states_the_cost_asymmetry(evaluated) -> None:
    """The report has to explain why precision beats coverage, or the
    numbers get read the wrong way round."""
    report, rows, _, _, _ = evaluated
    text = render_text(report, rows)

    assert "auto-match rate" in text
    assert "precision" in text
    assert "dunning letter" in text
    assert "threshold sweep" in text


def test_the_markdown_report_carries_the_tables(evaluated) -> None:
    report, rows, _, _, _ = evaluated
    markdown = render_markdown(report, rows)

    assert "## Headline" in markdown
    assert "## Accuracy by scenario" in markdown
    assert "## Threshold sweep" in markdown
    assert f"seed `{report.seed}`" in markdown


def test_the_markdown_report_is_written_to_disk(evaluated, tmp_path) -> None:
    report, rows, _, _, _ = evaluated
    path = write_markdown(report, rows, tmp_path)

    assert path.is_file()
    assert path.name == "evaluation.md"
    assert "Auto-match rate" in path.read_text(encoding="utf-8")


def test_an_empty_evaluation_does_not_divide_by_zero() -> None:
    report = build_report([])
    assert report.auto_match_rate == 0.0
    assert report.auto_precision == 0.0
    assert report.overall_accuracy == 0.0
