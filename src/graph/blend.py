"""Does a blend (1 - w) * XGBoost + w * GNN help? Two variants, chosen out of sample (experiment).

Usage: ``python -m src.graph.blend`` after ``python -m src.graph.stack`` (reads
``artifacts/gnn/scores.parquet``). Writes reports/gnn_blend.md and logs one MLflow run per
variant (experiment ``fraud-gnn``). Batch only: nothing here is served.

Variants (``gnn`` is the second blend input):

* ``transaction``: the transaction's own GNN score. Validation scores come from the stack
  run's final GNN on the full graph: earlier validation transactions are message-passing
  neighbours (features only, no labels), as they would be online.
* ``entity``: mean of the uid's out-of-fold GNN scores on strictly earlier training blocks;
  a uid without one gets ``gnn = xgb``, so its blend equals the XGBoost score.

Selection never sees validation. ``w`` is chosen on the last training block (block 2 of the
stack run's forward chaining), where every input is out of sample: XGBoost (v2 recipe) is
trained on blocks 0-1, the transaction score comes from the GNN trained on blocks 0-1, and the
entity score averages block-1 scores. Rule: the largest ``w`` whose paired day-block bootstrap
95% CI lower bound of AP(blend) - AP(XGBoost) is at least ``-MARGIN``. The chosen ``w`` is then
evaluated once on validation (XGBoost trained on all training rows; entity score from blocks
1-2) with the same bootstrap.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.array_types import FloatArray
from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.entity import USER_ID, build_entity_keys
from src.features.history import TIME, HistoryConfig
from src.features.split import chronological_split
from src.graph.build import GNN_SCORE, GNN_SCORES_PATH
from src.models.compare import (
    ComparisonConfig,
    build_model_frame,
    feature_columns,
    score,
    train_variant,
)
from src.models.metrics import average_precision, day_block_bootstrap_ap_delta

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "reports" / "gnn_blend.md"
WEIGHTS = tuple(round(0.05 * i, 2) for i in range(11))  # 0.00 .. 0.50
MARGIN = 0.01  # same as the promotion margin (configs/drift.toml)
RESAMPLES = 1_000
SEED = 42
FOLDS = 3  # src.graph.stack.FOLDS (not imported: that module needs torch)
SELECTION_BLOCK = FOLDS - 1
VARIANTS = ("transaction", "entity")


def time_blocks(t: np.ndarray[Any, Any], folds: int = FOLDS) -> np.ndarray[Any, Any]:
    """Same blocks as ``src.graph.stack.time_blocks`` (contiguous in time; ties share a block)."""
    edges = np.quantile(t, np.linspace(0, 1, folds + 1)[1:-1])
    return np.searchsorted(edges, t, side="right")


def blend(xgb: FloatArray, gnn: FloatArray, w: float) -> FloatArray:
    """``(1 - w) * xgb + w * gnn``; a missing ``gnn`` falls back to ``xgb`` (blend = xgb)."""
    g = np.where(np.isnan(gnn), xgb, gnn)
    out: FloatArray = (1.0 - w) * xgb + w * g
    return out


def entity_scores(history: pd.DataFrame, uids: pd.Series[Any]) -> FloatArray:
    """Mean GNN score per uid over ``history`` (rows with a score), looked up for ``uids``;
    NaN for a uid without any scored history row."""
    scored = history.dropna(subset=[GNN_SCORE])
    means = scored.groupby(USER_ID)[GNN_SCORE].mean()
    return uids.map(means).to_numpy(dtype=np.float64)


def choose_weight(rows: list[dict[str, Any]], margin: float = MARGIN) -> float:
    """The largest weight whose CI lower bound of AP(blend) - AP(xgb) is >= -margin."""
    ok = [r["w"] for r in rows if r["ci95_low"] >= -margin]
    return float(max(ok)) if ok else 0.0


@dataclass(frozen=True)
class Side:
    """One evaluation set: labels, days and the two blend inputs, all the same length."""

    name: str
    y: np.ndarray[Any, Any]
    day: np.ndarray[Any, Any]
    xgb: FloatArray
    gnn: FloatArray

    def __post_init__(self) -> None:
        n = len(self.y)
        if n == 0 or int(self.y.sum()) == 0:
            raise AssertionError(f"{self.name}: empty side or no frauds")
        if not len(self.day) == len(self.xgb) == len(self.gnn) == n:
            raise AssertionError(f"{self.name}: sides differ in length")
        if np.isnan(self.xgb).any():
            raise AssertionError(f"{self.name}: XGBoost scores have NaN")

    def counts(self) -> str:
        cover = int((~np.isnan(self.gnn)).sum())
        return (
            f"{self.name}: rows {len(self.y):,}, frauds {int(self.y.sum()):,}, "
            f"with a GNN score {cover:,} ({cover / len(self.y):.1%})"
        )


def compare(side: Side, w: float) -> dict[str, Any]:
    b = blend(side.xgb, side.gnn, w)
    boot = day_block_bootstrap_ap_delta(
        side.y, side.xgb, b, side.day, resamples=RESAMPLES, seed=SEED
    )
    return {
        "w": w,
        "ap_xgb": average_precision(side.y, side.xgb),
        "ap_blend": average_precision(side.y, b),
        **boot,
    }


def build_sides(raw_dir: Path) -> tuple[dict[str, Side], dict[str, Side], dict[str, Any]]:
    """Selection (last training block) and validation sides for both variants."""
    df = load_ieee_cis(raw_dir, usecols=[])
    hist = HistoryConfig()
    cols = feature_columns(hist)
    frame = build_model_frame(df, "baseline", hist)
    frame[USER_ID] = build_entity_keys(df)[USER_ID]
    scores = pd.read_parquet(GNN_SCORES_PATH).set_index(KEY)
    frame[GNN_SCORE] = frame[KEY].map(scores[GNN_SCORE]).to_numpy()
    frame["gnn_source"] = frame[KEY].map(scores["source"]).to_numpy()
    if frame["gnn_source"].isna().any():
        raise AssertionError("every transaction must be in the GNN score table")
    train, valid, boundary = chronological_split(frame, valid_fraction=0.2)
    if not (train["gnn_source"] == "forward-chained").all():
        raise AssertionError("training rows must carry forward-chained (out-of-fold) scores")
    if not (valid["gnn_source"] == "validation").all():
        raise AssertionError("validation rows must carry the final GNN's scores")

    block = time_blocks(train[TIME].to_numpy())
    # The stack run leaves exactly block 0 unscored; this ties our blocks to its blocks.
    if not np.array_equal(np.isnan(train[GNN_SCORE].to_numpy()), block == 0):
        raise AssertionError("time blocks differ from the stack run's forward chaining")
    early, last = train[block < SELECTION_BLOCK], train[block == SELECTION_BLOCK]
    cfg = ComparisonConfig(seed=SEED, imbalance="none")
    xgb_last = score(
        train_variant(early.drop(columns=[USER_ID, GNN_SCORE, "gnn_source"]), cols, cfg), last, cols
    )
    xgb_valid = score(
        train_variant(train.drop(columns=[USER_ID, GNN_SCORE, "gnn_source"]), cols, cfg),
        valid,
        cols,
    )

    def side(name: str, rows: pd.DataFrame, xgb: FloatArray, gnn: FloatArray) -> Side:
        return Side(name, rows[LABEL].to_numpy(), (rows[TIME] // 86_400).to_numpy(), xgb, gnn)

    selection = {
        "transaction": side("selection/transaction", last, xgb_last, last[GNN_SCORE].to_numpy()),
        "entity": side("selection/entity", last, xgb_last, entity_scores(early, last[USER_ID])),
    }
    validation = {
        "transaction": side(
            "validation/transaction", valid, xgb_valid, valid[GNN_SCORE].to_numpy()
        ),
        "entity": side("validation/entity", valid, xgb_valid, entity_scores(train, valid[USER_ID])),
    }
    info = {
        "train_end": boundary.train_end,
        "selection_days": (int(last[TIME].min() // 86_400), int(last[TIME].max() // 86_400)),
        "early_rows": len(early),
        "scores_sha256": hashlib.sha256(GNN_SCORES_PATH.read_bytes()).hexdigest(),
    }
    return selection, validation, info


def log_mlflow(variant: str, result: dict[str, Any], info: dict[str, Any]) -> str:
    from mlflow.entities import Metric, Param
    from mlflow.tracking import MlflowClient

    from src.graph.train import EXPERIMENT
    from src.models import tracking

    uri = tracking.tracking_uri()
    client = MlflowClient(tracking_uri=uri)
    exp = client.get_experiment_by_name(EXPERIMENT)
    if exp is None:
        raise RuntimeError(f"MLflow experiment {EXPERIMENT!r} not found (run src.graph.stack)")
    tags = {
        "stage": "blend",
        "variant": variant,
        "git_commit": tracking._git("rev-parse", "HEAD") or "unknown",
        "git_dirty": str(bool(tracking._git("status", "--porcelain"))).lower(),
    }
    run = client.create_run(str(exp.experiment_id), tags=tags, run_name=f"blend-{variant}")
    now = int(time.time() * 1000)
    metrics = [
        Metric(f"sel_{k}", float(r[k]), now, int(round(r["w"] * 100)))
        for r in result["sweep"]
        for k in ("ap_blend", "delta_mean", "ci95_low", "ci95_high")
    ]
    v = result["validation"]
    metrics += [Metric("chosen_w", result["chosen_w"], now, 0)]
    metrics += [
        Metric(f"valid_{k}", float(v[k]), now, 0)
        for k in ("ap_xgb", "ap_blend", "delta_mean", "ci95_low", "ci95_high", "share_b_better")
    ]
    params = {
        "weights": ",".join(map(str, WEIGHTS)),
        "margin": MARGIN,
        "resamples": RESAMPLES,
        "seed": SEED,
        "rule": "largest w: CI95 low of AP(blend)-AP(xgb) >= -margin, last training block",
        "scores_sha256": info["scores_sha256"],
        "selection_days": info["selection_days"],
    }
    client.log_batch(
        run.info.run_id, metrics=metrics, params=[Param(k, str(x)) for k, x in params.items()]
    )
    client.set_terminated(run.info.run_id)
    return str(run.info.run_id)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    started = time.perf_counter()
    selection, validation, info = build_sides(args.raw_dir)
    results: dict[str, Any] = {}
    for variant in VARIANTS:
        sel, val = selection[variant], validation[variant]
        print(sel.counts())
        print(val.counts())
        sweep = [compare(sel, w) for w in WEIGHTS]
        for r in sweep:
            logger.info(
                "%s w=%.2f AP %.4f delta CI [%.4f, %.4f]",
                variant,
                r["w"],
                r["ap_blend"],
                r["ci95_low"],
                r["ci95_high"],
            )
        chosen = choose_weight(sweep)
        valid_result = compare(val, chosen)  # the one look at validation
        results[variant] = {
            "selection": sel.counts(),
            "validation_counts": val.counts(),
            "sweep": sweep,
            "chosen_w": chosen,
            "validation": valid_result,
        }
        results[variant]["run_id"] = log_mlflow(variant, results[variant], info)
        logger.info("%s chosen w=%.2f validation %s", variant, chosen, valid_result)
    summary = {"results": results, "info": info, "seconds": time.perf_counter() - started}
    REPORT.write_text(render(summary), encoding="utf-8")
    logger.info("wrote %s", REPORT)
    return summary


def verdict(v: dict[str, Any]) -> str:
    if v["w"] == 0:
        return "w = 0: the blend is XGBoost alone"
    if v["ci95_low"] > MARGIN:
        return "better than XGBoost by more than the margin"
    if v["ci95_low"] >= -MARGIN:
        return "not worse than XGBoost by more than the margin (no evidence of a gain)"
    return "worse than XGBoost by more than the margin"


def render(s: dict[str, Any]) -> str:
    info = s["info"]
    d0, d1 = info["selection_days"]
    out = [
        "# GNN + XGBoost blend",
        "",
        "`(1 - w) * xgb + w * gnn`, generated by `python -m src.graph.blend` from "
        f"`artifacts/gnn/scores.parquet` (sha256 `{info['scores_sha256'][:16]}…`).",
        "",
        f"**Selection (never validation):** the last training block (days {d0}-{d1}), with "
        "out-of-fold inputs only: XGBoost (v2 recipe, seed 42) trained on the earlier blocks "
        f"({info['early_rows']:,} rows); the transaction score from the GNN trained on the "
        "earlier blocks; the entity score = mean of the uid's block-1 scores. Rule: the largest "
        f"`w` in 0-0.5 whose paired day-block bootstrap ({RESAMPLES:,} resamples) 95% CI lower "
        f"bound of AP(blend) - AP(xgb) is >= -{MARGIN}. **Validation** is evaluated once, at "
        "the chosen `w` (XGBoost trained on all training rows; entity score from blocks 1-2).",
        "",
    ]
    for variant, r in s["results"].items():
        v = r["validation"]
        out += [
            f"## Variant: {variant}",
            "",
            f"- {r['selection']}",
            f"- {r['validation_counts']}",
            "",
            "| w | AP blend | AP xgb | delta mean | 95% CI |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| {x['w']:.2f} | {x['ap_blend']:.4f} | {x['ap_xgb']:.4f} | "
            f"{x['delta_mean']:+.4f} | [{x['ci95_low']:+.4f}, {x['ci95_high']:+.4f}] |"
            for x in r["sweep"]
        ]
        out += [
            "",
            f"**Chosen w = {r['chosen_w']:.2f}.** Validation (one look): AP blend "
            f"{v['ap_blend']:.4f} vs XGBoost {v['ap_xgb']:.4f}, delta {v['delta_mean']:+.4f}, "
            f"95% CI [{v['ci95_low']:+.4f}, {v['ci95_high']:+.4f}] ({v['days']} days, "
            f"{v['resamples']:,} resamples); **{verdict(v)}**. MLflow run `{r['run_id']}`.",
            "",
        ]
    out += [
        "## Limitations",
        "",
        "- **Transaction variant, validation scores:** produced by the final GNN on the full "
        "graph, so earlier validation transactions are message-passing neighbours (features "
        "only, no labels). Temporal sampling keeps every neighbour strictly earlier. This is what "
        "an online GNN would see, but it is not a training-only graph, and serving it would need "
        "torch and a live graph in the scorer.",
        "- **Selection block vs validation:** selection uses GNNs and XGBoost trained on two "
        "thirds of the training window; validation uses models trained on all of it. A weight "
        "that suits the weaker selection models need not suit the final ones.",
        "- **Entity variant:** a static uid average, frozen at the end of the history window; "
        "most uids have few scored transactions behind it, and block 0 has no scores by design.",
        '- **Rule:** "largest w not worse by more than the margin" can pick a w that adds '
        "nothing; the verdict line says whether there is evidence of a gain.",
        "- One GNN seed (42) and one XGBoost seed (42); the stacking report shows a seed spread "
        "of about 0.01 PR-AUC for XGBoost alone.",
        "",
        f"Runtime {s['seconds'] / 60:.0f} min.",
        "",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    main()
