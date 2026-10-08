"""Application settings, read once from the environment / .env file."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMMode(StrEnum):
    """How Phase 4's remittance extractor should behave.

    The LLM is deliberately optional. Tests and CI run in ``MOCK``, so the
    whole project is runnable with no API key and no network access.
    """

    MOCK = "mock"
    LIVE = "live"
    OFF = "off"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://cashmatch:cashmatch@localhost:5432/cashmatch",
        description="SQLAlchemy URL for the application database.",
    )

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Accept the URL shape managed platforms actually hand out.

        Render, Heroku, Railway and Fly all emit ``postgres://``. SQLAlchemy
        2.x dropped that alias, and this project pins psycopg 3, so the URL
        has to carry an explicit driver. Normalising here rather than asking
        every deployment to hand-edit an environment variable is what makes
        the same image run anywhere.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    random_seed: int = Field(
        default=42,
        description="Seeds every random choice in the data generator so runs are identical.",
    )

    data_dir: Path = Field(
        default=Path(__file__).resolve().parents[2] / "data",
        description="Root for generated data and the ground-truth answer key.",
    )

    scenario_config: Path = Field(
        default=Path(__file__).resolve().parents[1] / "config" / "scenarios.yml",
        description="YAML file describing the synthetic data volume and scenario mix.",
    )

    matching_config: Path = Field(
        default=Path(__file__).resolve().parents[1] / "config" / "matching.yml",
        description="YAML file tuning the matching engine: prunes, tolerances, thresholds.",
    )

    llm_mode: LLMMode = LLMMode.MOCK
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once."""
    return Settings()
