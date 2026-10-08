"""The generated dataset is the ground against which every later phase is
measured, so its internal consistency is worth testing harder than most code.

Three properties matter above the rest:

* **Reproducibility.** The same seed must produce byte-identical data, or
  yesterday's accuracy number cannot be compared with today's.
* **The answer key balances.** Every paise of every payment is accounted for,
  or "was the matcher right?" has no answer.
* **The scenario labels are honest.** A payment labelled ``typo_reference``
  must really carry a mangled reference; otherwise Phase 6's per-scenario
  breakdown measures nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from random import Random

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cashmatch.db.base import Base
from cashmatch.generator import (
    GeneratorConfig,
    GroundTruth,
    ScenarioLabel,
    database_is_populated,
    generate_dataset,
    reset_database,
)
from cashmatch.generator.config import VolumeConfig
from cashmatch.generator.parties import (
    build_customers,
    build_unknown_payer,
    clean_payer_variant,
    hard_payer_variant,
)
from cashmatch.models import (
    AuditLog,
    BankTransaction,
    Customer,
    CustomerAlias,
    Invoice,
    InvoiceStatus,
    Remittance,
    RemittanceLinkSource,
    TransactionStatus,
)
from cashmatch.normalize import normalize_party_name, normalize_reference

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "scenarios.yml"


def small_config(seed: int = 42) -> GeneratorConfig:
    """The shipped config at a fraction of the volume, for fast tests."""
    config = GeneratorConfig.from_yaml(SHIPPED_CONFIG).model_copy(deep=True)
    config.seed = seed
    config.volume = VolumeConfig(customers=12, invoices=400, payments=150)
    return config


@pytest.fixture(scope="module")
def dataset(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Session, Path, GroundTruth]]:
    """One generated dataset, shared across the read-only tests below."""
    output = tmp_path_factory.mktemp("dataset")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    generate_dataset(session, small_config(), output)
    session.commit()

    truth = GroundTruth.read(output / "ground_truth" / "ground_truth.json")
    yield session, output, truth

    session.close()
    engine.dispose()


def _fresh_session(engine: Engine | None = None) -> Session:
    engine = engine or create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# --- reproducibility --------------------------------------------------------


def test_same_seed_produces_identical_data(tmp_path: Path) -> None:
    digests = []
    for run in ("a", "b"):
        output = tmp_path / run
        session = _fresh_session()
        generate_dataset(session, small_config(seed=7), output)
        session.commit()
        payload = json.loads((output / "ground_truth" / "ground_truth.json").read_text())
        # generated_at is wall-clock, so it is excluded from the comparison.
        payload.pop("generated_at")
        digests.append(json.dumps(payload, sort_keys=True))
        session.close()

    assert digests[0] == digests[1]


def test_a_different_seed_produces_different_data(tmp_path: Path) -> None:
    results = []
    for run, seed in (("a", 7), ("b", 8)):
        output = tmp_path / run
        session = _fresh_session()
        generate_dataset(session, small_config(seed=seed), output)
        session.commit()
        payload = json.loads((output / "ground_truth" / "ground_truth.json").read_text())
        results.append([p["statement_ref"] for p in payload["payments"]])
        session.close()

    assert results[0] != results[1]


# --- volumes and mix --------------------------------------------------------


def test_volumes_match_the_config(dataset) -> None:
    session, _, truth = dataset
    config = small_config()

    assert session.scalar(select(func.count()).select_from(Customer)) == config.volume.customers
    assert session.scalar(select(func.count()).select_from(Invoice)) == config.volume.invoices
    assert (
        session.scalar(select(func.count()).select_from(BankTransaction)) == config.volume.payments
    )
    assert len(truth.payments) == config.volume.payments


def test_scenario_counts_match_the_configured_percentages(dataset) -> None:
    """Largest-remainder allocation, so the parts sum to exactly the total
    rather than losing a few payments to rounding."""
    _, _, truth = dataset
    config = small_config()
    total = config.volume.payments

    assert sum(truth.scenario_counts.values()) == total
    for name, weight in config.scenarios.as_weights().items():
        expected = total * weight / 100
        # Largest remainder keeps every bucket within one of its exact share.
        assert abs(truth.scenario_counts[name] - expected) < 1.0


def test_the_shipped_config_produces_the_advertised_volumes(tmp_path: Path) -> None:
    """A slower test against the real config, because the README quotes these
    numbers and they should not silently drift."""
    session = _fresh_session()
    summary = generate_dataset(session, GeneratorConfig.from_yaml(SHIPPED_CONFIG), tmp_path)
    session.commit()

    assert (summary.customers, summary.invoices, summary.payments) == (50, 2000, 800)
    assert summary.scenario_counts == {
        "exact_single": 272,
        "bundled_multi": 144,
        "short_pay_deduction": 112,
        "partial_payment": 80,
        "missing_reference": 80,
        "typo_reference": 64,
        "payer_name_variant": 32,
        "no_matching_invoice": 16,
    }
    session.close()


# --- the database is handed over un-reconciled -------------------------------


def test_every_invoice_starts_fully_open(dataset) -> None:
    """The matcher receives the pre-matching state. If invoices arrived
    partly cleared, the problem would already be half solved."""
    session, _, _ = dataset
    invoices = session.scalars(select(Invoice)).all()

    assert all(inv.open_amount_paise == inv.amount_paise for inv in invoices)
    assert all(inv.status is InvoiceStatus.OPEN for inv in invoices)


def test_every_payment_starts_unmatched(dataset) -> None:
    session, _, _ = dataset
    statuses = {txn.status for txn in session.scalars(select(BankTransaction)).all()}
    assert statuses == {TransactionStatus.UNMATCHED}


def test_generation_is_recorded_in_the_audit_log(dataset) -> None:
    session, _, _ = dataset
    entry = session.scalar(select(AuditLog).where(AuditLog.action == "dataset_generated"))

    assert entry is not None
    assert entry.payload_after["seed"] == 42
    assert entry.payload_after["config_digest"].startswith("sha256:")


# --- the answer key balances -------------------------------------------------


def test_every_payment_is_fully_accounted_for(dataset) -> None:
    _, _, truth = dataset
    for payment in truth.payments:
        if payment.scenario is ScenarioLabel.NO_MATCHING_INVOICE:
            assert payment.allocations == []
            continue
        applied = sum(a.allocated_amount_paise for a in payment.allocations)
        assert applied == payment.amount_paise, payment.statement_ref


def test_no_invoice_is_claimed_by_two_payments(dataset) -> None:
    """Exclusive consumption keeps the answer key unambiguous. Without it,
    two payments could both legitimately claim the same invoice."""
    _, _, truth = dataset
    claimed: list[str] = [
        allocation.invoice_number
        for payment in truth.payments
        for allocation in payment.allocations
    ]
    assert len(claimed) == len(set(claimed))


def test_every_referenced_invoice_exists(dataset) -> None:
    session, _, truth = dataset
    known = set(session.scalars(select(Invoice.invoice_number)).all())
    referenced = {
        allocation.invoice_number
        for payment in truth.payments
        for allocation in payment.allocations
    }
    assert referenced <= known


def test_all_money_in_the_answer_key_is_integer_paise(dataset) -> None:
    _, output, _ = dataset
    payload = json.loads((output / "ground_truth" / "ground_truth.json").read_text())

    for payment in payload["payments"]:
        assert isinstance(payment["amount_paise"], int)
        for allocation in payment["allocations"]:
            assert isinstance(allocation["allocated_amount_paise"], int)
            assert isinstance(allocation["deduction_amount_paise"], int)


def test_the_answer_key_round_trips(dataset) -> None:
    _, output, truth = dataset
    reloaded = GroundTruth.read(output / "ground_truth" / "ground_truth.json")

    assert reloaded.seed == truth.seed
    assert reloaded.config_digest == truth.config_digest
    assert len(reloaded.by_statement_ref()) == len(truth.payments)


# --- the scenario labels are honest -----------------------------------------


def _invoice_amounts(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Invoice.invoice_number, Invoice.amount_paise)).all())


def test_exact_single_settles_one_invoice_in_full(dataset) -> None:
    session, _, truth = dataset
    amounts = _invoice_amounts(session)

    for payment in truth.payments:
        if payment.scenario is not ScenarioLabel.EXACT_SINGLE:
            continue
        assert len(payment.allocations) == 1
        allocation = payment.allocations[0]
        assert allocation.allocated_amount_paise == amounts[allocation.invoice_number]
        assert allocation.deduction_amount_paise == 0


def test_bundled_payments_cover_several_invoices(dataset) -> None:
    _, _, truth = dataset
    config = small_config()

    bundles = [p for p in truth.payments if p.scenario is ScenarioLabel.BUNDLED_MULTI]
    assert bundles
    for payment in bundles:
        assert config.bundle.min_invoices <= len(payment.allocations) <= config.bundle.max_invoices


def test_partial_payments_leave_money_on_the_invoice(dataset) -> None:
    session, _, truth = dataset
    amounts = _invoice_amounts(session)

    partials = [p for p in truth.payments if p.scenario is ScenarioLabel.PARTIAL_PAYMENT]
    assert partials
    for payment in partials:
        allocation = payment.allocations[0]
        assert 0 < allocation.allocated_amount_paise < amounts[allocation.invoice_number]


def test_short_pays_clear_the_invoice_with_cash_plus_a_reasoned_deduction(dataset) -> None:
    """The defining property of a short-pay: cash applied plus the claim must
    still equal what was billed. The customer is not underpaying, they are
    asserting a deduction."""
    session, _, truth = dataset
    amounts = _invoice_amounts(session)

    short_pays = [p for p in truth.payments if p.scenario is ScenarioLabel.SHORT_PAY_DEDUCTION]
    assert short_pays
    for payment in short_pays:
        assert payment.deduction_total_paise > 0
        for allocation in payment.allocations:
            billed = amounts[allocation.invoice_number]
            assert allocation.allocated_amount_paise + allocation.deduction_amount_paise == billed
            if allocation.deduction_amount_paise:
                assert allocation.deduction_reason is not None
            assert allocation.allocated_amount_paise > 0


def test_missing_reference_payments_carry_no_reference_at_all(dataset) -> None:
    """If a 'missing reference' narration still contained a number the matcher
    could use, the scenario would be measuring the wrong thing."""
    session, _, truth = dataset
    narrations = dict(
        session.execute(select(BankTransaction.statement_ref, BankTransaction.narration)).all()
    )

    blanks = [p for p in truth.payments if p.scenario is ScenarioLabel.MISSING_REFERENCE]
    assert blanks
    for payment in blanks:
        narration = narrations[payment.statement_ref]
        assert not any(char.isdigit() for char in narration), narration


def test_typo_references_never_contain_the_canonical_number(dataset) -> None:
    session, _, truth = dataset
    narrations = dict(
        session.execute(select(BankTransaction.statement_ref, BankTransaction.narration)).all()
    )

    typos = [p for p in truth.payments if p.scenario is ScenarioLabel.TYPO_REFERENCE]
    assert typos
    for payment in typos:
        canonical = payment.allocations[0].invoice_number
        tokens = narrations[payment.statement_ref].split()
        assert canonical not in tokens


def test_typo_references_include_both_recoverable_and_hard_cases(dataset) -> None:
    """Normalisation should rescue most typos but not all. If every typo
    normalised cleanly the scenario would be trivial; if none did, it would
    be unwinnable."""
    session, _, truth = dataset
    narrations = dict(
        session.execute(select(BankTransaction.statement_ref, BankTransaction.narration)).all()
    )

    recoverable = 0
    hard = 0
    for payment in truth.payments:
        if payment.scenario is not ScenarioLabel.TYPO_REFERENCE:
            continue
        target = normalize_reference(payment.allocations[0].invoice_number)
        tokens = narrations[payment.statement_ref].split()
        if any(normalize_reference(token) == target for token in tokens):
            recoverable += 1
        else:
            hard += 1

    assert recoverable > 0
    assert hard > 0


def test_payer_name_variants_are_not_on_file(dataset) -> None:
    """The point of this scenario is to force fuzzy matching, so the payer
    spelling must match neither the registered name nor any known alias."""
    session, _, truth = dataset
    payers = dict(
        session.execute(
            select(BankTransaction.statement_ref, BankTransaction.payer_name_normalized)
        ).all()
    )
    on_file = set(session.scalars(select(Customer.normalized_name)).all())
    on_file |= set(session.scalars(select(CustomerAlias.normalized_alias)).all())

    variants = [p for p in truth.payments if p.scenario is ScenarioLabel.PAYER_NAME_VARIANT]
    assert variants
    for payment in variants:
        assert payers[payment.statement_ref] not in on_file


def test_unmatchable_payments_come_from_strangers(dataset) -> None:
    session, _, truth = dataset
    payers = dict(
        session.execute(
            select(BankTransaction.statement_ref, BankTransaction.payer_name_normalized)
        ).all()
    )
    known = set(session.scalars(select(Customer.normalized_name)).all())

    strangers = [p for p in truth.payments if p.scenario is ScenarioLabel.NO_MATCHING_INVOICE]
    assert strangers
    for payment in strangers:
        assert payment.customer_code is None
        assert payment.allocations == []
        assert payers[payment.statement_ref] not in known


def test_most_payers_are_resolvable_without_fuzzy_matching(dataset) -> None:
    """Normalisation plus the alias table should handle the large majority.
    If it did not, the dataset would be unrealistically hostile."""
    session, _, _ = dataset
    on_file = set(session.scalars(select(Customer.normalized_name)).all())
    on_file |= set(session.scalars(select(CustomerAlias.normalized_alias)).all())

    payers = session.scalars(select(BankTransaction.payer_name_normalized)).all()
    resolvable = sum(1 for payer in payers if payer in on_file)

    assert resolvable / len(payers) > 0.8


# --- remittance advice -------------------------------------------------------


def test_advice_files_exist_on_disk_for_every_remittance(dataset) -> None:
    session, output, _ = dataset
    directory = output / "generated" / "remittances"

    remittances = session.scalars(select(Remittance)).all()
    assert remittances
    for advice in remittances:
        assert (directory / advice.source_filename).is_file()


def test_advice_coverage_is_near_the_configured_share(dataset) -> None:
    session, _, truth = dataset
    config = small_config()

    eligible = [p for p in truth.payments if p.scenario is not ScenarioLabel.NO_MATCHING_INVOICE]
    advised = session.scalar(select(func.count()).select_from(Remittance))

    assert abs(advised / len(eligible) * 100 - config.remittance.coverage_pct) < 12


def test_only_advice_quoting_the_bank_reference_arrives_pre_linked(dataset) -> None:
    """Advice travels on a different channel from the money. Unless it quotes
    the bank reference, pairing it with a payment is work the system has to
    do -- so it must arrive unlinked."""
    session, _, _ = dataset

    for advice in session.scalars(select(Remittance)).all():
        if advice.link_source is RemittanceLinkSource.EXPLICIT:
            assert advice.bank_transaction_id is not None
        else:
            assert advice.link_source is RemittanceLinkSource.UNLINKED
            assert advice.bank_transaction_id is None


def test_some_advice_is_unlinked_and_some_is_explicit(dataset) -> None:
    session, _, _ = dataset
    sources = {advice.link_source for advice in session.scalars(select(Remittance)).all()}
    assert sources == {RemittanceLinkSource.EXPLICIT, RemittanceLinkSource.UNLINKED}


def test_advice_text_mentions_the_invoices_it_settles(dataset) -> None:
    """Phase 4 has to extract invoice numbers from this text, so the numbers
    had better be in there in some recognisable form."""
    session, _, truth = dataset
    by_ref = truth.by_statement_ref()
    linked = session.scalars(
        select(Remittance).where(Remittance.bank_transaction_id.isnot(None))
    ).all()
    assert linked

    for advice in linked[:40]:
        payment = by_ref[advice.bank_transaction.statement_ref]
        digits = {
            allocation.invoice_number.split("-")[-1].lstrip("0")
            for allocation in payment.allocations
        }
        assert all(digit in advice.raw_text for digit in digits)


# --- name variation machinery ------------------------------------------------


def test_clean_variants_always_normalise_to_the_registered_name() -> None:
    """A 'clean' variant that was secretly hard would corrupt the scenario
    labels, so every branch of the generator is exercised here."""
    rng = Random(1)
    config = small_config()
    for party in build_customers(rng, 40, config):
        for _ in range(12):
            variant = clean_payer_variant(rng, party)
            assert normalize_party_name(variant) == party.normalized_name, variant


def test_hard_variants_mostly_defeat_normalisation() -> None:
    rng = Random(2)
    config = small_config()
    parties = build_customers(rng, 40, config)

    defeated = sum(
        normalize_party_name(hard_payer_variant(rng, party)) != party.normalized_name
        for party in parties
        for _ in range(8)
    )
    assert defeated / (len(parties) * 8) > 0.7


def test_generated_customers_have_distinct_normalised_names() -> None:
    """Two customers normalising to the same key would make correct matching
    impossible, and the accuracy numbers meaningless."""
    config = small_config()
    parties = build_customers(Random(3), 50, config)
    keys = [party.normalized_name for party in parties]

    assert len(keys) == len(set(keys))
    assert all(key for key in keys)


def test_unknown_payers_never_collide_with_a_real_customer() -> None:
    config = small_config()
    parties = build_customers(Random(4), 30, config)
    known = {party.normalized_name for party in parties}

    rng = Random(5)
    for _ in range(200):
        assert normalize_party_name(build_unknown_payer(rng, known)) not in known


# --- reset safety ------------------------------------------------------------


def test_populated_database_is_detected_and_can_be_reset(tmp_path: Path) -> None:
    session = _fresh_session()
    assert database_is_populated(session) is False

    generate_dataset(session, small_config(), tmp_path)
    session.flush()
    assert database_is_populated(session) is True

    reset_database(session)
    assert database_is_populated(session) is False
    # The audit trail deliberately survives the wipe.
    assert session.scalar(select(func.count()).select_from(AuditLog)) > 0
    session.close()
