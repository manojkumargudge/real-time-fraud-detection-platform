"""Train and save the baseline serving model (card_id / uid keys; D1 keys stay experimental).

Usage: ``python -m src.models.train`` writes to ``artifacts/baseline/`` (gitignored):

* ``model.json``    -- XGBoost booster
* ``metadata.json`` -- feature schema, encoder vocabulary, threshold, split, metrics, versions

Reuses the Milestone 2 protocol: one chronological split, ``train_variant`` (stage-1
holdout picks rounds and the max-F1 threshold, stage-2 refits on all training rows), and
validation labels used only for the reported metrics. The model is trained on the training
window only, so the stored validation metrics remain a genuine held-out estimate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.history import TIME, HistoryConfig
from src.features.split import chronological_split
from src.models.compare import (
    EXCLUDED,
    ComparisonConfig,
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


def train_baseline(df: pd.DataFrame, cfg: ComparisonConfig | None = None) -> tuple[Any, dict]:
    """Return ``(trained_variant, metadata)`` for the baseline variant."""
    cfg = cfg or ComparisonConfig()
    hist_cfg = HistoryConfig()
    cols = feature_columns(hist_cfg)
    started = time.perf_counter()

    frame = build_model_frame(df, "baseline", hist_cfg)
    train, valid, boundary = chronological_split(frame, valid_fraction=cfg.valid_fraction)
    trained = train_variant(train, cols, cfg)
    metrics = evaluate(
        valid[LABEL].to_numpy(),
        score(trained, valid.drop(columns=LABEL), cols),
        trained.threshold,
    )
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
    return trained, metadata


def save_bundle(trained: Any, metadata: dict, out_dir: Path) -> Path:
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
) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    df = load_ieee_cis(raw_dir, usecols=[])
    trained, metadata = train_baseline(df, cfg)
    save_bundle(trained, metadata, out_dir)
    return json.loads((out_dir / METADATA_FILE).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
