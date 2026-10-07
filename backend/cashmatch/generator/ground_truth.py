"""The answer key: what each generated payment is *actually* settling.

This file is written to disk and never loaded into the database the matcher
queries. The separation is physical, not a convention: if the correct answer
lived in a column, the matcher could read it and every accuracy number in
Phase 6 would be meaningless. A test in ``tests/test_models_constraints.py``
asserts that no scenario or truth column exists on ``bank_transactions``.

Phase 6 is the only component that loads this file, and it does so after
matching has finished.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cashmatch.generator.config import GeneratorConfig, ScenarioLabel
from cashmatch.models.enums import DeductionReason

GROUND_TRUTH_FILENAME = "ground_truth.json"


class TruthAllocation(BaseModel):
    """One invoice the payment settles, and by how much."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    allocated_amount_paise: int = Field(ge=0)
    deduction_amount_paise: int = Field(default=0, ge=0)
    deduction_reason: DeductionReason | None = None

    @model_validator(mode="after")
    def _reason_accompanies_deduction(self) -> Self:
        if self.deduction_amount_paise > 0 and self.deduction_reason is None:
            raise ValueError(
                f"{self.invoice_number}: a deduction of "
                f"{self.deduction_amount_paise} paise was recorded with no reason "
                "code. Every short-pay in the answer key must say why."
            )
        return self


class TruthPayment(BaseModel):
    """The correct outcome for one bank transaction."""

    model_config = ConfigDict(extra="forbid")

    statement_ref: str
    scenario: ScenarioLabel
    customer_code: str | None
    amount_paise: int = Field(gt=0)
    value_date: str
    allocations: list[TruthAllocation]
    remittance_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _allocations_account_for_the_money(self) -> Self:
        """Either the payment is fully allocated, or it belongs nowhere.

        There is no middle ground in the answer key: a half-explained payment
        would make "was the matcher right?" unanswerable.
        """
        if not self.allocations:
            if self.scenario is not ScenarioLabel.NO_MATCHING_INVOICE:
                raise ValueError(
                    f"{self.statement_ref}: scenario {self.scenario} has no "
                    "allocations. Only no_matching_invoice may be unallocated."
                )
            return self

        applied = sum(a.allocated_amount_paise for a in self.allocations)
        if applied != self.amount_paise:
            raise ValueError(
                f"{self.statement_ref}: allocations total {applied} paise but the "
                f"payment was {self.amount_paise} paise. The answer key must "
                "account for every paise received."
            )
        return self

    @property
    def deduction_total_paise(self) -> int:
        return sum(a.deduction_amount_paise for a in self.allocations)


class GroundTruth(BaseModel):
    """The complete answer key for one generated dataset."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    seed: int
    config_digest: str
    counts: dict[str, int]
    scenario_counts: dict[str, int]
    payments: list[TruthPayment]

    def by_statement_ref(self) -> dict[str, TruthPayment]:
        """Index for Phase 6, which looks truth up per matched transaction."""
        return {payment.statement_ref: payment for payment in self.payments}

    def write(self, directory: str | Path) -> Path:
        """Write the answer key as indented JSON, readable and diffable."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / GROUND_TRUTH_FILENAME
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def read(cls, path: str | Path) -> GroundTruth:
        """Load and validate an answer key written by a previous run."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"No ground-truth file at {path}. Generate a dataset first with "
                "`cashmatch generate`, which writes it alongside the data."
            )
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def config_digest(config: GeneratorConfig) -> str:
    """A short fingerprint of the config that produced a dataset.

    Stored in the answer key so an accuracy result can be traced back to the
    exact scenario mix it was measured against. Comparing two runs with
    different digests is comparing two different problems.
    """
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_ground_truth(
    config: GeneratorConfig, payments: list[TruthPayment], counts: dict[str, int]
) -> GroundTruth:
    scenario_counts: dict[str, int] = {label.value: 0 for label in ScenarioLabel}
    for payment in payments:
        scenario_counts[payment.scenario.value] += 1

    return GroundTruth(
        generated_at=datetime.now(UTC),
        seed=config.seed,
        config_digest=config_digest(config),
        counts=counts,
        scenario_counts=scenario_counts,
        payments=payments,
    )
