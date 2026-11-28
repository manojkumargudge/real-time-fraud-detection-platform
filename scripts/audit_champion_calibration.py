"""Measure the shipped champion's probabilities on its original validation window."""

import argparse
import json
from pathlib import Path

import numpy as np

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.split import chronological_split
from src.models.compare import build_model_frame
from src.models.metrics import average_precision
from src.serving.model import ModelBundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--model-dir", type=Path, default=Path("artifacts/baseline"))
    args = parser.parse_args()
    bundle = ModelBundle.load(args.model_dir)
    expected = bundle.metadata["split"]
    raw = load_ieee_cis(args.raw_dir, usecols=[])
    frame = build_model_frame(raw, "baseline", bundle.history_config)
    _, valid, split = chronological_split(
        frame,
        train_end=expected["train_end"],
        gap_seconds=expected["valid_start"] - expected["train_end"],
    )
    if split.valid_start != expected["valid_start"] or len(valid) != expected["valid_rows"]:
        raise ValueError("Validation split does not match the served bundle")
    labels = valid[LABEL].to_numpy(dtype=np.int64)
    scores = bundle.predict(valid.drop(columns=LABEL)).astype(np.float64)
    pr_auc = average_precision(labels, scores)
    if abs(pr_auc - bundle.metadata["validation_metrics"]["average_precision"]) > 1e-9:
        raise ValueError("Recomputed PR-AUC does not match the served bundle")
    clipped = np.clip(scores, 1e-15, 1 - 1e-15)
    bins = np.minimum((scores * 10).astype(np.int64), 9)
    error = 0.0
    table = []
    for index in range(10):
        mask = bins == index
        count = int(mask.sum())
        if count == 0:
            continue
        confidence = float(scores[mask].mean())
        observed = float(labels[mask].mean())
        error += (count / len(scores)) * abs(confidence - observed)
        table.append(
            dict(bin=index, count=count, mean_probability=confidence, observed_fraud_rate=observed)
        )
    print(
        json.dumps(
            {
                "model_version": bundle.model_version,
                "validation_start": split.valid_start,
                "rows": len(labels),
                "frauds": int(labels.sum()),
                "pr_auc": pr_auc,
                "brier": float(np.mean((scores - labels) ** 2)),
                "log_loss": float(
                    -np.mean(labels * np.log(clipped) + (1 - labels) * np.log1p(-clipped))
                ),
                "ece_10_equal_width": error,
                "bins": table,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
