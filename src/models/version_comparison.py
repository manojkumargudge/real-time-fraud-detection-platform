"""Compare two registered model versions on the standard validation split at fixed alert budgets.

Usage: ``python -m src.models.version_comparison [--old 1] [--new 2]`` downloads both versions of
``fraud-xgboost`` from the MLflow registry, scores the Milestone 2 validation window (the last
20% of ``TransactionDT``, the split stored in both bundles) through the serving loader
(``ModelBundle.predict``), and writes ``reports/model_v1_vs_v2.md``.

At a budget ``k`` each model alerts on its own top ``ceil(k * n)`` validation scores, so both
flag the same number of transactions and differ only in which ones; the reported threshold is
the lowest score inside the budget. This uses validation labels and scores, so it describes
the past window, not a threshold to deploy (serving keeps each bundle's own threshold).
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.array_types import FloatArray, IntArray
from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.history import HistoryConfig
from src.features.split import chronological_split
from src.models.compare import build_model_frame
from src.models.metrics import alert_budget_metrics, average_precision, threshold_metrics
from src.serving.model import ModelBundle

logger = logging.getLogger(__name__)

REPORT = Path(__file__).resolve().parents[2] / "reports" / "model_v1_vs_v2.md"
RATES = (0.01, 0.02, 0.05)
AP_TOLERANCE = 1e-9  # recomputed vs stored validation PR-AUC


@dataclass(frozen=True)
class VersionResult:
    version: str
    model_version: str
    imbalance: str
    pr_auc: float
    deployed: dict[str, float]  # at the bundle's own threshold
    budgets: dict[float, dict[str, float]]  # rate -> precision, recall, threshold, alerts, tp


def evaluate_version(
    version: str, bundle: ModelBundle, y: IntArray, s: FloatArray
) -> VersionResult:
    ap = average_precision(y, s)
    stored = float(bundle.metadata["validation_metrics"]["average_precision"])
    if abs(ap - stored) > AP_TOLERANCE:
        raise AssertionError(
            f"version {version}: recomputed PR-AUC {ap} != stored {stored}; not the same split"
        )
    at = threshold_metrics(y, s, bundle.threshold)
    budgets = {
        float(r["rate"] or 0): {
            "precision": float(r["precision"] or 0),
            "recall": float(r["recall"] or 0),
            "threshold": float(r["cutoff_score"] or 0),
            "alerts": float(r["alerts"] or 0),
            "tp": float(r["tp"] or 0),
        }
        for r in alert_budget_metrics(y, s, RATES)
    }
    return VersionResult(
        version,
        bundle.model_version,
        str(bundle.metadata["config"].get("imbalance", "scale_pos_weight")),
        ap,
        {
            "threshold": bundle.threshold,
            "precision": at.precision,
            "recall": at.recall,
            "alert_rate": at.flagged_rate,
            "alerts": float(at.flagged),
        },
        budgets,
    )


def worse_points(old: VersionResult, new: VersionResult) -> list[str]:
    """Plain statements of every compared metric where ``new`` is below ``old``."""
    out = []
    for rate in RATES:
        for metric in ("precision", "recall"):
            a, b = old.budgets[rate][metric], new.budgets[rate][metric]
            if b < a:
                out.append(
                    f"at the {rate:.0%} budget, {metric} is lower: {b:.4f} vs {a:.4f} "
                    f"({b - a:+.4f})"
                )
    if new.deployed["recall"] < old.deployed["recall"]:
        out.append(
            "at each bundle's own deployed threshold, recall is lower: "
            f"{new.deployed['recall']:.4f} vs {old.deployed['recall']:.4f} "
            f"(it also flags fewer transactions: {new.deployed['alert_rate']:.2%} vs "
            f"{old.deployed['alert_rate']:.2%})"
        )
    if new.deployed["precision"] < old.deployed["precision"]:
        out.append(
            "at each bundle's own deployed threshold, precision is lower: "
            f"{new.deployed['precision']:.4f} vs {old.deployed['precision']:.4f}"
        )
    return out


def render(old: VersionResult, new: VersionResult, rows: int, positives: int) -> str:
    def name(v: VersionResult) -> str:
        return f"v{v.version} (`{v.model_version}`, {v.imbalance})"

    lines = [
        f"# Model v{old.version} vs v{new.version} at fixed alert budgets",
        "",
        f"Validation window: the standard Milestone 2 split stored in both bundles, {rows:,} rows, "
        f"{positives:,} frauds ({positives / rows:.2%}). Scored with the serving loader; each "
        "model's recomputed PR-AUC matches its stored value. At budget *k* each model alerts on "
        "its own top *k* of validation scores (same alert count for both); *threshold* is the "
        "lowest score inside the budget. These budget thresholds use validation scores, so they "
        "describe this window, not a deployable setting. Generated by "
        "`python -m src.models.version_comparison`.",
        "",
        f"- {name(old)}: validation PR-AUC {old.pr_auc:.4f}",
        f"- {name(new)}: validation PR-AUC {new.pr_auc:.4f}",
        "",
        f"| Budget | Alerts | Recall v{old.version} | Recall v{new.version} | "
        f"Precision v{old.version} | Precision v{new.version} | "
        f"Threshold v{old.version} | Threshold v{new.version} |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rate in RATES:
        a, b = old.budgets[rate], new.budgets[rate]
        lines.append(
            f"| {rate:.0%} | {int(a['alerts']):,} | {a['recall']:.4f} | {b['recall']:.4f} | "
            f"{a['precision']:.4f} | {b['precision']:.4f} | {a['threshold']:.4f} | "
            f"{b['threshold']:.4f} |"
        )
    lines += [
        "",
        "At each bundle's own deployed threshold (max F1 on its training holdout):",
        "",
        "| Model | Threshold | Alert rate | Recall | Precision |",
        "|---|---|---|---|---|",
        *(
            f"| v{v.version} | {v.deployed['threshold']:.4f} | {v.deployed['alert_rate']:.2%} | "
            f"{v.deployed['recall']:.4f} | {v.deployed['precision']:.4f} |"
            for v in (old, new)
        ),
        "",
        f"Where v{new.version} is worse:",
        "",
    ]
    worse = worse_points(old, new)
    lines += [f"- {w}" for w in worse] or ["- nowhere among the metrics above."]
    lines.append("")
    return "\n".join(lines)


def _download(version: str, into: Path) -> ModelBundle:
    from mlflow.tracking import MlflowClient

    from src.models import tracking

    uri = tracking.tracking_uri()
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    mv = client.get_model_version(tracking.MODEL_NAME, version)
    path = client.download_artifacts(str(mv.run_id), tracking.ARTIFACT_PATH, str(into))
    return ModelBundle.load(path)


def main(old: str = "1", new: str = "2", report: Path = REPORT) -> tuple[Any, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with tempfile.TemporaryDirectory() as tmp:
        bundles = {v: _download(v, Path(tmp) / f"v{v}") for v in (old, new)}
    splits = {v: b.metadata["split"] for v, b in bundles.items()}
    if splits[old]["valid_start"] != splits[new]["valid_start"]:
        raise AssertionError(f"versions were validated on different windows: {splits}")
    frac = float(bundles[new].metadata["config"]["valid_fraction"])

    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    frame = build_model_frame(df, "baseline", HistoryConfig())
    _, valid, boundary = chronological_split(frame, valid_fraction=frac)
    if (
        boundary.valid_start != splits[new]["valid_start"]
        or len(valid) != splits[new]["valid_rows"]
    ):
        raise AssertionError("rebuilt validation window differs from the bundles' split")
    y = valid[LABEL].to_numpy(dtype=np.int64)
    x = valid.drop(columns=LABEL)
    results = [evaluate_version(v, bundles[v], y, bundles[v].predict(x)) for v in (old, new)]
    report.write_text(render(results[0], results[1], len(y), int(y.sum())), encoding="utf-8")
    logger.info("wrote %s", report)
    return results[0], results[1]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", default="1")
    parser.add_argument("--new", default="2")
    args = parser.parse_args()
    main(args.old, args.new)
