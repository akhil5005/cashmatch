"""Typed, validated view of ``config/scenarios.yml``.

The scenario mix lives in YAML rather than in code so that the dataset can be
reshaped without touching Python: raise ``missing_reference`` to 40, re-run the
generator, and watch what it does to the auto-match rate. That experiment is
the whole reason the file exists.

Validation is strict and the messages name the offending key, because a
silently-wrong percentage mix would quietly invalidate every accuracy number
measured against the data.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScenarioLabel(StrEnum):
    """The scenario a generated payment belongs to.

    Labels are mutually exclusive and are what Phase 6 reports accuracy
    against, so a weak spot in one scenario shows up instead of being averaged
    away by the others.
    """

    EXACT_SINGLE = "exact_single"
    BUNDLED_MULTI = "bundled_multi"
    SHORT_PAY_DEDUCTION = "short_pay_deduction"
    PARTIAL_PAYMENT = "partial_payment"
    MISSING_REFERENCE = "missing_reference"
    TYPO_REFERENCE = "typo_reference"
    PAYER_NAME_VARIANT = "payer_name_variant"
    NO_MATCHING_INVOICE = "no_matching_invoice"


class _Strict(BaseModel):
    """Base that rejects unknown keys, so a typo in the YAML is an error
    rather than a setting that silently does nothing."""

    model_config = ConfigDict(extra="forbid")


def _require_pct_total(values: dict[str, int], label: str) -> None:
    total = sum(values.values())
    if total != 100:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
        raise ValueError(
            f"{label} must sum to exactly 100 but sums to {total}. Got: {parts}. "
            "Adjust the percentages in config/scenarios.yml so they add up."
        )


class VolumeConfig(_Strict):
    customers: int = Field(gt=0)
    invoices: int = Field(gt=0)
    payments: int = Field(gt=0)

    @model_validator(mode="after")
    def _enough_invoices_to_go_around(self) -> Self:
        if self.invoices < self.payments:
            raise ValueError(
                f"volume.invoices ({self.invoices}) is below volume.payments "
                f"({self.payments}). Bundled payments consume several invoices each, "
                "so there would not be enough open items to pay."
            )
        return self


class PeriodConfig(_Strict):
    invoice_start: date
    invoice_end: date
    payment_offset_days: tuple[int, int]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.invoice_end <= self.invoice_start:
            raise ValueError(
                f"period.invoice_end ({self.invoice_end}) must be after "
                f"period.invoice_start ({self.invoice_start})."
            )
        low, high = self.payment_offset_days
        if high <= low:
            raise ValueError(
                f"period.payment_offset_days must be [min, max] with max greater "
                f"than min; got [{low}, {high}]."
            )
        return self


class ScenarioMix(_Strict):
    """Percentage weight of each payment scenario. Must sum to 100."""

    exact_single: int = Field(ge=0, le=100)
    bundled_multi: int = Field(ge=0, le=100)
    short_pay_deduction: int = Field(ge=0, le=100)
    partial_payment: int = Field(ge=0, le=100)
    missing_reference: int = Field(ge=0, le=100)
    typo_reference: int = Field(ge=0, le=100)
    payer_name_variant: int = Field(ge=0, le=100)
    no_matching_invoice: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _sums_to_hundred(self) -> Self:
        _require_pct_total(self.model_dump(), "scenarios")
        return self

    @model_validator(mode="after")
    def _covers_every_label(self) -> Self:
        """The YAML keys and ScenarioLabel must stay in lockstep; a label with
        no weight would silently never be generated."""
        declared = set(self.model_dump())
        known = {label.value for label in ScenarioLabel}
        if declared != known:
            raise ValueError(
                "scenarios keys do not match the known scenario labels. "
                f"Missing: {sorted(known - declared)}; "
                f"unexpected: {sorted(declared - known)}."
            )
        return self

    def as_weights(self) -> dict[str, int]:
        """Scenario name to weight, in declaration order (deterministic)."""
        return self.model_dump()


class BundleConfig(_Strict):
    min_invoices: int = Field(ge=2)
    max_invoices: int = Field(ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_invoices < self.min_invoices:
            raise ValueError(
                f"bundle.max_invoices ({self.max_invoices}) is below "
                f"bundle.min_invoices ({self.min_invoices})."
            )
        return self


class ShareRange(_Strict):
    min_share_pct: int = Field(gt=0, lt=100)
    max_share_pct: int = Field(gt=0, lt=100)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_share_pct < self.min_share_pct:
            raise ValueError(
                f"max_share_pct ({self.max_share_pct}) is below min_share_pct "
                f"({self.min_share_pct})."
            )
        return self


class DeductionConfig(ShareRange):
    round_to_rupees: int = Field(gt=0)
    reasons: dict[str, int]

    @model_validator(mode="after")
    def _reasons_valid(self) -> Self:
        # Imported here to keep this module importable without the ORM.
        from cashmatch.models.enums import DeductionReason

        known = {reason.value for reason in DeductionReason}
        unknown = sorted(set(self.reasons) - known)
        if unknown:
            raise ValueError(
                f"deduction.reasons contains unknown reason code(s): {unknown}. "
                f"Valid codes are: {sorted(known)}."
            )
        _require_pct_total(self.reasons, "deduction.reasons")
        return self


class InvoiceConfig(_Strict):
    min_amount_inr: int = Field(gt=0)
    max_amount_inr: int = Field(gt=0)
    round_amount_share_pct: int = Field(ge=0, le=100)
    payment_terms_days: list[int]
    po_number_share_pct: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if self.max_amount_inr <= self.min_amount_inr:
            raise ValueError(
                f"invoice.max_amount_inr ({self.max_amount_inr}) must exceed "
                f"invoice.min_amount_inr ({self.min_amount_inr})."
            )
        if not self.payment_terms_days:
            raise ValueError("invoice.payment_terms_days must list at least one value.")
        if any(term <= 0 for term in self.payment_terms_days):
            raise ValueError("invoice.payment_terms_days values must all be positive.")
        return self


class CustomerConfig(_Strict):
    registered_alias_share_pct: int = Field(ge=0, le=100)
    max_registered_aliases: int = Field(ge=0)


class RemittanceConfig(_Strict):
    coverage_pct: int = Field(ge=0, le=100)
    pdf_share_pct: int = Field(ge=0, le=100)
    explicit_link_share_pct: int = Field(ge=0, le=100)


class GeneratorConfig(_Strict):
    """The whole of ``scenarios.yml``, validated."""

    seed: int
    volume: VolumeConfig
    period: PeriodConfig
    scenarios: ScenarioMix
    bundle: BundleConfig
    partial: ShareRange
    deduction: DeductionConfig
    invoice: InvoiceConfig
    customer: CustomerConfig
    remittance: RemittanceConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> GeneratorConfig:
        """Load and validate a config file, with errors that name the file."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Scenario config not found at {path}. Expected the YAML file that "
                "describes dataset volume and the scenario mix; the project ships "
                "one at backend/config/scenarios.yml."
            )

        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(raw, dict):
            raise ValueError(
                f"{path} should contain a mapping of settings at the top level, "
                f"but parsed as {type(raw).__name__}."
            )

        try:
            return cls.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"{path} failed validation.\n{exc}") from exc
