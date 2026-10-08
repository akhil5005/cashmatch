"""Typed, validated view of ``config/matching.yml``.

Separate from the generator's config on purpose: ``scenarios.yml`` describes
the problem, ``matching.yml`` describes the attempt at solving it. Tuning one
while holding the other fixed is the only way to attribute a change in
results to a cause.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Scorers from rapidfuzz.fuzz that make sense for short business names.
_ALLOWED_SCORERS = ("WRatio", "ratio", "partial_ratio", "token_sort_ratio", "token_set_ratio")


class _Strict(BaseModel):
    """Rejects unknown keys, so a typo is an error rather than a silent no-op."""

    model_config = ConfigDict(extra="forbid")


class ScoringWeights(_Strict):
    """Relative importance of each signal. Must sum to 1.0."""

    reference_match: float = Field(ge=0, le=1)
    amount_match: float = Field(ge=0, le=1)
    customer_identity: float = Field(ge=0, le=1)
    remittance_agreement: float = Field(ge=0, le=1)
    date_proximity: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        total = sum(self.model_dump().values())
        # Tolerate float representation error, not a genuine mistake.
        if abs(total - 1.0) > 1e-6:
            parts = ", ".join(f"{k}={v}" for k, v in sorted(self.model_dump().items()))
            raise ValueError(
                f"scoring.weights must sum to 1.0 but sum to {total:.6f}. Got: {parts}. "
                "A confidence built from weights that do not sum to one is not "
                "comparable between runs, and the thresholds stop meaning anything."
            )
        return self

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class ScoringThresholds(_Strict):
    auto_apply: float = Field(gt=0, le=1)
    review_floor: float = Field(ge=0, lt=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.auto_apply <= self.review_floor:
            raise ValueError(
                f"scoring.thresholds.auto_apply ({self.auto_apply}) must exceed "
                f"review_floor ({self.review_floor}); otherwise there is no band "
                "left for human review and every match is either posted or "
                "discarded."
            )
        return self


class ScoringPenalties(_Strict):
    """Multipliers applied after the weighted sum. Each is in (0, 1]."""

    ambiguous: float = Field(gt=0, le=1)
    unreconciled_advice: float = Field(gt=0, le=1)
    residual_cash: float = Field(gt=0, le=1)
    guessed_claim_bearer: float = Field(gt=0, le=1)


class DateProximityConfig(_Strict):
    ideal_days: int = Field(ge=0)
    zero_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.zero_days <= self.ideal_days:
            raise ValueError(
                f"scoring.date_proximity.zero_days ({self.zero_days}) must exceed "
                f"ideal_days ({self.ideal_days})."
            )
        return self


class ScoringConfig(_Strict):
    weights: ScoringWeights
    thresholds: ScoringThresholds
    penalties: ScoringPenalties
    date_proximity: DateProximityConfig


class CustomerIdentificationConfig(_Strict):
    fuzzy_min_score: float = Field(ge=0, le=100)
    fuzzy_margin: float = Field(ge=0, le=100)
    fuzzy_scorer: str = "WRatio"

    @model_validator(mode="after")
    def _known_scorer(self) -> Self:
        if self.fuzzy_scorer not in _ALLOWED_SCORERS:
            raise ValueError(
                f"customer_identification.fuzzy_scorer {self.fuzzy_scorer!r} is not "
                f"supported. Choose one of: {', '.join(_ALLOWED_SCORERS)}."
            )
        return self


class RemittanceConfig(_Strict):
    """How extracted advice feeds the cascade."""

    enabled: bool = True
    require_reconciled: bool = False


class ReferenceConfig(_Strict):
    enabled: bool = True
    min_digits: int = Field(ge=1)
    allow_bare_numeric: bool = True
    max_digits: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_digits < self.min_digits:
            raise ValueError(
                f"reference.max_digits ({self.max_digits}) is below "
                f"reference.min_digits ({self.min_digits})."
            )
        return self


class AmountConfig(_Strict):
    tolerance_paise: int = Field(ge=0)


class DateWindowConfig(_Strict):
    days_before_payment: int = Field(ge=0)
    days_after_payment: int = Field(ge=0)


class SubsetSumConfig(_Strict):
    enabled: bool = True
    max_combination_size: int = Field(ge=1, le=12)
    max_candidate_invoices: int = Field(ge=1)
    max_solutions: int = Field(ge=1)
    combination_budget: int = Field(ge=1)


class ShortPayConfig(_Strict):
    enabled: bool = True
    max_combination_size: int = Field(ge=1, le=12)
    max_solutions: int = Field(ge=1)
    combination_budget: int = Field(ge=1)
    min_deduction_pct: float = Field(gt=0, lt=100)
    max_deduction_pct: float = Field(gt=0, lt=100)
    max_deduction_paise: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_deduction_pct <= self.min_deduction_pct:
            raise ValueError(
                f"short_pay.max_deduction_pct ({self.max_deduction_pct}) must exceed "
                f"short_pay.min_deduction_pct ({self.min_deduction_pct})."
            )
        return self


class MatchingConfig(_Strict):
    """The whole of ``matching.yml``, validated."""

    scoring: ScoringConfig
    customer_identification: CustomerIdentificationConfig
    remittance: RemittanceConfig
    reference: ReferenceConfig
    amount: AmountConfig
    date_window: DateWindowConfig
    subset_sum: SubsetSumConfig
    short_pay: ShortPayConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> MatchingConfig:
        """Load and validate a config file, with errors that name the file."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Matching config not found at {path}. Expected the YAML file that "
                "tunes the matching engine; the project ships one at "
                "backend/config/matching.yml."
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
