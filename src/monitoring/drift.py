"""Feature and prediction drift with Evidently (0.7 API: ``Report`` / ``Dataset``).

* Feature drift: ``DataDriftPreset`` over the model's input features, with the per-column
  methods and threshold pinned in the config (both are distances: drift iff score >= threshold).
* Prediction drift: ``ValueDrift`` on the score column.

Telemetry is switched off before Evidently is imported.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.monitoring.config import FeatureDriftConfig, PredictionDriftConfig

os.environ.setdefault("EVIDENTLY_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")

from evidently import DataDefinition, Dataset, Report  # noqa: E402
from evidently.core.report import Snapshot  # noqa: E402
from evidently.metrics import ValueDrift  # noqa: E402
from evidently.presets import DataDriftPreset  # noqa: E402

SCORE = "score"


@dataclass(frozen=True)
class ColumnDrift:
    column: str
    method: str
    score: float
    threshold: float

    @property
    def drifted(self) -> bool:
        return self.score >= self.threshold


@dataclass
class DriftResult:
    columns: list[ColumnDrift]
    drifted_count: int
    drifted_share: float
    prediction_method: str
    prediction_score: float
    prediction_threshold: float
    snapshot: Snapshot = field(repr=False)

    @property
    def prediction_drift_detected(self) -> bool:
        return self.prediction_score >= self.prediction_threshold

    def top_drifted(self, n: int) -> list[dict[str, Any]]:
        """Drifted features ordered by score / threshold (how far past the threshold)."""
        drifted = sorted(
            (c for c in self.columns if c.drifted), key=lambda c: -(c.score / c.threshold)
        )
        return [
            {"feature": c.column, "method": c.method, "score": round(c.score, 4)}
            for c in drifted[:n]
        ]


def run_drift(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    *,
    numerical: list[str],
    categorical: list[str],
    feature_cfg: FeatureDriftConfig,
    prediction_cfg: PredictionDriftConfig,
) -> DriftResult:
    """``reference`` / ``current`` hold the feature columns plus ``score``."""
    features = [*numerical, *categorical]
    definition = DataDefinition(
        numerical_columns=[*numerical, SCORE], categorical_columns=categorical
    )
    cols = [*features, SCORE]
    report = Report(
        [
            DataDriftPreset(
                columns=features,
                num_method=feature_cfg.num_method,
                cat_method=feature_cfg.cat_method,
                threshold=feature_cfg.column_threshold,
            ),
            ValueDrift(
                column=SCORE, method=prediction_cfg.method, threshold=prediction_cfg.threshold
            ),
        ]
    )
    snapshot = report.run(
        Dataset.from_pandas(_prepare(current[cols], categorical), data_definition=definition),
        Dataset.from_pandas(_prepare(reference[cols], categorical), data_definition=definition),
    )
    return _parse(snapshot, features)


def _prepare(frame: pd.DataFrame, categorical: list[str]) -> pd.DataFrame:
    """Categoricals as plain strings (missing stays missing), numerics as float."""
    out = frame.copy()
    for col in out.columns:
        if col in categorical:
            out[col] = out[col].astype("object").where(out[col].notna(), None)
        else:
            out[col] = pd.to_numeric(out[col], errors="raise").astype("float64")
    return out


def _parse(snapshot: Snapshot, features: list[str]) -> DriftResult:
    count: float | None = None
    columns: dict[str, ColumnDrift] = {}
    prediction: ColumnDrift | None = None
    for metric in snapshot.dict()["metrics"]:
        config, value = metric["config"], metric["value"]
        kind = str(config["type"]).rsplit(":", 1)[-1]
        if kind == "DriftedColumnsCount":
            count = float(value["count"])
        elif kind == "ValueDrift":
            drift = ColumnDrift(
                str(config["column"]),
                str(config["method"]),
                float(value),
                float(config["threshold"]),
            )
            if drift.column == SCORE:
                prediction = drift
            else:
                columns[drift.column] = drift
    if count is None or prediction is None or set(columns) != set(features):
        raise RuntimeError("Evidently report is missing expected metrics")
    ordered = [columns[f] for f in features]
    drifted = sum(c.drifted for c in ordered)
    if drifted != int(count):  # our per-column reading must agree with Evidently's own count
        raise RuntimeError(f"drifted features: parsed {drifted}, Evidently counted {count}")
    return DriftResult(
        columns=ordered,
        drifted_count=drifted,
        drifted_share=drifted / len(ordered),
        prediction_method=prediction.method,
        prediction_score=prediction.score,
        prediction_threshold=prediction.threshold,
        snapshot=snapshot,
    )


def save_html(snapshot: Snapshot, path: Path, title: str) -> Path:
    """Write the Evidently HTML with ``title`` as the page title and a banner at the top."""
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.save_html(str(path))
    text = path.read_text(encoding="utf-8")
    safe = html.escape(title)
    banner = (
        f'<div style="font:16px sans-serif;padding:12px;background:#fff3cd;color:#222">{safe}</div>'
    )
    text = text.replace("<head>", f"<head><title>{safe}</title>", 1)
    text = text.replace("<body>", f"<body>{banner}", 1) if "<body>" in text else banner + text
    path.write_text(text, encoding="utf-8")
    return path
