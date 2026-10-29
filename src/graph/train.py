"""Train and evaluate the GNN (batch only).

Usage: ``python -m src.graph.train [--seed 42] [--epochs 8] [--degree-limit 1000]`` (needs
requirements-gnn.txt). Writes reports/gnn_checkpoint.md and logs the run to MLflow
(experiment ``fraud-gnn``, same tracking store as ``src.models.tracking``).

Label use (tests/test_graph.py):

* training: labels of the *fit* split only (training rows before the training holdout), on the
  training graph (transactions before the validation cut-off);
* early stopping and the threshold: the training holdout (last 20% of training time);
* validation labels: only in ``evaluate``, after training, for the reported metrics.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
from torch import nn

from src.array_types import FloatArray, IntArray
from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.data.quality import peak_memory_mb
from src.graph.build import DEFAULT_DEGREE_LIMIT, ENTITY_COLUMNS, TxGraph, build_graph
from src.graph.data import DEFAULT_FANOUT, TX, temporal_loader, to_hetero
from src.graph.model import HeteroSAGE
from src.models.metrics import (
    alert_budget_metrics,
    average_precision,
    select_threshold_max_f1,
    threshold_metrics,
)

logger = logging.getLogger(__name__)

REPORT = Path(__file__).resolve().parents[2] / "reports" / "gnn_checkpoint.md"
EXPERIMENT = "fraud-gnn"
RATES = (0.01, 0.02, 0.05)


@dataclass(frozen=True)
class GnnConfig:
    seed: int = 42
    hidden: int = 64
    layers: int = 2
    dropout: float = 0.1
    lr: float = 1e-3
    weight_decay: float = 1e-5
    batch_size: int = 1024
    fanout: tuple[int, ...] = DEFAULT_FANOUT
    max_epochs: int = 8
    patience: int = 2  # epochs without holdout PR-AUC improvement
    degree_limit: int = DEFAULT_DEGREE_LIMIT
    max_batches_per_epoch: int | None = None  # tests only


@dataclass
class TrainResult:
    model: HeteroSAGE
    best_epoch: int
    holdout_pr_auc: float
    threshold: float
    history: list[dict[str, float]] = field(default_factory=list)
    train_seconds: float = 0.0


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def predict(model: HeteroSAGE, data: Any, seeds: IntArray, cfg: GnnConfig) -> FloatArray:
    """Fraud probabilities for tx nodes ``seeds`` (in order), with temporal sampling.

    pyg-lib samples neighbors with torch's global RNG, so it is reseeded here: the scores do
    not depend on how many random draws happened before."""
    torch.manual_seed(cfg.seed)
    model.eval()
    out = []
    with torch.no_grad():
        for batch in temporal_loader(
            data, seeds, batch_size=4096, fanout=cfg.fanout, seed=cfg.seed
        ):
            logits = model(batch)[: batch[TX].batch_size]
            out.append(torch.sigmoid(logits).numpy())
    scores: FloatArray = np.concatenate(out).astype(np.float64)
    return scores


def train_model(
    graph: TxGraph, cfg: GnnConfig, *, train_mask: np.ndarray[Any, Any] | None = None
) -> TrainResult:
    """Fit on the fit split of a *training* graph; early-stop on its holdout split.

    With ``train_mask`` (cross-fitting): train on exactly those labelled rows for
    ``cfg.max_epochs`` epochs, without early stopping or a threshold (no holdout is used)."""
    if graph.is_valid.any():
        raise ValueError("train_model needs the training graph (no validation transactions)")
    set_seed(cfg.seed)
    data = to_hetero(graph)
    model = HeteroSAGE(
        data.metadata(),
        graph.x_num.shape[1],
        graph.category_sizes,
        hidden=cfg.hidden,
        layers=cfg.layers,
        dropout=cfg.dropout,
    )
    opt = torch.optim.Adam(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    loss_fn = nn.BCEWithLogitsLoss()
    fixed = train_mask is not None
    mask = graph.is_fit if train_mask is None else train_mask
    fit_seeds = np.flatnonzero(mask).astype(np.int64)
    hold_seeds = np.flatnonzero(graph.is_holdout).astype(np.int64)
    y_hold = graph.labels[hold_seeds]
    best: dict[str, Any] = {"ap": -1.0, "epoch": -1, "state": None}
    history: list[dict[str, float]] = []
    started = time.perf_counter()
    for epoch in range(cfg.max_epochs):
        model.train()
        torch.manual_seed(cfg.seed + epoch)  # neighbor sampling + dropout for this epoch
        t0, total, n = time.perf_counter(), 0.0, 0
        loader = temporal_loader(
            data,
            fit_seeds,
            batch_size=cfg.batch_size,
            fanout=cfg.fanout,
            shuffle=True,
            seed=cfg.seed + epoch,
        )
        for i, batch in enumerate(loader):
            if cfg.max_batches_per_epoch is not None and i >= cfg.max_batches_per_epoch:
                break
            k = batch[TX].batch_size
            y = batch[TX].y[:k].float()
            if (y < 0).any():
                raise AssertionError("a training seed has no label")
            opt.zero_grad()
            loss = loss_fn(model(batch)[:k], y)
            loss.backward()
            opt.step()
            total, n = total + float(loss.detach()) * k, n + k
        ap = (
            float("nan")
            if fixed
            else average_precision(y_hold, predict(model, data, hold_seeds, cfg))
        )
        history.append(
            {
                "epoch": epoch,
                "loss": total / max(n, 1),
                "holdout_pr_auc": ap,
                "seconds": time.perf_counter() - t0,
            }
        )
        logger.info(
            "epoch %d loss %.4f holdout PR-AUC %.4f (%.0fs)",
            epoch,
            total / max(n, 1),
            ap,
            time.perf_counter() - t0,
        )
        if fixed:
            continue
        if ap > best["ap"]:
            best = {
                "ap": ap,
                "epoch": epoch,
                "state": {k: v.clone() for k, v in model.state_dict().items()},
            }
        elif epoch - best["epoch"] >= cfg.patience:
            break
    if fixed:
        return TrainResult(
            model=model,
            best_epoch=cfg.max_epochs - 1,
            holdout_pr_auc=float("nan"),
            threshold=float("nan"),
            history=history,
            train_seconds=time.perf_counter() - started,
        )
    model.load_state_dict(best["state"])
    hold_scores = predict(model, data, hold_seeds, cfg)
    return TrainResult(
        model=model,
        best_epoch=int(best["epoch"]),
        holdout_pr_auc=float(best["ap"]),
        threshold=select_threshold_max_f1(y_hold, hold_scores),
        history=history,
        train_seconds=time.perf_counter() - started,
    )


def evaluate(
    model: HeteroSAGE, graph: TxGraph, valid_labels: IntArray, cfg: GnnConfig, threshold: float
) -> tuple[FloatArray, dict[str, Any]]:
    """Validation scores and metrics on the full graph (validation labels used only here)."""
    data = to_hetero(graph)
    seeds = np.flatnonzero(graph.is_valid).astype(np.int64)
    scores = predict(model, data, seeds, cfg)
    metrics = {
        "pr_auc": average_precision(valid_labels, scores),
        "budgets": {
            float(r["rate"] or 0): r for r in alert_budget_metrics(valid_labels, scores, RATES)
        },
        "at_threshold": threshold_metrics(valid_labels, scores, threshold).to_dict(),
    }
    return scores, metrics


# --------------------------------------------------------------------------- report / tracking


def xgboost_reference(df: pd.DataFrame, valid_ids: IntArray) -> tuple[FloatArray, str]:
    """Scores of the served XGBoost bundle (v2) on the same validation rows, same order."""
    from src.features.history import HistoryConfig
    from src.models.compare import build_model_frame
    from src.serving.model import ModelBundle

    bundle = ModelBundle.load(Path(__file__).resolve().parents[2] / "artifacts" / "baseline")
    frame = build_model_frame(df, "baseline", HistoryConfig()).set_index("TransactionID")
    rows = frame.loc[valid_ids].drop(columns=LABEL).reset_index()
    return bundle.predict(rows).astype(np.float64), bundle.model_version


def graph_summary(graph: TxGraph) -> dict[str, Any]:
    return {
        "tx_nodes": graph.num_tx,
        "valid_tx_nodes": int(graph.is_valid.sum()),
        "entity_nodes": {t: len(v) for t, v in graph.entity_names.items()},
        "edges": {t: int(v.shape[1]) for t, v in graph.edges.items()},
        "entity_edges": {"__".join(k): int(v.shape[1]) for k, v in graph.entity_edges.items()},
        "hubs": {t: len(v) for t, v in graph.hubs.items()},
    }


def log_mlflow(cfg: GnnConfig, metrics: dict[str, float], tags: dict[str, str]) -> str:
    """Log one run to ``fraud-gnn``. ``git_commit`` and ``git_dirty`` are always written here
    (as in src.models.tracking), after the caller's tags, so a caller cannot override them."""
    from mlflow.entities import Metric, Param
    from mlflow.tracking import MlflowClient

    from src.models import tracking

    tags = {
        **tags,
        "git_commit": tracking._git("rev-parse", "HEAD") or "unknown",
        "git_dirty": str(bool(tracking._git("status", "--porcelain"))).lower(),
    }
    uri = tracking.tracking_uri()
    client = MlflowClient(tracking_uri=uri)
    exp = client.get_experiment_by_name(EXPERIMENT)
    exp_id = (
        exp.experiment_id
        if exp
        else client.create_experiment(
            EXPERIMENT, artifact_location=tracking._artifact_location(uri)
        )
    )
    run = client.create_run(str(exp_id), tags=tags, run_name=f"gnn-seed{cfg.seed}")
    now = int(time.time() * 1000)
    client.log_batch(
        run.info.run_id,
        metrics=[Metric(k, float(v), now, 0) for k, v in metrics.items()],
        params=[Param(k, str(v)) for k, v in asdict(cfg).items()],
    )
    client.set_terminated(run.info.run_id)
    return str(run.info.run_id)


def log_artifacts(run_id: str, paths: list[Path], artifact_path: str = "outputs") -> None:
    """Attach files (score table, report) to an existing ``fraud-gnn`` run, once written."""
    from mlflow.tracking import MlflowClient

    from src.models import tracking

    client = MlflowClient(tracking_uri=tracking.tracking_uri())
    for path in paths:
        client.log_artifact(run_id, str(path), artifact_path)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--epochs", type=int, default=GnnConfig.max_epochs)
    parser.add_argument(
        "--degree-limit",
        type=int,
        default=DEFAULT_DEGREE_LIMIT,
        help="drop entity values above this training degree from the model graph (huge = keep)",
    )
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    torch.set_num_threads(os.cpu_count() or 1)
    cfg = GnnConfig(seed=args.seed, max_epochs=args.epochs, degree_limit=args.degree_limit)
    t0 = time.perf_counter()
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    train_graph, _ = build_graph(df, include_validation=False, degree_limit=cfg.degree_limit)
    full_graph, valid_labels = build_graph(
        df, include_validation=True, degree_limit=cfg.degree_limit
    )
    assert valid_labels is not None
    build_seconds = time.perf_counter() - t0
    result = train_model(train_graph, cfg)
    t1 = time.perf_counter()
    scores, metrics = evaluate(result.model, full_graph, valid_labels, cfg, result.threshold)
    eval_seconds = time.perf_counter() - t1
    valid_ids = full_graph.tx_id[full_graph.is_valid]
    xgb_scores, xgb_version = xgboost_reference(df, valid_ids)
    xgb = {
        "pr_auc": average_precision(valid_labels, xgb_scores),
        "budgets": {
            float(r["rate"] or 0): r for r in alert_budget_metrics(valid_labels, xgb_scores, RATES)
        },
    }
    peak = peak_memory_mb()
    flat = {
        "valid_pr_auc": metrics["pr_auc"],
        "holdout_pr_auc": result.holdout_pr_auc,
        "best_epoch": result.best_epoch,
        "train_seconds": result.train_seconds,
        "graph_build_seconds": build_seconds,
        "eval_seconds": eval_seconds,
        "peak_memory_mb": peak or float("nan"),
        **{
            f"valid_{m}_at_budget_{r:.0%}".replace("%", "pct"): float(metrics["budgets"][r][m] or 0)
            for r in RATES
            for m in ("precision", "recall")
        },
    }
    run_id = log_mlflow(cfg, flat, {"model": "hetero-graphsage"})
    summary = {
        "config": asdict(cfg),
        "graph_train": graph_summary(train_graph),
        "graph_full": graph_summary(full_graph),
        "hubs": train_graph.hubs,
        "gnn": metrics,
        "xgboost": xgb,
        "xgboost_version": xgb_version,
        "history": result.history,
        "threshold": result.threshold,
        "timing": {
            "graph_build_seconds": build_seconds,
            "train_seconds": result.train_seconds,
            "eval_seconds": eval_seconds,
        },
        "peak_memory_mb": peak,
        "mlflow_run_id": run_id,
        "n_valid": int(len(valid_labels)),
        "valid_fraud_rate": float(valid_labels.mean()),
    }
    args.report.write_text(render(summary), encoding="utf-8")
    logger.info("wrote %s", args.report)
    return summary


def render(s: dict[str, Any]) -> str:
    g, f = s["graph_train"], s["graph_full"]
    lines = [
        "# GNN day-1 checkpoint",
        "",
        "Heterogeneous GraphSAGE on transaction nodes, temporal neighbor sampling (only strictly "
        "earlier transactions, per seed). Generated by `python -m src.graph.train`. "
        f"Seed {s['config']['seed']}; MLflow run `{s['mlflow_run_id']}` (experiment `fraud-gnn`).",
        "",
        "## Graph",
        "",
        f"Degree limit: entity values linked to more than {s['config']['degree_limit']:,} training "
        "transactions are dropped (counted on training rows only).",
        "",
        "| Node / edge type | Training graph | Full graph (train + validation) "
        "| Hub values dropped |",
        "|---|---|---|---|",
        f"| tx nodes | {g['tx_nodes']:,} | {f['tx_nodes']:,} "
        f"({f['valid_tx_nodes']:,} validation) | - |",
        *(
            f"| {t} nodes / edges | {g['entity_nodes'][t]:,} / {g['edges'][t]:,} | "
            f"{f['entity_nodes'][t]:,} / {f['edges'][t]:,} | {g['hubs'][t]} |"
            for t in ENTITY_COLUMNS
        ),
        "",
        "Edges are stored once per direction (tx -> entity and entity -> tx).",
        "",
        "Dropped hub values (training degree):",
        "",
    ]
    for t, vals in s["hubs"].items():
        shown = ", ".join(f"`{v}` ({d:,})" for v, d in vals[:15])
        more = f", ... and {len(vals) - 15} more" if len(vals) > 15 else ""
        lines.append(f"- {t} ({len(vals)}): {shown}{more}" if vals else f"- {t}: none")
    lines += [
        "",
        "## Validation (same split as XGBoost; labels used only here)",
        "",
        f"{s['n_valid']:,} validation transactions, fraud rate {s['valid_fraud_rate']:.2%}.",
        "",
        "| Model | PR-AUC | Precision @1% | Recall @1% | Precision @2% | Recall @2% "
        "| Precision @5% | Recall @5% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, m in (
        ("GNN (hetero GraphSAGE)", s["gnn"]),
        (f"XGBoost v2 `{s['xgboost_version']}`", s["xgboost"]),
    ):
        cells = " | ".join(
            f"{float(m['budgets'][r]['precision']):.4f} | {float(m['budgets'][r]['recall']):.4f}"
            for r in RATES
        )
        lines.append(f"| {name} | {m['pr_auc']:.4f} | {cells} |")
    t = s["timing"]
    lines += [
        "",
        f"GNN threshold (max F1 on the training holdout): {s['threshold']:.4f}.",
        "",
        "## Training",
        "",
        "| Epoch | Loss | Holdout PR-AUC | Seconds |",
        "|---|---|---|---|",
        *(
            f"| {int(h['epoch'])} | {h['loss']:.4f} | {h['holdout_pr_auc']:.4f} "
            f"| {h['seconds']:.0f} |"
            for h in s["history"]
        ),
        "",
        f"Graph build {t['graph_build_seconds']:.0f} s (both graphs), "
        f"training {t['train_seconds']:.0f} s, "
        f"validation inference {t['eval_seconds']:.0f} s; peak process memory "
        f"{s['peak_memory_mb']:,.0f} MB.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
