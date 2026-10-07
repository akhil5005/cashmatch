"""initial schema

Creates the eight CashMatch tables.

Enum columns are declared with ``native_enum=False`` so they compile to
VARCHAR on both PostgreSQL and SQLite. The permitted values are spelled out
literally here rather than imported from ``cashmatch.models.enums``: a
migration must describe the schema as it was at this revision, and importing
live application code would let a future enum change rewrite history.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cashmatch.db.types import MoneyColumn, PortableJSON

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    # --- customers ---------------------------------------------------------
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("code", name="uq_customers_code"),
    )
    op.create_index("ix_customers_normalized_name", "customers", ["normalized_name"])

    # --- customer_aliases --------------------------------------------------
    op.create_table(
        "customer_aliases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column("normalized_alias", sa.String(length=255), nullable=False),
        sa.Column("source", _enum("erp", "learned", "manual", name="aliassource"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_customer_aliases_customer_id_customers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customer_aliases"),
        sa.UniqueConstraint("customer_id", "normalized_alias", name="uq_alias_per_customer"),
    )
    op.create_index("ix_customer_aliases_customer_id", "customer_aliases", ["customer_id"])
    op.create_index(
        "ix_customer_aliases_normalized_alias", "customer_aliases", ["normalized_alias"]
    )

    # --- invoices ----------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.String(length=64), nullable=False),
        sa.Column("normalized_number", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_paise", MoneyColumn(), nullable=False),
        sa.Column("open_amount_paise", MoneyColumn(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            _enum("open", "partially_paid", "paid", "written_off", name="invoicestatus"),
            nullable=False,
        ),
        sa.Column("po_number", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_paise > 0", name="ck_invoices_amount_positive"),
        sa.CheckConstraint("open_amount_paise >= 0", name="ck_invoices_open_amount_non_negative"),
        sa.CheckConstraint(
            "open_amount_paise <= amount_paise", name="ck_invoices_open_amount_within_amount"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_invoices_customer_id_customers",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint("invoice_number", name="uq_invoices_invoice_number"),
    )
    op.create_index("ix_invoices_normalized_number", "invoices", ["normalized_number"])
    op.create_index("ix_invoices_customer_id", "invoices", ["customer_id"])
    op.create_index("ix_invoices_due_date", "invoices", ["due_date"])
    op.create_index("ix_invoices_po_number", "invoices", ["po_number"])
    # The matcher's hottest lookup: open items belonging to one customer.
    op.create_index("ix_invoices_customer_status", "invoices", ["customer_id", "status"])

    # --- bank_transactions -------------------------------------------------
    op.create_table(
        "bank_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("statement_ref", sa.String(length=64), nullable=False),
        sa.Column("value_date", sa.Date(), nullable=False),
        sa.Column("amount_paise", MoneyColumn(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payer_name_raw", sa.String(length=255), nullable=False),
        sa.Column("payer_name_normalized", sa.String(length=255), nullable=False),
        sa.Column("narration", sa.Text(), nullable=False),
        sa.Column("normalized_narration", sa.Text(), nullable=False),
        sa.Column("bank_account", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            _enum(
                "unmatched",
                "matched",
                "partially_applied",
                "ignored",
                name="transactionstatus",
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("amount_paise > 0", name="ck_bank_transactions_amount_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_bank_transactions"),
        sa.UniqueConstraint("statement_ref", name="uq_bank_transactions_statement_ref"),
    )
    op.create_index("ix_bank_transactions_value_date", "bank_transactions", ["value_date"])
    op.create_index(
        "ix_bank_transactions_payer_name_normalized",
        "bank_transactions",
        ["payer_name_normalized"],
    )
    op.create_index(
        "ix_bank_transactions_status_date", "bank_transactions", ["status", "value_date"]
    )

    # --- remittances -------------------------------------------------------
    op.create_table(
        "remittances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "source_type",
            _enum("email", "pdf", "csv", "manual", name="remittancesource"),
            nullable=False,
        ),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        # Nullable by design: advice and money arrive on separate channels and
        # pairing them is part of the matching work, not a precondition.
        sa.Column("bank_transaction_id", sa.Integer(), nullable=True),
        sa.Column(
            "link_source",
            _enum("explicit", "inferred", "unlinked", name="remittancelinksource"),
            nullable=False,
        ),
        sa.Column(
            "extraction_status",
            _enum("pending", "extracted", "failed", name="extractionstatus"),
            nullable=False,
        ),
        sa.Column(
            "extraction_method",
            _enum("none", "llm", "regex_fallback", "mock", name="extractionmethod"),
            nullable=False,
        ),
        sa.Column("extracted_payload", PortableJSON, nullable=True),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["bank_transaction_id"],
            ["bank_transactions.id"],
            name="fk_remittances_bank_transaction_id_bank_transactions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remittances"),
    )
    op.create_index("ix_remittances_bank_transaction_id", "remittances", ["bank_transaction_id"])

    # --- match_results -----------------------------------------------------
    op.create_table(
        "match_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bank_transaction_id", sa.Integer(), nullable=False),
        sa.Column(
            "decision",
            _enum(
                "auto_applied",
                "needs_review",
                "unapplied",
                "rejected",
                "manually_applied",
                name="matchdecision",
            ),
            nullable=False,
        ),
        # A ratio, not money: fixed-point Decimal is correct here.
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column(
            "strategy",
            _enum(
                "none",
                "reference_exact",
                "amount_exact",
                "subset_sum",
                "short_pay",
                "remittance_guided",
                "manual",
                name="matchstrategy",
            ),
            nullable=False,
        ),
        sa.Column("matched_amount_paise", MoneyColumn(), nullable=False),
        sa.Column("unapplied_amount_paise", MoneyColumn(), nullable=False),
        sa.Column("explanation", PortableJSON, nullable=False),
        sa.Column("reason_text", sa.String(length=500), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "review_action",
            _enum("approved", "rejected", "reassigned", name="reviewaction"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_match_results_confidence_in_range"
        ),
        sa.CheckConstraint(
            "matched_amount_paise >= 0", name="ck_match_results_matched_amount_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["bank_transaction_id"],
            ["bank_transactions.id"],
            name="fk_match_results_bank_transaction_id_bank_transactions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_results"),
    )
    op.create_index(
        "ix_match_results_bank_transaction_id", "match_results", ["bank_transaction_id"]
    )
    op.create_index(
        "ix_match_results_decision_created", "match_results", ["decision", "created_at"]
    )

    # --- match_allocations -------------------------------------------------
    op.create_table(
        "match_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_result_id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("allocated_amount_paise", MoneyColumn(), nullable=False),
        sa.Column("deduction_amount_paise", MoneyColumn(), nullable=False),
        sa.Column(
            "deduction_reason",
            _enum(
                "damage",
                "promo",
                "pricing",
                "freight",
                "tds",
                "short_ship",
                "unknown",
                name="deductionreason",
            ),
            nullable=True,
        ),
        sa.Column("deduction_note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allocated_amount_paise >= 0", name="ck_match_allocations_allocated_non_negative"
        ),
        sa.CheckConstraint(
            "deduction_amount_paise >= 0", name="ck_match_allocations_deduction_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name="fk_match_allocations_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["match_result_id"],
            ["match_results.id"],
            name="fk_match_allocations_match_result_id_match_results",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_allocations"),
        sa.UniqueConstraint("match_result_id", "invoice_id", name="uq_allocation_per_invoice"),
    )
    op.create_index(
        "ix_match_allocations_match_result_id", "match_allocations", ["match_result_id"]
    )
    op.create_index("ix_match_allocations_invoice_id", "match_allocations", ["invoice_id"])

    # --- audit_log ---------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("payload_before", PortableJSON, nullable=True),
        sa.Column("payload_after", PortableJSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    # Reverse creation order so foreign keys never block a drop.
    op.drop_table("audit_log")
    op.drop_table("match_allocations")
    op.drop_table("match_results")
    op.drop_table("remittances")
    op.drop_table("bank_transactions")
    op.drop_table("invoices")
    op.drop_table("customer_aliases")
    op.drop_table("customers")
