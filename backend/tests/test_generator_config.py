"""The scenario YAML is the knob a user turns, so its validation has to be
strict and its error messages have to name the offending key.

A silently-wrong percentage mix would not crash anything -- it would just
quietly invalidate every accuracy number measured against the dataset.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from cashmatch.generator.config import GeneratorConfig, ScenarioLabel

SHIPPED_CONFIG = Path(__file__).resolve().parents[1] / "config" / "scenarios.yml"


@pytest.fixture(scope="module")
def shipped_raw() -> dict:
    return yaml.safe_load(SHIPPED_CONFIG.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "scenarios.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_shipped_config_is_valid() -> None:
    config = GeneratorConfig.from_yaml(SHIPPED_CONFIG)
    assert config.seed == 42
    assert config.volume.customers == 50
    assert config.volume.invoices == 2000
    assert config.volume.payments == 800


def test_scenario_keys_cover_every_label(shipped_raw: dict) -> None:
    """The YAML and the ScenarioLabel enum must stay in lockstep, or a label
    would exist that the generator never produces."""
    assert set(shipped_raw["scenarios"]) == {label.value for label in ScenarioLabel}


def test_scenario_mix_must_sum_to_100(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["scenarios"]["exact_single"] = 40  # now sums to 106

    with pytest.raises(ValueError) as exc:
        GeneratorConfig.from_yaml(_write(tmp_path, raw))

    message = str(exc.value)
    assert "scenarios must sum to exactly 100" in message
    assert "106" in message


def test_deduction_reasons_must_sum_to_100(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["deduction"]["reasons"]["damage"] = 5

    with pytest.raises(ValueError, match="deduction.reasons must sum to exactly 100"):
        GeneratorConfig.from_yaml(_write(tmp_path, raw))


def test_unknown_deduction_reason_is_rejected(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["deduction"]["reasons"] = {"damage": 50, "gremlins": 50}

    with pytest.raises(ValueError) as exc:
        GeneratorConfig.from_yaml(_write(tmp_path, raw))

    assert "gremlins" in str(exc.value)
    assert "Valid codes are" in str(exc.value)


def test_a_typo_in_a_key_is_an_error_not_a_no_op(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["volume"]["custommers"] = 10

    with pytest.raises(ValueError, match="custommers"):
        GeneratorConfig.from_yaml(_write(tmp_path, raw))


def test_more_payments_than_invoices_is_rejected(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["volume"]["invoices"] = 100
    raw["volume"]["payments"] = 500

    with pytest.raises(ValueError, match="below volume.payments"):
        GeneratorConfig.from_yaml(_write(tmp_path, raw))


def test_inverted_period_is_rejected(tmp_path: Path, shipped_raw: dict) -> None:
    raw = copy.deepcopy(shipped_raw)
    raw["period"]["invoice_end"] = raw["period"]["invoice_start"]

    with pytest.raises(ValueError, match="must be after"):
        GeneratorConfig.from_yaml(_write(tmp_path, raw))


def test_missing_file_says_where_it_looked(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="backend/config/scenarios.yml"):
        GeneratorConfig.from_yaml(tmp_path / "nope.yml")


def test_malformed_yaml_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "scenarios.yml"
    path.write_text("seed: [unclosed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid YAML"):
        GeneratorConfig.from_yaml(path)
