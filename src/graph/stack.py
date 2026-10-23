"""Does the GNN score help XGBoost? GNN score as an extra feature, cross-fitted.

Usage: ``python -m src.graph.stack --degree-limit N --epochs E`` (needs requirements-gnn.txt).
Writes reports/gnn_stacking.md and ``artifacts/gnn/scores.parquet`` (GNN score per
transaction, reused by ``src.graph.rings``), and logs to MLflow (experiment ``fraud-gnn``).

Leakage: the feature must be out-of-sample on every XGBoost training row, or XGBoost would
trust a score fitted to those same labels. Forward chaining: the training window is cut into
``FOLDS`` contiguous time blocks and block ``k`` is scored by a GNN trained only on the labels
of blocks ``< k`` (fixed epochs, no early stopping); block 0 gets no score (NaN, which XGBoost
treats as missing). So no training row's score is built from its own or future labels.
Validation rows are scored by a GNN trained on all training labels. Validation labels are
used only for the final metrics, and message passing is always temporal (strictly earlier
transactions only). Each XGBoost (with and without the feature) is
trained exactly as the served v2 model (``compare.train_variant``, no class weight).
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import pandas as pd
import torch

from src.array_types import FloatArray
from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.history import HistoryConfig
from src.features.split import chronological_split
from src.graph.build import (
    DEFAULT_DEGREE_LIMIT,
    GNN_SCORE,
    GNN_SCORES_PATH,
    TxGraph,
    build_graph,
)
from src.graph.data import to_hetero
from src.graph.train import GnnConfig, log_mlflow, predict, train_model
from src.models.compare import (
    ComparisonConfig,
    build_model_frame,
    feature_columns,
    score,
    train_variant,
)
from src.models.metrics import average_precision

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "reports" / "gnn_stacking.md"
SCORES = GNN_SCORES_PATH
FOLDS = 3
SEEDS = (42, 7, 2026)
FEATURE = GNN_SCORE


def time_blocks(t: np.ndarray[Any, Any], folds: int) -> np.ndarray[Any, Any]:
    """Block index 0..folds-1 per row, contiguous in time (rows with equal times share a block)."""
    edges = np.quantile(t, np.linspace(0, 1, folds + 1)[1:-1])
    return np.searchsorted(edges, t, side="right")


def forward_folds(
    block: np.ndarray[Any, Any], folds: int
) -> list[tuple[int, np.ndarray[Any, Any]]]:
    """``(k, train_mask)`` per scored block: train on earlier blocks only; block 0 is skipped."""
    return [(k, block < k) for k in range(1, folds)]


def crossfit_scores(
    graph: TxGraph, cfg: GnnConfig, folds: int = FOLDS, trainer: Any = train_model
) -> FloatArray:
    """Forward-chained GNN score per training row (NaN for the first block)."""
    block = time_blocks(graph.tx_time, folds)
    out = np.full(graph.num_tx, np.nan)
    data = to_hetero(graph)
    for k, train_mask in forward_folds(block, folds):
        result = trainer(graph, cfg, train_mask=train_mask)
        rows = np.flatnonzero(block == k).astype(np.int64)
        out[rows] = predict(result.model, data, rows, cfg)
        logger.info(
            "fold %d/%d scored rows=%d (%.0fs)", k + 1, folds, len(rows), result.train_seconds
        )
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--degree-limit", type=int, default=DEFAULT_DEGREE_LIMIT)
    parser.add_argument("--epochs", type=int, required=True, help="fixed GNN epochs (day-1 best)")
    parser.add_argument("--seed", type=int, default=42, help="GNN seed (XGBoost uses SEEDS)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    torch.set_num_threads(os.cpu_count() or 1)
    started = time.perf_counter()
    cfg = GnnConfig(seed=args.seed, max_epochs=args.epochs, degree_limit=args.degree_limit)
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    train_graph, _ = build_graph(df, include_validation=False, degree_limit=cfg.degree_limit)
    full_graph, _ = build_graph(df, include_validation=True, degree_limit=cfg.degree_limit)

    oof = crossfit_scores(train_graph, cfg)
    final = train_model(train_graph, cfg, train_mask=np.ones(train_graph.num_tx, dtype=bool))
    valid_rows = np.flatnonzero(full_graph.is_valid).astype(np.int64)
    valid_scores = predict(final.model, to_hetero(full_graph), valid_rows, cfg)
    scores = pd.concat(
        [
            pd.DataFrame({KEY: train_graph.tx_id, FEATURE: oof, "source": "forward-chained"}),
            pd.DataFrame(
                {KEY: full_graph.tx_id[valid_rows], FEATURE: valid_scores, "source": "validation"}
            ),
        ]
    )
    SCORES.parent.mkdir(parents=True, exist_ok=True)
    scores.to_parquet(SCORES, index=False)
    gnn_seconds = time.perf_counter() - started

    hist = HistoryConfig()
    frame = build_model_frame(df, "baseline", hist)
    frame[FEATURE] = frame[KEY].map(scores.set_index(KEY)[FEATURE]).to_numpy()
    if not frame[KEY].isin(scores[KEY]).all():
        raise AssertionError("every transaction must be in the score table")
    train, valid, _ = chronological_split(frame, valid_fraction=0.2)
    y = valid[LABEL].to_numpy()
    base_cols = feature_columns(hist)
    results: dict[str, list[float]] = {"xgboost_v2": [], "xgboost_plus_gnn": []}
    for seed in SEEDS:
        xcfg = ComparisonConfig(seed=seed, imbalance="none")
        for name, cols in (("xgboost_v2", base_cols), ("xgboost_plus_gnn", [*base_cols, FEATURE])):
            trained = train_variant(train, cols, xcfg)
            ap = average_precision(y, score(trained, valid.drop(columns=LABEL), cols))
            results[name].append(ap)
            logger.info("seed %d %s PR-AUC %.4f", seed, name, ap)
    gnn_alone = average_precision(y, frame.loc[valid.index, FEATURE].to_numpy())

    spread = max(max(v) - min(v) for v in results.values())
    gain = mean(results["xgboost_plus_gnn"]) - mean(results["xgboost_v2"])
    keep = gain > spread
    summary = {
        "results": results,
        "gain": gain,
        "spread": spread,
        "keep": keep,
        "gnn_alone_valid_pr_auc": gnn_alone,
        "gnn_cfg": cfg,
        "seconds": time.perf_counter() - started,
        "gnn_seconds": gnn_seconds,
    }
    from src.models.tracking import _git

    log_mlflow(
        replace(cfg),
        {
            "stack_mean_pr_auc": mean(results["xgboost_plus_gnn"]),
            "base_mean_pr_auc": mean(results["xgboost_v2"]),
            "gain": gain,
            "seed_spread": spread,
            "gnn_alone_valid_pr_auc": gnn_alone,
        },
        {"git_commit": _git("rev-parse", "HEAD") or "unknown", "stage": "stacking"},
    )
    REPORT.write_text(render(summary), encoding="utf-8")
    logger.info("wrote %s keep=%s gain=%.4f spread=%.4f", REPORT, keep, gain, spread)
    return summary


def render(s: dict[str, Any]) -> str:
    r = s["results"]
    rows = [
        f"| {name} | " + " | ".join(f"{v:.4f}" for v in r[name]) + f" | {mean(r[name]):.4f} | "
        f"{max(r[name]) - min(r[name]):.4f} |"
        for name in ("xgboost_v2", "xgboost_plus_gnn")
    ]
    verdict = (
        "**kept**: the gain is larger than the seed spread"
        if s["keep"]
        else "**no gain**: the difference is not larger than the seed spread"
    )
    cfg = s["gnn_cfg"]
    return "\n".join(
        [
            "# GNN score as an XGBoost feature",
            "",
            f"GNN: hetero GraphSAGE, degree limit {cfg.degree_limit:,}, {cfg.max_epochs} fixed "
            f"epochs, seed {cfg.seed}. Training rows get forward-chained scores ({FOLDS} "
            "contiguous time blocks; block k is scored by a GNN trained on blocks < k only; the "
            "first block has no score and is missing for XGBoost); validation rows are scored "
            "by a GNN trained on all training labels. XGBoost is trained exactly "
            "as the served v2 (no class weight) with and without the extra `gnn_score` column. "
            "Validation labels are used only for these metrics. Generated by "
            "`python -m src.graph.stack`.",
            "",
            "| Model | Seed 42 | Seed 7 | Seed 2026 | Mean PR-AUC | Spread |",
            "|---|---|---|---|---|---|",
            *rows,
            "",
            f"Gain {s['gain']:+.4f} vs seed spread {s['spread']:.4f} (larger of the two ranges): "
            f"{verdict}. The served model is not changed either way.",
            "",
            f"GNN score alone on validation: PR-AUC {s['gnn_alone_valid_pr_auc']:.4f}. "
            f"Runtime {s['seconds'] / 60:.0f} min "
            f"(GNN cross-fitting {s['gnn_seconds'] / 60:.0f} min).",
            "",
        ]
    )


if __name__ == "__main__":
    main()
