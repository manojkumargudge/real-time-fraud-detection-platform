"""Train and save the baseline serving model (card_id / uid keys; D1 keys stay experimental).

Usage: ``python -m src.models.train [--seed N] [--register] [--no-track]`` writes to
``artifacts/baseline/`` (gitignored):

* ``model.json``    -- XGBoost booster
* ``metadata.json`` -- feature schema, encoder vocabulary, threshold, split, metrics, versions

and logs the run to MLflow (``src.models.tracking``; local ``mlflow.db`` unless
``MLFLOW_TRACKING_URI`` is set). ``--register`` also registers it as ``fraud-xgboost`` and
moves the ``champion`` alias to it.

Reuses the Milestone 2 protocol: one chronological split, ``train_variant`` (stage-1
holdout picks rounds and the max-F1 threshold, stage-2 refits on all training rows), and
validation labels used only for the reported metrics. The model is trained on the training
window only, so the stored validation metrics remain a genuine held-out estimate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import FloatArray, IntArray
from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.history import TIME, HistoryConfig
from src.features.split import chronological_split
from src.models.compare import (
    EXCLUDED,
    ComparisonConfig,
    TrainedVariant,
    build_model_frame,
    evaluate,
    feature_columns,
    score,
    train_variant,
)

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "baseline"
MODEL_FILE = "model.json"
METADATA_FILE = "metadata.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BaselineRun:
    """A trained baseline plus the validation scores, for run tracking (not saved in the bundle)."""

    trained: TrainedVariant
    metadata: dict[str, Any]
    y_valid: IntArray
    valid_scores: FloatArray
    train_fraud_rate: float


def train_baseline(
    df: pd.DataFrame, cfg: ComparisonConfig | None = None
) -> tuple[TrainedVariant, dict[str, Any]]:
    """Return ``(trained_variant, metadata)`` for the baseline variant."""
    run = fit_baseline(df, cfg)
    return run.trained, run.metadata


def fit_baseline(df: pd.DataFrame, cfg: ComparisonConfig | None = None) -> BaselineRun:
    cfg = cfg or ComparisonConfig()
    hist_cfg = HistoryConfig()
    cols = feature_columns(hist_cfg)
    started = time.perf_counter()

    frame = build_model_frame(df, "baseline", hist_cfg)
    train, valid, boundary = chronological_split(frame, valid_fraction=cfg.valid_fraction)
    trained = train_variant(train, cols, cfg)
    y_valid = valid[LABEL].to_numpy(dtype=np.int64)
    valid_scores = score(trained, valid.drop(columns=LABEL), cols)
    metrics = evaluate(y_valid, valid_scores, trained.threshold)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "variant": "baseline",
        "entity_keys": {"history": "uid", "change_flags": "card_id"},
        "feature_columns": cols,
        "excluded_inputs": list(EXCLUDED),
        "history_windows": dict(hist_cfg.windows),
        "change_columns": list(hist_cfg.change_columns),
        "encoder": trained.encoder.to_dict(),
        "threshold": trained.threshold,
        "threshold_policy": "max F1 on chronological training holdout (last 20% of train time)",
        "best_rounds": trained.best_rounds,
        "scale_pos_weight": trained.scale_pos_weight,
        "xgb_params": cfg.xgb_params,
        "seed": cfg.seed,
        "split": {
            "train_end": boundary.train_end,
            "valid_start": boundary.valid_start,
            "train_rows": len(train),
            "valid_rows": len(valid),
            "train_dt_min": int(train[TIME].min()),
        },
        "validation_metrics": metrics,
        "holdout_average_precision": trained.holdout_average_precision,
        "training_seconds": round(time.perf_counter() - started, 1),
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": asdict(cfg),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "xgboost": xgb.__version__,
        },
    }
    return BaselineRun(trained, metadata, y_valid, valid_scores, float(train[LABEL].mean()))


def save_bundle(trained: TrainedVariant, metadata: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / MODEL_FILE
    trained.model.save_model(model_path)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    metadata = {**metadata, "model_sha256": digest, "model_version": f"baseline-{digest[:12]}"}
    (out_dir / METADATA_FILE).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    logger.info("saved model version=%s dir=%s", metadata["model_version"], out_dir)
    return out_dir


def main(
    raw_dir: Path = DEFAULT_RAW_DIR,
    out_dir: Path = ARTIFACTS_DIR,
    cfg: ComparisonConfig | None = None,
    *,
    track: bool = False,
    register: bool = False,
) -> dict[str, Any]:
    """Train and save the bundle; with ``track``, also log the run to MLflow (``register``
    additionally registers it and points the ``champion`` alias at it)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    df = load_ieee_cis(raw_dir, usecols=[])
    run = fit_baseline(df, cfg)
    save_bundle(run.trained, run.metadata, out_dir)
    saved: dict[str, Any] = json.loads((out_dir / METADATA_FILE).read_text(encoding="utf-8"))
    if track or register:
        # Imported here: serving imports this module but does not ship mlflow.
        from src.models.tracking import log_training_run

        log_training_run(run, out_dir, register=register)
    return saved


def _cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ARTIFACTS_DIR)
    parser.add_argument("--seed", type=int, default=ComparisonConfig.seed)
    parser.add_argument("--no-track", action="store_true", help="skip MLflow logging")
    parser.add_argument(
        "--register", action="store_true", help="register the run and move the champion alias"
    )
    args = parser.parse_args(argv)
    main(
        out_dir=args.out_dir,
        cfg=ComparisonConfig(seed=args.seed),
        track=not args.no_track,
        register=args.register,
    )


if __name__ == "__main__":
    _cli()
