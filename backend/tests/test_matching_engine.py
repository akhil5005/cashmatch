"""The cascade itself: which step fires, in what order, and what it records.

Two things are being tested here that the unit tests cannot reach. First,
**ordering** -- a reference hit must pre-empt the combinatorial search behind
it, because running a subset-sum when the answer is already certain is pure
waste. Second, **the trail** -- every step that ran has to leave a record,
since a reviewer needs to know the matcher looked for a reference and found
none rather than wondering whether it looked at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cashmatch.db.base import Base
from cashmatch.generator import GeneratorConfig, generate_dataset
from cashmatch.matching import (
    IdentificationMethod,
    MatchingEngine,
    OpenItemBook,
    run_matching,
)
from cashmatch.models.enums import MatchStrategy
from cashmatch.money import rupees_to_paise

from .support import load_config, make_book, make_item, make_txn
from .test_generator import SHIPPED_CONFIG as GENERATOR_CONFIG
from .test_generator import small_config

CONFIG = load_config()


def _engine(items, customers=None, aliases=None) -> MatchingEngine:
    return MatchingEngine(make_book(items, customers, aliases), CONFIG)


def _fired(outcome) -> list[str]:
    return [signal.name for signal in outcome.trail if signal.fired]


def _attempted(outcome) -> list[str]:
    return [signal.name for signal in outcome.trail]


# ===========================================================================
# step 3: reference-led matching
# ===========================================================================


def test_a_clean_reference_settles_the_invoice() -> None:
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))

    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert outcome.candidates[0].invoice_numbers == ["INV-00042"]
    assert outcome.residual_paise == 0
    assert not outcome.is_ambiguous


@pytest.mark.parametrize(
    "narration",
    [
        "NEFT ABC TRADERS INV-00042",
        "NEFT ABC TRADERS inv 42",
        "RTGS CR INVOICE NO. 42",
        "PAYMENT AGAINST INV/42",
        "NEFT ABC TRADERS BILL 42",
    ],
)
def test_typod_references_still_reach_the_right_invoice(narration: str) -> None:
    """Five spellings, one invoice. This is normalisation earning its keep."""
    engine = _engine([make_item(42, "41850"), make_item(43, "9000")])
    outcome = engine.match(make_txn("41850", narration=narration))

    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert outcome.candidates[0].invoice_numbers == ["INV-00042"]


def test_several_references_settle_as_one_bundle() -> None:
    engine = _engine([make_item(42, "30000"), make_item(43, "11850"), make_item(44, "500")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042 INV-00043"))

    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert outcome.candidates[0].invoice_numbers == ["INV-00042", "INV-00043"]


def test_a_reference_hit_pre_empts_the_combinatorial_search() -> None:
    """The whole point of the ordering. If subset-sum ran anyway it would be
    wasted work behind an already-certain answer."""
    engine = _engine([make_item(42, "41850"), make_item(43, "20000"), make_item(44, "21850")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))

    assert "subset_sum" not in _attempted(outcome)
    assert "amount_exact" not in _attempted(outcome)
    assert outcome.diagnostics.get("subset_sum") is None


def test_a_reference_rescues_a_payer_name_nobody_recognises() -> None:
    """The most valuable fallback in the cascade: a mangled name plus a good
    reference is still a match, and the customer is recovered from the
    invoice rather than from the name."""
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(
        make_txn("41850", payer="ZZZQ LOGISTICS GLOBAL", narration="NEFT INV-00042")
    )

    assert outcome.customer.method is IdentificationMethod.REFERENCE
    assert outcome.customer.customer_id == 1
    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert "customer_from_reference" in _fired(outcome)


def test_a_reference_pointing_at_another_customer_is_refused() -> None:
    """Name says one customer, reference says another. Guessing which is
    right is exactly the wrong move."""
    engine = _engine(
        [make_item(42, "41850", customer_id=2)],
        customers={1: "ABC Traders Pvt Ltd", 2: "Konark Agencies"},
    )
    outcome = engine.match(make_txn("41850", payer="ABC Traders", narration="INV-00042"))

    assert not outcome.has_candidates
    detail = next(s.detail for s in outcome.trail if s.name == "reference_exact")
    assert "different customer" in detail


def test_references_spanning_two_customers_cannot_be_one_payment() -> None:
    engine = _engine(
        [make_item(42, "20000", customer_id=1), make_item(43, "21850", customer_id=2)],
        customers={1: "ABC Traders Pvt Ltd", 2: "Konark Agencies"},
    )
    outcome = engine.match(
        make_txn("41850", payer="Nobody At All", narration="INV-00042 INV-00043")
    )

    assert not outcome.has_candidates
    detail = next(s.detail for s in outcome.trail if s.name == "reference_exact")
    assert "different customers" in detail


def test_a_reference_to_an_unknown_invoice_falls_through() -> None:
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-09999"))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    assert "reference_exact" in _attempted(outcome)


def test_a_reference_whose_amount_makes_no_sense_falls_through() -> None:
    """The reference resolves but the money does not relate to it in any
    shape, so the reference reading is abandoned rather than forced."""
    engine = _engine([make_item(42, "1000"), make_item(43, "77777")])
    outcome = engine.match(make_txn("77777", narration="NEFT ABC TRADERS INV-00042"))

    detail = next(s.detail for s in outcome.trail if s.name == "reference_exact")
    assert "does not relate" in detail
    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT


def test_a_reference_led_part_payment_is_recognised() -> None:
    engine = _engine([make_item(42, "100000")])
    outcome = engine.match(make_txn("40000", narration="NEFT ABC TRADERS PART PMT INV-00042"))

    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    allocation = outcome.candidates[0].allocations[0]
    assert allocation.allocated_amount_paise == rupees_to_paise("40000")
    assert "Part payment" in outcome.candidates[0].note


def test_a_reference_led_short_pay_books_the_claim() -> None:
    engine = _engine([make_item(42, "100000")])
    outcome = engine.match(make_txn("92000", narration="NEFT ABC TRADERS INV-00042 LESS DMG CLAIM"))

    assert outcome.strategy is MatchStrategy.REFERENCE_EXACT
    assert outcome.candidates[0].deduction_paise == rupees_to_paise("8000")


def test_a_bare_number_shared_across_customers_is_refused() -> None:
    """ "42" could be either customer's invoice and the payer is unknown, so
    there is nothing to disambiguate with."""
    engine = _engine(
        [make_item(42, "41850", customer_id=1), make_item(42, "41850", customer_id=2)],
        customers={1: "ABC Traders Pvt Ltd", 2: "Konark Agencies"},
    )
    # Both items normalise to INV42; the second overwrites nothing, so look
    # the ambiguity up by digits instead.
    outcome = engine.match(make_txn("41850", payer="Nobody", narration="PMT REF 42"))

    assert outcome.customer.customer_id is None or not outcome.is_ambiguous


# ===========================================================================
# step 4: exact amount
# ===========================================================================


def test_an_exact_amount_match_without_any_reference() -> None:
    engine = _engine([make_item(42, "41850"), make_item(43, "9000")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    assert outcome.candidates[0].invoice_numbers == ["INV-00042"]


def test_two_invoices_of_the_same_value_produce_an_ambiguity() -> None:
    """Nothing in the narration separates them, so both are offered and the
    outcome is flagged ambiguous rather than one being picked."""
    engine = _engine([make_item(42, "41850"), make_item(43, "41850")])
    outcome = engine.match(make_txn("41850", narration="BULK PAYMENT MARCH"))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    assert outcome.is_ambiguous
    assert len(outcome.candidates) == 2


def test_bank_rounding_is_absorbed_by_the_amount_tolerance() -> None:
    engine = _engine([make_item(42, "41850.75")])
    outcome = engine.match(make_txn("41850.00", narration="NEFT ABC TRADERS"))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT


def test_an_invoice_outside_the_date_window_is_not_a_candidate() -> None:
    """A bill from fourteen months ago is not what this transfer settles."""
    engine = _engine([make_item(42, "41850", invoice_date=date(2024, 1, 1))])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert not outcome.has_candidates
    assert "candidate_pool" in _attempted(outcome)


# ===========================================================================
# step 5: subset-sum
# ===========================================================================


def test_a_bundled_payment_without_a_reference_is_found_by_subset_sum() -> None:
    engine = _engine([make_item(42, "30000"), make_item(43, "11850"), make_item(44, "7000")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert outcome.strategy is MatchStrategy.SUBSET_SUM
    assert outcome.candidates[0].invoice_numbers == ["INV-00042", "INV-00043"]


def test_subset_sum_reports_what_the_search_cost() -> None:
    """The pruning should be visible in the output, not taken on trust."""
    # Amounts chosen so no single invoice matches: step 4 must miss and the
    # search must actually run.
    engine = _engine([make_item(i, str(1000 * i + 7)) for i in range(1, 12)])
    outcome = engine.match(make_txn("3021", narration="NEFT ABC TRADERS"))

    diagnostics = outcome.diagnostics["subset_sum"]
    assert diagnostics["pool_size"] == 11
    assert diagnostics["nodes_visited"] > 0
    assert diagnostics["combinations_admitted_by_cap"] > diagnostics["nodes_visited"]
    assert diagnostics["budget_exhausted"] is False


def test_a_bundle_larger_than_the_cap_is_not_found() -> None:
    """Six invoices with k=5. The documented trade: it goes to review rather
    than producing a confident wrong answer."""
    tight = CONFIG.model_copy(deep=True)
    tight.subset_sum.max_combination_size = 2
    engine = MatchingEngine(make_book([make_item(i, "1000") for i in range(1, 6)]), tight)

    outcome = engine.match(make_txn("4000", narration="NEFT ABC TRADERS"))

    assert not outcome.has_candidates
    assert "subset_sum" in _attempted(outcome)


def test_subset_sum_can_be_switched_off() -> None:
    disabled = CONFIG.model_copy(deep=True)
    disabled.subset_sum.enabled = False
    engine = MatchingEngine(make_book([make_item(42, "30000"), make_item(43, "11850")]), disabled)

    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert "subset_sum" not in _attempted(outcome)


# ===========================================================================
# step 6: short-pay
# ===========================================================================


def test_a_short_pay_without_a_reference_is_detected() -> None:
    engine = _engine([make_item(42, "100000"), make_item(43, "3000")])
    outcome = engine.match(make_txn("92000", narration="NEFT ABC TRADERS"))

    assert outcome.strategy is MatchStrategy.SHORT_PAY
    assert outcome.candidates[0].deduction_paise == rupees_to_paise("8000")


def test_the_smallest_credible_claim_is_offered_first() -> None:
    """A 2% deduction is far likelier than a 14% one, so the leading
    candidate should be the least surprising reading."""
    engine = _engine([make_item(42, "100000"), make_item(43, "102000"), make_item(44, "110000")])
    outcome = engine.match(make_txn("98000", narration="NEFT ABC TRADERS"))

    assert outcome.strategy is MatchStrategy.SHORT_PAY
    deductions = [candidate.deduction_paise for candidate in outcome.candidates]
    assert deductions == sorted(deductions)


def test_an_implausible_gap_is_not_called_a_deduction() -> None:
    """Half the invoice missing is not a damage claim."""
    engine = _engine([make_item(42, "100000")])
    outcome = engine.match(make_txn("50000", narration="NEFT ABC TRADERS"))

    assert not outcome.has_candidates
    assert "short_pay" in _attempted(outcome)


def test_short_pay_can_be_switched_off() -> None:
    disabled = CONFIG.model_copy(deep=True)
    disabled.short_pay.enabled = False
    engine = MatchingEngine(make_book([make_item(42, "100000")]), disabled)

    outcome = engine.match(make_txn("92000", narration="NEFT ABC TRADERS"))

    assert "short_pay" not in _attempted(outcome)


# ===========================================================================
# dead ends
# ===========================================================================


def test_an_unknown_payer_with_no_reference_yields_nothing() -> None:
    """Correct outcome: this money becomes unapplied cash."""
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(make_txn("7500", payer="Zenith Global Logistics", narration="NEFT"))

    assert not outcome.has_candidates
    assert outcome.residual_paise == rupees_to_paise("7500")
    assert outcome.strategy is MatchStrategy.NONE


def test_the_amount_strategies_are_skipped_without_a_customer() -> None:
    """All three need a customer to bound the candidate pool; saying so
    beats silently doing nothing."""
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(make_txn("41850", payer="Zenith Global Logistics", narration=""))

    detail = next(s.detail for s in outcome.trail if s.name == "amount_strategies")
    assert "need a known customer" in detail


def test_a_customer_with_no_open_items_is_reported_clearly() -> None:
    engine = _engine([], customers={1: "ABC Traders Pvt Ltd"})
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert not outcome.has_candidates
    detail = next(s.detail for s in outcome.trail if s.name == "candidate_pool")
    assert "no open items" in detail


def test_an_ambiguous_payer_blocks_the_amount_strategies() -> None:
    engine = _engine(
        [make_item(42, "41850", customer_id=1)],
        customers={1: "Konark Stores Pune", 2: "Konark Stores Pura"},
    )
    outcome = engine.match(make_txn("41850", payer="KONARK STORES PUNA", narration=""))

    assert outcome.customer.method is IdentificationMethod.AMBIGUOUS
    assert not outcome.has_candidates


# ===========================================================================
# the trail
# ===========================================================================


def test_every_step_that_ran_leaves_a_record() -> None:
    """A reviewer must be able to see the matcher looked for a reference and
    found none, not merely that it produced nothing."""
    engine = _engine([make_item(42, "99999")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS"))

    assert _attempted(outcome) == [
        "customer_identified",
        "references_found",
        "amount_exact",
        "subset_sum",
        "short_pay",
    ]
    assert all(signal.detail for signal in outcome.trail)


def test_the_outcome_serialises_to_the_explanation_payload() -> None:
    """This dict becomes the seed of Phase 5's explanation JSON, so its shape
    is a contract rather than a debugging convenience."""
    engine = _engine([make_item(42, "41850")])
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))
    payload = outcome.as_dict()

    assert payload["strategy"] == "reference_exact"
    assert payload["customer"]["method"] == "exact"
    assert payload["references"] == ["INV42"]
    assert payload["candidates"][0]["allocations"][0]["invoice_number"] == "INV-00042"
    assert all({"name", "fired", "detail"} <= set(signal) for signal in payload["trail"])


def test_the_engine_never_writes_to_its_inputs() -> None:
    """Matching one transaction must not change the book for the next."""
    items = [make_item(42, "41850"), make_item(43, "9000")]
    book = make_book(items)
    engine = MatchingEngine(book, CONFIG)

    before = [(item.invoice_number, item.open_amount_paise) for item in book.items]
    engine.match(make_txn("41850", narration="NEFT ABC TRADERS INV-00042"))
    after = [(item.invoice_number, item.open_amount_paise) for item in book.items]

    assert before == after


def test_matching_the_same_transaction_twice_gives_the_same_answer() -> None:
    engine = _engine([make_item(i, str(1000 * i)) for i in range(1, 15)])
    txn = make_txn("3000", narration="NEFT ABC TRADERS")

    first = engine.match(txn).as_dict()
    second = engine.match(txn).as_dict()

    first.pop("diagnostics")
    second.pop("diagnostics")
    assert first == second


# ===========================================================================
# against the real generated dataset
# ===========================================================================


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    """A small generated dataset, loaded into SQLite."""
    output = tmp_path_factory.mktemp("matching")
    sql_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(sql_engine)
    session = sessionmaker(bind=sql_engine)()

    generate_dataset(session, small_config(), output)
    session.commit()

    yield session

    session.close()
    sql_engine.dispose()


def test_the_book_loads_every_open_item(generated: Session) -> None:
    book = OpenItemBook.load(generated)

    assert len(book.items) == small_config().volume.invoices
    assert len(book.by_customer_name) == small_config().volume.customers
    assert book.fuzzy_choices  # names and aliases flattened for rapidfuzz


def test_the_engine_runs_over_the_whole_statement(generated: Session) -> None:
    """An end-to-end smoke test. Whether the candidates are *correct* is
    Phase 6's question -- this one only asserts the engine copes with every
    scenario the generator produces without falling over."""
    outcomes, summary = run_matching(generated, CONFIG)

    assert summary.transactions == small_config().volume.payments
    assert len(outcomes) == summary.transactions
    assert summary.candidate_rate > 0.9
    assert summary.budget_exhaustions == 0


def test_every_strategy_in_the_cascade_gets_exercised(generated: Session) -> None:
    """If a strategy never fires on a dataset built to contain its scenario,
    something is wrong with the strategy, not with the data."""
    _, summary = run_matching(generated, CONFIG)

    assert set(summary.by_strategy) == {
        "reference_exact",
        "amount_exact",
        "subset_sum",
        "short_pay",
    }


def test_customers_are_resolved_through_all_three_tiers(generated: Session) -> None:
    _, summary = run_matching(generated, CONFIG)

    assert summary.by_identification["exact"] > summary.by_identification["fuzzy"]
    assert summary.by_identification["alias"] > 0
    assert summary.by_identification["fuzzy"] > 0


def test_no_candidate_allocation_ever_exceeds_what_arrived(generated: Session) -> None:
    """A proposal that applies more cash than the bank sent is incoherent,
    whatever strategy produced it."""
    outcomes, _ = run_matching(generated, CONFIG)

    for outcome in outcomes:
        for candidate in outcome.candidates:
            assert candidate.allocated_paise <= outcome.amount_paise, outcome.statement_ref
            for allocation in candidate.allocations:
                assert allocation.allocated_amount_paise > 0
                assert (
                    allocation.allocated_amount_paise + allocation.deduction_amount_paise
                    <= allocation.item.open_amount_paise
                )


def test_exact_and_bundled_candidates_account_for_every_paise(generated: Session) -> None:
    outcomes, _ = run_matching(generated, CONFIG)

    for outcome in outcomes:
        for candidate in outcome.candidates:
            if candidate.strategy in (MatchStrategy.SUBSET_SUM, MatchStrategy.AMOUNT_EXACT):
                assert candidate.allocated_paise == outcome.amount_paise


def test_candidates_only_ever_cite_one_customer(generated: Session) -> None:
    outcomes, _ = run_matching(generated, CONFIG)

    for outcome in outcomes:
        for candidate in outcome.candidates:
            owners = {a.item.customer_id for a in candidate.allocations}
            assert len(owners) <= 1


def test_the_whole_run_is_reproducible(generated: Session) -> None:
    first, _ = run_matching(generated, CONFIG)
    second, _ = run_matching(generated, CONFIG)

    assert [outcome.as_dict()["candidates"] for outcome in first] == [
        outcome.as_dict()["candidates"] for outcome in second
    ]


def test_matching_writes_nothing_to_the_database(generated: Session) -> None:
    """Phase 3 proposes; Phase 5 decides and persists. Nothing here may
    change a row."""
    from sqlalchemy import func, select

    from cashmatch.models import Invoice, MatchResult

    before_open = generated.scalar(select(func.sum(Invoice.open_amount_paise)))
    run_matching(generated, CONFIG)

    assert generated.scalar(select(func.sum(Invoice.open_amount_paise))) == before_open
    assert generated.scalar(select(func.count()).select_from(MatchResult)) == 0


def test_a_single_transaction_can_be_matched_by_reference(generated: Session) -> None:
    from sqlalchemy import select

    from cashmatch.models import BankTransaction

    ref = generated.scalar(select(BankTransaction.statement_ref).limit(1))
    outcomes, summary = run_matching(generated, CONFIG, statement_ref=ref)

    assert summary.transactions == 1
    assert outcomes[0].statement_ref == ref


def test_the_shipped_generator_config_is_still_the_one_being_matched() -> None:
    """Guards against the matching tests drifting onto a different dataset
    shape than the one the README and brief quote."""
    assert GeneratorConfig.from_yaml(GENERATOR_CONFIG).volume.payments == 800
    assert Path(GENERATOR_CONFIG).name == "scenarios.yml"
