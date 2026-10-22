"""Baseline (card_id/uid) vs candidate (card_d1/uid_d1) entity keys, scored by XGBoost.

Usage: ``python -m src.models.compare`` writes ``reports/model_comparison.{json,md}``
(aggregates only: no rows, identifiers, predictions or model files).

Protocol (identical for both variants; validation labels are used only in ``evaluate``):

1. History features over the full chronological file (strictly-earlier rows, no labels).
2. One shared chronological split: train = earliest 80% of time, validation = the rest.
3. Stage 1 (threshold + rounds): split train again in time into fit / holdout. Fit the encoder
   and XGBoost on fit, early-stop on holdout, pick the max-F1 threshold on holdout scores.
4. Stage 2: refit the encoder and XGBoost on all training rows with the stage-1 round count,
   score validation, and apply the stage-1 threshold unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import FloatArray, NumArray
from src.data.load import DEFAULT_RAW_DIR, HAS_IDENTITY, KEY, LABEL, load_ieee_cis
from src.features.entity import (
    CARD_ID,
    CARD_START_ID,
    SECONDS_PER_DAY,
    USER_ID,
    USER_START_ID,
    build_account_start_keys,
    build_entity_keys,
)
from src.features.history import TIME, HistoryConfig, compute_history_features
from src.features.preprocess import DEFAULT_CATEGORICAL, CategoricalEncoder
from src.features.split import chronological_split
from src.models.metrics import (
    average_precision,
    day_block_bootstrap_ap_delta,
    select_threshold_max_f1,
    threshold_metrics,
)

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

RAW_NUMERIC = (
    "TransactionAmt",
    "card1",
    "card2",
    "card3",
    "card5",
    "addr1",
    "addr2",
    "dist1",
    HAS_IDENTITY,
)
# Never model inputs: identifiers, raw time (non-stationary offset), label, raw D1, entity keys.
EXCLUDED = (KEY, TIME, LABEL, "D1", CARD_ID, USER_ID)
VARIANTS = ("baseline", "candidate")


def feature_columns(config: HistoryConfig | None = None) -> list[str]:
    return [*RAW_NUMERIC, *DEFAULT_CATEGORICAL, *(config or HistoryConfig()).feature_names()]


# Point-in-time / leakage audit, recorded in the report.
FEATURE_AUDIT: dict[str, str] = {
    "TransactionAmt, card1-3, card5, addr1, addr2, dist1, categorical fields": (
        "fields of the scored transaction itself; known at authorisation; no labels"
    ),
    HAS_IDENTITY: "identity row exists for this transaction; assumed captured at authorisation",
    "uid_* history": "strictly earlier rows of the same entity; no labels (tested)",
    "card_*_changed": "last known value among strictly earlier rows; no labels (tested)",
    "candidate keys (D1)": (
        "UNRESOLVED: D1 is undocumented by the provider. It is assumed to be a backward-looking "
        "day count delivered with the transaction. If the provider derived it with hindsight, the "
        "candidate keys could carry future information. Raw D1 is excluded from model inputs."
    ),
}


@dataclass(frozen=True)
class ComparisonConfig:
    valid_fraction: float = 0.2
    holdout_fraction: float = 0.2  # of the training window, used for rounds + threshold
    seed: int = 42
    max_rounds: int = 2000
    early_stopping_rounds: int = 100
    bootstrap_resamples: int = 500
    xgb_params: dict[str, Any] = field(
        default_factory=lambda: {
            "objective": "binary:logistic",
            "eval_metric": "aucpr",
            "max_depth": 8,
            "eta": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 1.0,
            "tree_method": "hist",
        }
    )


@dataclass
class TrainedVariant:
    encoder: CategoricalEncoder
    model: xgb.Booster
    threshold: float
    best_rounds: int
    scale_pos_weight: float
    holdout_average_precision: float
    seconds: float


def build_model_frame(df: pd.DataFrame, variant: str, config: HistoryConfig) -> pd.DataFrame:
    """Model inputs for one variant (+ label/time/ID for splitting), aligned to ``df.index``."""
    if variant == "baseline":
        keys = build_entity_keys(df)
    elif variant == "candidate":
        keys = build_account_start_keys(df).rename(
            columns={CARD_START_ID: CARD_ID, USER_START_ID: USER_ID}
        )
    else:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")
    hist = compute_history_features(df, config, keys=keys)
    raw = df[[KEY, TIME, *RAW_NUMERIC, *DEFAULT_CATEGORICAL]].copy()
    raw[HAS_IDENTITY] = raw[HAS_IDENTITY].astype("float32")
    if LABEL in df.columns:
        raw[LABEL] = df[LABEL]
    return pd.concat([raw, hist[config.feature_names()]], axis=1)


def _matrix(frame: pd.DataFrame, encoder: CategoricalEncoder, cols: list[str]) -> pd.DataFrame:
    return encoder.transform(frame[cols]).astype("float32")


def _scale_pos_weight(y: pd.Series[int]) -> float:
    pos = int(y.sum())
    if pos == 0:
        raise ValueError("training labels contain no positives")
    return float((len(y) - pos) / pos)


def _params(cfg: ComparisonConfig, spw: float) -> dict[str, Any]:
    return {**cfg.xgb_params, "scale_pos_weight": spw, "seed": cfg.seed}


def _dmatrix(x: pd.DataFrame, y: pd.Series[int] | None = None) -> xgb.DMatrix:
    return xgb.DMatrix(x, label=None if y is None else y.to_numpy(), missing=np.nan)


def train_variant(train: pd.DataFrame, cols: list[str], cfg: ComparisonConfig) -> TrainedVariant:
    """Fit using training rows only. Validation data never reaches this function."""
    started = time.perf_counter()
    fit, holdout, _ = chronological_split(train, valid_fraction=cfg.holdout_fraction)

    enc1 = CategoricalEncoder().fit(fit)
    d_hold = _dmatrix(_matrix(holdout, enc1, cols), holdout[LABEL])
    stage1 = xgb.train(
        _params(cfg, _scale_pos_weight(fit[LABEL])),
        _dmatrix(_matrix(fit, enc1, cols), fit[LABEL]),
        num_boost_round=cfg.max_rounds,
        evals=[(d_hold, "holdout")],
        early_stopping_rounds=cfg.early_stopping_rounds,
        verbose_eval=False,
    )
    rounds = int(stage1.best_iteration) + 1
    hold_scores = stage1.predict(d_hold, iteration_range=(0, rounds))
    threshold = select_threshold_max_f1(holdout[LABEL].to_numpy(), hold_scores)
    hold_ap = average_precision(holdout[LABEL].to_numpy(), hold_scores)

    encoder = CategoricalEncoder().fit(train)
    spw = _scale_pos_weight(train[LABEL])
    model = xgb.train(
        _params(cfg, spw), _dmatrix(_matrix(train, encoder, cols), train[LABEL]), rounds
    )
    return TrainedVariant(
        encoder=encoder,
        model=model,
        threshold=threshold,
        best_rounds=rounds,
        scale_pos_weight=spw,
        holdout_average_precision=hold_ap,
        seconds=time.perf_counter() - started,
    )


def score(trained: TrainedVariant, frame: pd.DataFrame, cols: list[str]) -> FloatArray:
    """Scores for every row of ``frame`` (labels are not read). NaN inputs are kept."""
    return trained.model.predict(_dmatrix(_matrix(frame, trained.encoder, cols)))


def evaluate(y: NumArray, scores: NumArray, threshold: float) -> dict[str, Any]:
    return {
        "average_precision": average_precision(y, scores),
        "at_threshold": threshold_metrics(y, scores, threshold).to_dict(),
    }


def _id_digest(ids: pd.Series[int]) -> str:
    return hashlib.sha256(np.sort(ids.to_numpy(dtype=np.int64)).tobytes()).hexdigest()[:16]


def run_comparison(df: pd.DataFrame, cfg: ComparisonConfig | None = None) -> dict[str, Any]:
    cfg = cfg or ComparisonConfig()
    hist_cfg = HistoryConfig()
    cols = feature_columns(hist_cfg)

    # One split for both variants, decided on the raw frame before any variant exists.
    train_idx_frame, valid_idx_frame, boundary = chronological_split(
        df[[TIME]], valid_fraction=cfg.valid_fraction
    )
    train_index, valid_index = train_idx_frame.index, valid_idx_frame.index
    y_valid = df.loc[valid_index, LABEL].to_numpy()

    results: dict[str, Any] = {}
    valid_scores: dict[str, FloatArray] = {}
    digests = set()
    for variant in VARIANTS:
        t0 = time.perf_counter()
        frame = build_model_frame(df, variant, hist_cfg)
        feature_seconds = time.perf_counter() - t0
        train, valid = frame.loc[train_index], frame.loc[valid_index]
        digests.add(_id_digest(valid[KEY]))

        trained = train_variant(train, cols, cfg)
        t1 = time.perf_counter()
        s = score(trained, valid.drop(columns=LABEL), cols)
        if len(s) != len(valid_index):
            raise RuntimeError("validation rows were dropped during scoring")
        valid_scores[variant] = s
        metrics = evaluate(y_valid, s, trained.threshold)
        importance = trained.model.get_score(importance_type="gain")
        # "gain" scores are floats; the list form only exists for multi-output models
        gains = {k: v for k, v in importance.items() if isinstance(v, float)}
        top = sorted(gains.items(), key=lambda kv: -kv[1])[:10]
        results[variant] = {
            **metrics,
            "threshold_source": "max F1 on chronological training holdout (stage 1)",
            "holdout_average_precision": trained.holdout_average_precision,
            "best_rounds": trained.best_rounds,
            "scale_pos_weight": trained.scale_pos_weight,
            "top10_gain_features": [name for name, _ in top],
            "seconds": {
                "features": round(feature_seconds, 1),
                "train": round(trained.seconds, 1),
                "evaluate": round(time.perf_counter() - t1, 1),
            },
        }
        logger.info(
            "variant=%s valid_ap=%.4f holdout_ap=%.4f rounds=%d",
            variant,
            metrics["average_precision"],
            trained.holdout_average_precision,
            trained.best_rounds,
        )
        del frame, train, valid

    if len(digests) != 1:
        raise RuntimeError("variants were evaluated on different validation rows")
    day = (df.loc[valid_index, TIME] // SECONDS_PER_DAY).to_numpy()
    bootstrap = day_block_bootstrap_ap_delta(
        y_valid,
        valid_scores["baseline"],
        valid_scores["candidate"],
        day,
        resamples=cfg.bootstrap_resamples,
        seed=cfg.seed,
    )
    return {
        "dataset": "IEEE-CIS train (labelled file only)",
        "split": {
            "train_end": boundary.train_end,
            "valid_start": boundary.valid_start,
            "train_rows": len(train_index),
            "valid_rows": len(valid_index),
            "valid_fraud_prevalence": float(y_valid.mean()),
            "valid_id_digest": digests.pop(),
        },
        "config": {**asdict(cfg), "history_windows": dict(hist_cfg.windows)},
        "features": {"model_inputs": cols, "excluded": list(EXCLUDED)},
        "variants": {
            "baseline": "history keys card_id / uid",
            "candidate": "history keys card_d1 / uid_d1 (estimated account-start day from D1)",
        },
        "feature_audit": FEATURE_AUDIT,
        "results": results,
        "ap_delta_candidate_minus_baseline_day_bootstrap": bootstrap,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "xgboost": xgb.__version__,
        },
    }


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def render_markdown(report: dict[str, Any]) -> str:
    r, b, sp = (
        report["results"],
        report["ap_delta_candidate_minus_baseline_day_bootstrap"],
        report["split"],
    )
    rows = []
    for v in VARIANTS:
        m, t = r[v], r[v]["at_threshold"]
        rows.append(
            f"| {v} | {m['average_precision']:.4f} | {m['holdout_average_precision']:.4f} | "
            f"{t['threshold']:.4f} | {t['precision']:.4f} | {t['recall']:.4f} | {t['f1']:.4f} | "
            f"{t['flagged']:,} ({_pct(t['flagged_rate'])}) | {m['best_rounds']} |"
        )
    cm = [
        f"| {v} | {r[v]['at_threshold']['tp']:,} | {r[v]['at_threshold']['fp']:,} | "
        f"{r[v]['at_threshold']['fn']:,} | {r[v]['at_threshold']['tn']:,} |"
        for v in VARIANTS
    ]
    secs = [
        f"| {v} | {r[v]['seconds']['features']} | {r[v]['seconds']['train']} | "
        f"{r[v]['seconds']['evaluate']} |"
        for v in VARIANTS
    ]
    env = report["environment"]
    return "\n".join(
        [
            "# Model comparison: baseline vs D1-based entity keys",
            "",
            "Generated by `python -m src.models.compare`; raw numbers in "
            "[`model_comparison.json`](model_comparison.json).",
            "",
            f"Validation: {sp['valid_rows']:,} rows (`TransactionDT >= {sp['valid_start']:,}`), "
            f"fraud prevalence {_pct(sp['valid_fraud_prevalence'])}. Training: "
            f"{sp['train_rows']:,} rows. Both variants scored the identical rows "
            f"(ID digest `{sp['valid_id_digest']}`).",
            "",
            "| Variant | Valid PR-AUC | Train-holdout PR-AUC | Threshold | Precision | Recall | F1 "
            "| Flagged | Rounds |",
            "|---|---|---|---|---|---|---|---|---|",
            *rows,
            "",
            "Confusion matrices at the training-selected threshold:",
            "",
            "| Variant | TP | FP | FN | TN |",
            "|---|---|---|---|---|",
            *cm,
            "",
            f"PR-AUC delta (candidate − baseline), paired day-block bootstrap over {b['days']} "
            f"validation days, {b['resamples']} resamples: mean {b['delta_mean']:+.4f}, "
            f"95% CI [{b['ci95_low']:+.4f}, {b['ci95_high']:+.4f}], candidate better in "
            f"{_pct(b['share_b_better'])} of resamples.",
            "",
            "Durations (seconds):",
            "",
            "| Variant | Features | Train (both stages) | Score + evaluate |",
            "|---|---|---|---|",
            *secs,
            "",
            "Threshold policy: maximise F1 on a chronological holdout carved from the training "
            "window (last 20% of training time); the same holdout chooses the boosting rounds. "
            "The model is then refit on all training rows and the threshold is applied unchanged. "
            "Validation labels are used only for the numbers above.",
            "",
            f"Environment: Python {env['python']}, numpy {env['numpy']}, pandas {env['pandas']}, "
            f"xgboost {env['xgboost']}, seed {report['config']['seed']}.",
            "",
            "Interpretation and caveats: see [docs/features.md](../docs/features.md) §1b.",
            "",
        ]
    )


def main(
    raw_dir: Path = DEFAULT_RAW_DIR,
    out_dir: Path = REPORTS_DIR,
    cfg: ComparisonConfig | None = None,
) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    df = load_ieee_cis(raw_dir, usecols=[])
    report = run_comparison(df, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model_comparison.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "model_comparison.md").write_text(render_markdown(report), encoding="utf-8")
    logger.info("wrote %s", out_dir / "model_comparison.json")
    return report


if __name__ == "__main__":
    main()
