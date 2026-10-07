"""Orchestrates a full synthetic dataset: parties, invoices, payments, advice.

Everything downstream of ``Random(config.seed)`` is deterministic, so the same
config file always produces byte-identical data. That matters because an
accuracy number is only comparable to last week's if the dataset underneath it
has not moved.

The database receives the *pre-matching* state: every invoice fully open,
every payment unmatched. Working out which settles which is the job the rest
of the project exists to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from random import Random

from sqlalchemy import select
from sqlalchemy.orm import Session

from cashmatch import ENGINE_VERSION
from cashmatch.generator.config import GeneratorConfig
from cashmatch.generator.ground_truth import (
    GROUND_TRUTH_FILENAME,
    GroundTruth,
    TruthPayment,
    build_ground_truth,
    config_digest,
)
from cashmatch.generator.ledger import build_invoices
from cashmatch.generator.parties import build_customers
from cashmatch.generator.remittance import REMITTANCE_DIRNAME, build_remittances
from cashmatch.generator.scenarios import PaymentFactory
from cashmatch.models import (
    AuditLog,
    BankTransaction,
    Customer,
    CustomerAlias,
    Invoice,
    MatchResult,
    Remittance,
)
from cashmatch.models.enums import (
    AliasSource,
    InvoiceStatus,
    RemittanceLinkSource,
    TransactionStatus,
)
from cashmatch.normalize import normalize_narration, normalize_party_name

# Deleted in this order so no foreign key is ever left dangling.
_RESET_ORDER = (MatchResult, Remittance, BankTransaction, Invoice, CustomerAlias, Customer)


@dataclass(slots=True)
class GenerationSummary:
    """What a generation run produced, for the CLI to report."""

    customers: int
    aliases: int
    invoices: int
    payments: int
    remittances: int
    scenario_counts: dict[str, int]
    ground_truth_path: Path
    remittance_dir: Path
    config_digest: str
    seed: int


def database_is_populated(session: Session) -> bool:
    """True when any domain table already holds rows."""
    for model in _RESET_ORDER:
        if session.execute(select(model.id).limit(1)).first() is not None:
            return True
    return False


def reset_database(session: Session) -> None:
    """Delete every domain row, children first.

    The audit log is kept deliberately: a record that a dataset was wiped is
    exactly the kind of thing an audit trail exists to preserve.
    """
    for model in _RESET_ORDER:
        session.query(model).delete(synchronize_session=False)
    session.flush()


def generate_dataset(
    session: Session, config: GeneratorConfig, output_root: Path
) -> GenerationSummary:
    """Build a complete dataset and write the answer key beside it.

    Args:
        session: an open session; the caller owns the transaction.
        config: validated contents of ``config/scenarios.yml``.
        output_root: the ``data/`` directory. Generated documents go to
            ``generated/``, the answer key to ``ground_truth/``.
    """
    rng = Random(config.seed)

    parties = build_customers(rng, config.volume.customers, config)
    invoice_specs = build_invoices(rng, parties, config)
    payment_specs = PaymentFactory(rng, config, parties, invoice_specs).build_all()

    generated_dir = output_root / "generated"
    remittance_specs = build_remittances(
        rng,
        config,
        payment_specs,
        {party.code: party.legal_name for party in parties},
        generated_dir,
    )

    # --- persist ----------------------------------------------------------
    customers_by_code: dict[str, Customer] = {}
    alias_count = 0
    for party in parties:
        customer = Customer(
            code=party.code,
            legal_name=party.legal_name,
            normalized_name=party.normalized_name,
            city=party.city,
            payment_terms_days=party.payment_terms_days,
        )
        for alias in party.registered_aliases:
            customer.aliases.append(
                CustomerAlias(
                    alias=alias,
                    normalized_alias=normalize_party_name(alias),
                    source=AliasSource.ERP,
                )
            )
            alias_count += 1
        customers_by_code[party.code] = customer
    session.add_all(customers_by_code.values())
    session.flush()

    session.add_all(
        Invoice(
            invoice_number=spec.invoice_number,
            normalized_number=spec.normalized_number,
            customer_id=customers_by_code[spec.customer_code].id,
            invoice_date=spec.invoice_date,
            due_date=spec.due_date,
            # The book is handed over un-reconciled: every invoice fully open.
            amount_paise=spec.amount_paise,
            open_amount_paise=spec.amount_paise,
            status=InvoiceStatus.OPEN,
            po_number=spec.po_number,
        )
        for spec in invoice_specs
    )

    transactions = {
        spec.statement_ref: BankTransaction(
            statement_ref=spec.statement_ref,
            value_date=spec.value_date,
            amount_paise=spec.amount_paise,
            payer_name_raw=spec.payer_name_raw,
            payer_name_normalized=normalize_party_name(spec.payer_name_raw),
            narration=spec.narration,
            normalized_narration=normalize_narration(spec.narration),
            bank_account=spec.bank_account,
            status=TransactionStatus.UNMATCHED,
        )
        for spec in payment_specs
    }
    session.add_all(transactions.values())
    session.flush()

    for spec in remittance_specs:
        linked = spec.carries_bank_reference and spec.statement_ref in transactions
        session.add(
            Remittance(
                source_type=spec.source_type,
                raw_text=spec.raw_text,
                source_filename=spec.source_filename,
                received_at=spec.received_at,
                # Only advice quoting the bank reference arrives pre-linked.
                # The rest is Phase 3's problem, which is the realistic case.
                bank_transaction_id=transactions[spec.statement_ref].id if linked else None,
                link_source=(
                    RemittanceLinkSource.EXPLICIT if linked else RemittanceLinkSource.UNLINKED
                ),
            )
        )

    # --- answer key -------------------------------------------------------
    truth_payments = [
        TruthPayment(
            statement_ref=spec.statement_ref,
            scenario=spec.scenario,
            customer_code=spec.customer_code,
            amount_paise=spec.amount_paise,
            value_date=spec.value_date.isoformat(),
            allocations=spec.allocations,
            remittance_refs=spec.remittance_refs,
        )
        for spec in payment_specs
    ]

    counts = {
        "customers": len(parties),
        "aliases": alias_count,
        "invoices": len(invoice_specs),
        "payments": len(payment_specs),
        "remittances": len(remittance_specs),
    }
    ground_truth = build_ground_truth(config, truth_payments, counts)
    ground_truth_dir = output_root / "ground_truth"
    ground_truth.write(ground_truth_dir)

    digest = config_digest(config)
    session.add(
        AuditLog(
            entity_type="dataset",
            entity_id=0,
            action="dataset_generated",
            actor=f"system:{ENGINE_VERSION}",
            payload_after={
                "seed": config.seed,
                "config_digest": digest,
                "counts": counts,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
    )
    session.flush()

    return GenerationSummary(
        customers=len(parties),
        aliases=alias_count,
        invoices=len(invoice_specs),
        payments=len(payment_specs),
        remittances=len(remittance_specs),
        scenario_counts=ground_truth.scenario_counts,
        ground_truth_path=ground_truth_dir / GROUND_TRUTH_FILENAME,
        remittance_dir=generated_dir / REMITTANCE_DIRNAME,
        config_digest=digest,
        seed=config.seed,
    )


def load_ground_truth(output_root: Path) -> GroundTruth:
    """Read the answer key written by the last generation run."""
    return GroundTruth.read(output_root / "ground_truth" / GROUND_TRUTH_FILENAME)
