"""Drift job configuration, loaded from one TOML file (``configs/drift.toml``).

Every value is required and unknown keys are rejected, so a typo or a missing threshold fails
before any data is read instead of silently falling back to a default.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.models.compare import IMBALANCE_METHODS

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "drift.toml"

Probability = Field(gt=0.0, lt=1.0)


class ConfigError(ValueError):
    """The drift configuration file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LabelsConfig(_Strict):
    delay_days: int = Field(ge=1)


class ReferenceConfig(_Strict):
    days: int = Field(ge=1)


class CurrentConfig(_Strict):
    default_days: int = Field(ge=1)


class FeatureDriftConfig(_Strict):
    num_method: Literal["wasserstein", "psi", "jensenshannon"]
    cat_method: Literal["jensenshannon", "psi"]
    column_threshold: float = Probability
    share_threshold: float = Probability
    top_n: int = Field(ge=1)


class PredictionDriftConfig(_Strict):
    method: Literal["psi", "wasserstein", "jensenshannon"]
    threshold: float = Field(gt=0.0)


class PerformanceConfig(_Strict):
    window_days: int = Field(ge=1)
    max_drop: float = Probability
    min_positives: int = Field(ge=1)


class RetrainConfig(_Strict):
    holdout_days: int = Field(ge=1)
    seed: int
    imbalance: str

    @field_validator("imbalance")
    @classmethod
    def _known_imbalance(cls, value: str) -> str:
        if value not in IMBALANCE_METHODS:
            raise ValueError(f"must be one of {IMBALANCE_METHODS}, got {value!r}")
        return value


class PromotionConfig(_Strict):
    margin: float = Field(ge=0.0, lt=1.0)
    resamples: int = Field(ge=100)
    seed: int
    unit: Literal["day", "transaction"]


class DriftConfig(_Strict):
    labels: LabelsConfig
    reference: ReferenceConfig
    current: CurrentConfig
    feature_drift: FeatureDriftConfig
    prediction_drift: PredictionDriftConfig
    performance: PerformanceConfig
    retrain: RetrainConfig
    promotion: PromotionConfig


def parse_config(data: dict[str, Any]) -> DriftConfig:
    try:
        return DriftConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid drift config:\n{exc}") from exc


def load_config(path: Path | str = DEFAULT_CONFIG) -> DriftConfig:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"drift config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"drift config {path} is not valid TOML: {exc}") from exc
    return parse_config(data)
