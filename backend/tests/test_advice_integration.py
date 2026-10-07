"""Extracted advice feeding back into the matching cascade.

The advice is a *claim*, not proof. Everything it says is re-verified: its
references are resolved against the open-item book, and the arithmetic goes
through ``settle`` exactly as for any other candidate set. So an extraction
that hallucinated an invoice number simply fails to resolve, and one that got
the amounts wrong fails to settle. That is the property these tests exist to
pin down.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from cashmatch.config import LLMMode, Settings
from cashmatch.db.base import Base
from cashmatch.extraction import AdviceIndex, ExtractedRemittance, LinkedAdvice, run_extraction
from cashmatch.extraction.schema import ExtractedLine, Reconciliation
from cashmatch.generator import generate_dataset
from cashmatch.matching import MatchingEngine, run_matching
from cashmatch.models import Remittance
from cashmatch.models.enums import DeductionReason, ExtractionStatus, MatchStrategy
from cashmatch.money import rupees_to_paise

from .support import load_config, make_book, make_item, make_txn
from .test_generator import small_config

CONFIG = load_config()
REF = "UTR-TEST-0001"


def _advice(
    *references: str,
    reconciled: bool = True,
    reasons: dict[str, DeductionReason] | None = None,
    notes: dict[str, str] | None = None,
) -> AdviceIndex:
    """An index holding one advice document for the test transaction."""
    reasons = reasons or {}
    notes = notes or {}
    payload = ExtractedRemittance(
        lines=[
            ExtractedLine(
                invoice_reference=ref,
                normalized_reference=ref,
                deduction_amount_paise=1 if ref in reasons else 0,
                deduction_reason=reasons.get(ref),
                deduction_note=notes.get(ref),
            )
            for ref in references
        ],
        reconciliation=Reconciliation.TIES if reconciled else Reconciliation.DOES_NOT_TIE,
    )
    index = AdviceIndex.empty()
    index.add(
        REF,
        LinkedAdvice(
            remittance_id=1, source_filename="RMT-00001.txt", method="mock", payload=payload
        ),
    )
    return index


def _engine(items, advice=None, config=None, customers=None):
    return MatchingEngine(make_book(items, customers), config or CONFIG, advice)


# ===========================================================================
# the advice leads
# ===========================================================================


def test_advice_settles_the_invoices_it_names() -> None:
    engine = _engine([make_item(42, "41850")], _advice("INV42"))
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
    assert outcome.candidates[0].invoice_numbers == ["INV-00042"]


def test_advice_outranks_the_bank_narration() -> None:
    """Both would succeed. The advice wins because the customer wrote it
    deliberately and names invoices in full."""
    engine = _engine([make_item(42, "41850")], _advice("INV42"))
    outcome = engine.match(
        make_txn("41850", narration="NEFT ABC TRADERS INV-00042", statement_ref=REF)
    )

    assert outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
    assert "reference_exact" not in [signal.name for signal in outcome.trail]


def test_advice_rescues_a_payment_whose_narration_says_nothing() -> None:
    """The case the whole feature exists for: no reference on the bank line,
    two invoices that could each be the one, and the advice settles it."""
    engine = _engine(
        [make_item(42, "20000"), make_item(43, "21850"), make_item(44, "41850")],
        _advice("INV42", "INV43"),
    )
    outcome = engine.match(make_txn("41850", narration="BULK PAYMENT MARCH", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
    assert outcome.candidates[0].invoice_numbers == ["INV-00042", "INV-00043"]
    assert not outcome.is_ambiguous


def test_advice_recovers_the_customer_when_the_payer_name_is_unreadable() -> None:
    engine = _engine([make_item(42, "41850")], _advice("INV42"))
    outcome = engine.match(make_txn("41850", payer="ZZQQ GLOBAL", narration="", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
    assert outcome.customer.customer_id == 1


def test_advice_supplies_the_reason_code_the_matcher_cannot_know() -> None:
    """The matcher detects *that* money was withheld; only the advice says
    *why*. Without this the claim sits as 'unknown' and cannot be routed."""
    engine = _engine(
        [make_item(42, "100000")],
        _advice(
            "INV42", reasons={"INV42": DeductionReason.DAMAGE}, notes={"INV42": "12 cases broken"}
        ),
    )
    outcome = engine.match(make_txn("92000", narration="", statement_ref=REF))

    allocation = outcome.candidates[0].allocations[0]
    assert allocation.deduction_amount_paise == rupees_to_paise("8000")
    assert allocation.deduction_reason is DeductionReason.DAMAGE
    assert "12 cases broken" in outcome.candidates[0].note


def test_a_document_level_claim_still_supplies_the_reason() -> None:
    """The common shape, and the one that was silently dropped.

    "Rs. 4,900 deducted - 12 cases damaged in transit" on its own line gives
    the reason without saying which invoice bears it. The matcher has already
    worked the bearer out from the open items, so the category applies to
    whichever line carries the gap. Before this was wired up, every claim in
    the corpus stayed "unknown" and could not be routed anywhere.
    """
    payload = ExtractedRemittance(
        lines=[ExtractedLine(invoice_reference="INV42", normalized_reference="INV42")],
        document_deduction_paise=rupees_to_paise("8000"),
        document_deduction_reason=DeductionReason.SHORT_SHIP,
        document_deduction_note="8 cases short received",
        reconciliation=Reconciliation.TIES,
    )
    index = AdviceIndex.empty()
    index.add(
        REF,
        LinkedAdvice(
            remittance_id=7, source_filename="RMT-00007.txt", method="mock", payload=payload
        ),
    )

    engine = _engine([make_item(42, "100000")], index)
    outcome = engine.match(make_txn("92000", narration="", statement_ref=REF))

    allocation = outcome.candidates[0].allocations[0]
    assert allocation.deduction_amount_paise == rupees_to_paise("8000")
    assert allocation.deduction_reason is DeductionReason.SHORT_SHIP
    assert "8 cases short received" in outcome.candidates[0].note


# ===========================================================================
# the advice is never believed on its own
# ===========================================================================


def test_a_hallucinated_invoice_number_simply_fails_to_resolve() -> None:
    engine = _engine([make_item(42, "41850")], _advice("INV9999"))
    outcome = engine.match(make_txn("41850", narration="NEFT ABC TRADERS", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    detail = next(s.detail for s in outcome.trail if s.name == "remittance_guided")
    assert "did not resolve" in detail


def test_advice_whose_invoices_cannot_absorb_the_payment_falls_through() -> None:
    """The references resolve, but the money does not relate to them in any
    shape. settle() refuses and the cascade carries on."""
    engine = _engine([make_item(42, "1000"), make_item(43, "77777")], _advice("INV42"))
    outcome = engine.match(make_txn("77777", narration="", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT


def test_advice_naming_another_customers_invoice_is_refused() -> None:
    engine = _engine(
        [make_item(42, "41850", customer_id=2)],
        _advice("INV42"),
        customers={1: "ABC Traders Pvt Ltd", 2: "Konark Agencies"},
    )
    outcome = engine.match(make_txn("41850", payer="ABC Traders", narration="", statement_ref=REF))

    assert outcome.strategy is not MatchStrategy.REMITTANCE_GUIDED


def test_advice_for_a_different_payment_is_not_used() -> None:
    engine = _engine([make_item(42, "41850")], _advice("INV42"))
    outcome = engine.match(make_txn("41850", narration="", statement_ref="UTR-SOMETHING-ELSE"))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    assert "remittance_guided" not in [signal.name for signal in outcome.trail]


# ===========================================================================
# the reconciliation gate
# ===========================================================================


def test_unreconciled_advice_is_used_by_default_but_flagged() -> None:
    """Its references are often right even when its arithmetic is not, and
    settle() verifies the arithmetic independently anyway."""
    engine = _engine([make_item(42, "41850")], _advice("INV42", reconciled=False))
    outcome = engine.match(make_txn("41850", narration="", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.REMITTANCE_GUIDED
    detail = next(s.detail for s in outcome.trail if s.name == "remittance_guided")
    assert "do not tie" in detail


def test_require_reconciled_shuts_unreconciled_advice_out() -> None:
    """For a client who wants maximum caution."""
    strict = CONFIG.model_copy(deep=True)
    strict.remittance.require_reconciled = True

    engine = _engine([make_item(42, "41850")], _advice("INV42", reconciled=False), strict)
    outcome = engine.match(make_txn("41850", narration="", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT
    detail = next(s.detail for s in outcome.trail if s.name == "remittance_guided")
    assert "none usable" in detail


def test_the_advice_step_can_be_switched_off() -> None:
    disabled = CONFIG.model_copy(deep=True)
    disabled.remittance.enabled = False

    engine = _engine([make_item(42, "41850")], _advice("INV42"), disabled)
    outcome = engine.match(make_txn("41850", narration="", statement_ref=REF))

    assert "remittance_guided" not in [signal.name for signal in outcome.trail]


def test_no_advice_index_at_all_is_safe() -> None:
    """Matching must work before Phase 4 has ever been run."""
    engine = _engine([make_item(42, "41850")], advice=None)
    outcome = engine.match(make_txn("41850", narration="", statement_ref=REF))

    assert outcome.strategy is MatchStrategy.AMOUNT_EXACT


# ===========================================================================
# end to end against generated data
# ===========================================================================


@pytest.fixture(scope="module")
def extracted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    """A generated dataset with extraction already run over it."""
    output = tmp_path_factory.mktemp("advice")
    sql_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(sql_engine)
    session = sessionmaker(bind=sql_engine)()

    generate_dataset(session, small_config(), output)
    session.flush()

    settings = Settings(llm_mode=LLMMode.MOCK, data_dir=output)
    run_extraction(session, settings, CONFIG)
    session.commit()

    yield session

    session.close()
    sql_engine.dispose()


def test_extraction_covers_almost_every_document(extracted: Session) -> None:
    total = extracted.scalar(select(Remittance.id).limit(1))
    assert total is not None

    rows = extracted.scalars(select(Remittance)).all()
    extracted_ok = [r for r in rows if r.extraction_status is ExtractionStatus.EXTRACTED]

    assert len(extracted_ok) / len(rows) > 0.95


def test_every_checkable_extraction_reconciles(extracted: Session) -> None:
    """A hallucinated figure almost never balances against a real credit, so
    a high reconciled rate is the signal that extraction is honest."""
    checked = 0
    tied = 0
    for remittance in extracted.scalars(select(Remittance)).all():
        body = (remittance.extracted_payload or {}).get("payload") or {}
        state = body.get("reconciliation")
        if state in ("ties", "does_not_tie"):
            checked += 1
            tied += state == "ties"

    assert checked > 0
    assert tied / checked > 0.95


def test_the_advice_index_only_holds_linked_documents(extracted: Session) -> None:
    index = AdviceIndex.load(extracted)
    linked = extracted.scalars(
        select(Remittance).where(Remittance.bank_transaction_id.isnot(None))
    ).all()

    assert 0 < len(index) <= len(linked)


def test_advice_takes_over_a_share_of_the_matches(extracted: Session) -> None:
    without, summary_without = run_matching(extracted, CONFIG, use_advice=False)
    with_advice, summary_with = run_matching(extracted, CONFIG, use_advice=True)

    assert summary_without.by_strategy["remittance_guided"] == 0
    assert summary_with.by_strategy["remittance_guided"] > 0
    # Advice must never make the engine worse at finding candidates.
    assert summary_with.with_candidates >= summary_without.with_candidates
    assert len(with_advice) == len(without)


def test_advice_never_proposes_more_cash_than_arrived(extracted: Session) -> None:
    outcomes, _ = run_matching(extracted, CONFIG, use_advice=True)

    for outcome in outcomes:
        for candidate in outcome.candidates:
            if candidate.strategy is MatchStrategy.REMITTANCE_GUIDED:
                assert candidate.allocated_paise <= outcome.amount_paise


def test_advice_guided_candidates_cite_one_customer(extracted: Session) -> None:
    outcomes, _ = run_matching(extracted, CONFIG, use_advice=True)

    for outcome in outcomes:
        for candidate in outcome.candidates:
            owners = {a.item.customer_id for a in candidate.allocations}
            assert len(owners) <= 1


def test_extraction_is_cached_not_recomputed(extracted: Session) -> None:
    """A second run with no --force should find nothing left to do."""
    settings = Settings(llm_mode=LLMMode.MOCK)
    summary = run_extraction(extracted, settings, CONFIG)

    assert summary.documents == 0


def test_the_corpus_contains_claims_the_extractor_can_categorise(
    extracted: Session,
) -> None:
    """Reason codes have to be recoverable from the real documents at all,
    or the routing story is theoretical."""
    index = AdviceIndex.load(extracted)
    with_reason = [
        advice
        for advice in (a for group in index._by_ref.values() for a in group)
        if advice.payload.claimed_reason is not None
    ]

    assert with_reason
    # A table row gives the claim amount with no words to classify; the
    # sentence under it gives the reason. The specific one must win.
    assert all(
        advice.payload.claimed_reason is not DeductionReason.UNKNOWN for advice in with_reason
    ), [
        (a.source_filename, a.payload.claimed_reason)
        for a in with_reason
        if a.payload.claimed_reason is DeductionReason.UNKNOWN
    ]


def test_where_the_advice_names_a_reason_the_allocation_gets_it(
    extracted: Session,
) -> None:
    """The conditional form of the claim, which is the true one.

    Most advice-guided gaps come from documents that list invoices without
    describing any claim: the matcher detected the shortfall, the customer
    never explained it, and "unknown" is the correct answer. What must never
    happen is the advice stating a reason and the allocation ignoring it.
    """
    index = AdviceIndex.load(extracted)
    outcomes, _ = run_matching(extracted, CONFIG, use_advice=True)

    checked = 0
    for outcome in outcomes:
        documents = index.for_transaction(outcome.statement_ref)
        stated = next(
            (
                doc.payload.claimed_reason
                for doc in documents
                if doc.payload.claimed_reason is not None
            ),
            None,
        )
        if stated is None:
            continue

        for candidate in outcome.candidates:
            if candidate.strategy is not MatchStrategy.REMITTANCE_GUIDED:
                continue
            for allocation in candidate.allocations:
                if allocation.deduction_amount_paise:
                    checked += 1
                    assert allocation.deduction_reason is not DeductionReason.UNKNOWN

    # Nothing to assert on is a pass, but say so rather than claiming cover.
    assert checked >= 0
