"""Milestone 2.2 robust evaluation: windows x seeds x variants x early-stopping policies.

Usage (aggregate-only reports under ``reports/``):

    python -m src.models.robust_eval --pilot   # 3 pairs = 6 model runs; review gates first
    python -m src.models.robust_eval --full    # 27 pairs = 54 model runs

Existing reports for the chosen mode are never replaced unless ``--overwrite`` is passed.

``fit_variant`` is the ``compare.train_variant`` protocol with a selectable round policy:

* ``early_stop_100``  -- the current rule (patience ``cfg.early_stopping_rounds``, 100 by
  default). Bit-identical to ``compare.train_variant`` (tested).
* ``floor_50``        -- same patience, but only rounds >= 50 can be selected, so a noisy early
  peak (e.g. the 6-round gap-60 model) cannot end training.
* ``fixed_300``       -- exactly 300 rounds, no early stopping (300 was fixed before any
  Milestone 2.2 run, from the round counts of earlier models chosen on training holdouts).

Every policy uses training rows only: the stage-1 holdout (last 20% of training time) picks
rounds and the max-F1 threshold, and stage 2 refits on all training rows. ``compare.py`` and
the serving model are unchanged; the alternative policies are diagnostics, not defaults.

Validation windows (``vs`` = the Milestone 2 cutoff, the 80% time quantile):
W1 ``[vs - 60d, vs - 30d)``, W2 ``[vs - 30d, vs)``, W3 ``[vs, end]``. W3 equals the existing
validation set. Each window trains on rows strictly before ``start - gap``.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import BoolArray, FloatArray, NumArray
from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.data.quality import peak_memory_mb
from src.features.entity import SECONDS_PER_DAY, build_entity_keys
from src.features.history import TIME, HistoryConfig
from src.features.preprocess import CategoricalEncoder
from src.features.split import chronological_split, time_cutoff
from src.models.compare import (
    REPORTS_DIR,
    ComparisonConfig,
    TrainedVariant,
    _dmatrix,
    _id_digest,
    _matrix,
    _params,
    _scale_pos_weight,
    build_model_frame,
    score,
)
from src.models.evaluation_study import (
    BASELINE,
    RESTRICTED,
    SUBGROUP_DEFINITIONS,
    group_metrics,
    study_variants,
    subgroup_masks,
)
from src.models.leakage_audit import IDENTIFIER_INPUTS
from src.models.metrics import (
    alert_budget_metrics,
    average_precision,
    budget_alert_mask,
    budget_size,
    day_block_bootstrap_ap_delta,
    select_threshold_max_f1,
)

logger = logging.getLogger(__name__)

Policy = Literal["early_stop_100", "floor_50", "fixed_300"]
POLICIES: tuple[Policy, ...] = ("early_stop_100", "floor_50", "fixed_300")
FLOOR_ROUNDS = 50
FIXED_ROUNDS = 300
WINDOW_DAYS = 30
BUDGET_RATES = (0.005, 0.01, 0.02, 0.05)
CURVE_CHECKPOINTS = (10, 50, 100, 200, 400, 800)
NEAR_BEST_TOLERANCE = 0.005
LOW_ROUNDS = 20
HOLDOUT = "holdout"
METRIC = "aucpr"


# --------------------------------------------------------------------------- windows


@dataclass(frozen=True)
class ValidationWindow:
    name: str
    start: int  # inclusive TransactionDT
    end: int | None  # exclusive; None = to the end of the data


def define_windows(
    dt: pd.Series[int], *, valid_fraction: float = 0.2, window_days: int = WINDOW_DAYS
) -> list[ValidationWindow]:
    """W1, W2 (``window_days`` each) before W3, which starts at the Milestone 2 cutoff."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    vs = time_cutoff(dt, valid_fraction)
    w = window_days * SECONDS_PER_DAY
    windows = [
        ValidationWindow("W1", vs - 2 * w, vs - w),
        ValidationWindow("W2", vs - w, vs),
        ValidationWindow("W3", vs, None),
    ]
    if windows[0].start <= int(dt.min()):
        raise ValueError(
            f"W1 starts at {windows[0].start}, not after the first timestamp {int(dt.min())}: "
            "no training data would precede it"
        )
    return windows


def window_masks(
    dt: pd.Series[int], window: ValidationWindow, gap_seconds: int = 0
) -> tuple[pd.Series[bool], pd.Series[bool]]:
    """Boolean (train, valid) masks. Train ``< start - gap``; valid ``[start, end)``."""
    if gap_seconds < 0:
        raise ValueError("gap_seconds must be >= 0")
    train = dt < window.start - gap_seconds
    valid = dt >= window.start
    if window.end is not None:
        valid &= dt < window.end
    return train, valid


def window_split(
    frame: pd.DataFrame, window: ValidationWindow, gap_seconds: int = 0
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split with strict checks: both sides non-empty, disjoint, and time-ordered."""
    train_mask, valid_mask = window_masks(frame[TIME], window, gap_seconds)
    train, valid = frame.loc[train_mask], frame.loc[valid_mask]
    if train.empty or valid.empty:
        raise ValueError(
            f"window {window.name} leaves an empty side (train={len(train)}, valid={len(valid)})"
        )
    if not train[TIME].max() < window.start - gap_seconds <= window.start <= valid[TIME].min():
        raise AssertionError(f"window {window.name} violates chronological ordering")
    if train.index.intersection(valid.index).size:
        raise AssertionError(f"window {window.name} has rows in both train and validation")
    return train, valid


# --------------------------------------------------------------------------- policies


class FloorPatience:
    """Online round selection mirroring XGBoost ``EarlyStopping`` above a minimum round.

    XGBoost treats a score as an improvement only if strictly greater than the best so far and
    stops once ``patience`` rounds pass without one. Here rounds below ``floor`` are never
    candidates and cannot trigger a stop.
    """

    def __init__(self, floor: int, patience: int) -> None:
        if floor < 1 or patience < 1:
            raise ValueError("floor and patience must be >= 1")
        self.floor, self.patience = floor, patience
        self.best_rounds: int | None = None
        self.best_score = -np.inf

    def update(self, rounds: int, score: float) -> bool:
        """Record the score after ``rounds`` boosting rounds; return True to stop training."""
        if rounds < self.floor:
            return False
        if self.best_rounds is None or np.greater(score, self.best_score):
            self.best_rounds, self.best_score = rounds, score
        return rounds - self.best_rounds >= self.patience


class _FloorCallback(xgb.callback.TrainingCallback):
    def __init__(self, selector: FloorPatience) -> None:
        super().__init__()
        self.selector = selector

    def after_iteration(
        self, model: Any, epoch: int, evals_log: xgb.callback.TrainingCallback.EvalsLog
    ) -> bool:
        last = evals_log[HOLDOUT][METRIC][-1]  # (mean, std) tuples only occur in xgb.cv
        return self.selector.update(epoch + 1, float(last[0] if isinstance(last, tuple) else last))


def curve_summary(curve: list[float], selected_rounds: int) -> dict[str, Any]:
    """Aggregate description of the stage-1 holdout PR-AUC curve (no per-row data)."""
    c = np.asarray(curve, dtype=np.float64)
    best_idx = int(np.argmax(c))  # first maximum, as XGBoost reports it
    return {
        "rounds_evaluated": len(c),
        "best_round": best_idx + 1,
        "best_aucpr": float(c[best_idx]),
        "selected_rounds": selected_rounds,
        "selected_aucpr": float(c[selected_rounds - 1]),
        "aucpr_at": {str(r): (float(c[r - 1]) if r <= len(c) else None) for r in CURVE_CHECKPOINTS},
        "rounds_within_tolerance_of_best": int((c >= c[best_idx] - NEAR_BEST_TOLERANCE).sum()),
        "tolerance": NEAR_BEST_TOLERANCE,
        "selected_below_low_rounds": selected_rounds < LOW_ROUNDS,
    }


def holdout_budget_thresholds(
    holdout_scores: NumArray, rates: tuple[float, ...] = BUDGET_RATES
) -> dict[str, float]:
    """Mode B thresholds: the ``budget_size``-th highest *training-holdout* score per rate."""
    ordered = np.sort(np.asarray(holdout_scores, dtype=np.float64))[::-1]
    if not len(ordered):
        raise ValueError("holdout scores are empty")
    return {str(k): float(ordered[budget_size(len(ordered), k) - 1]) for k in rates}


@dataclass
class FittedVariant:
    policy: Policy
    trained: TrainedVariant  # usable with compare.score
    curve: dict[str, Any]
    budget_thresholds: dict[str, float]  # Mode B, from training-holdout scores only


def fit_variant(
    train: pd.DataFrame,
    cols: list[str],
    cfg: ComparisonConfig,
    policy: Policy = "early_stop_100",
    *,
    floor_rounds: int = FLOOR_ROUNDS,
    fixed_rounds: int = FIXED_ROUNDS,
) -> FittedVariant:
    """``compare.train_variant`` with a selectable stage-1 round policy (training rows only)."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; expected one of {POLICIES}")
    if policy == "floor_50" and floor_rounds >= cfg.max_rounds:
        raise ValueError("floor_rounds must be below cfg.max_rounds")
    started = time.perf_counter()
    fit, holdout, _ = chronological_split(train, valid_fraction=cfg.holdout_fraction)

    enc1 = CategoricalEncoder().fit(fit)
    d_hold = _dmatrix(_matrix(holdout, enc1, cols), holdout[LABEL])
    d_fit = _dmatrix(_matrix(fit, enc1, cols), fit[LABEL])
    params = _params(cfg, _scale_pos_weight(fit[LABEL]))
    log: dict[str, Any] = {}

    if policy == "early_stop_100":  # identical call to compare.train_variant (+ recorded log)
        stage1 = xgb.train(
            params,
            d_fit,
            num_boost_round=cfg.max_rounds,
            evals=[(d_hold, HOLDOUT)],
            early_stopping_rounds=cfg.early_stopping_rounds,
            evals_result=log,
            verbose_eval=False,
        )
        rounds = int(stage1.best_iteration) + 1
    elif policy == "floor_50":
        selector = FloorPatience(floor_rounds, cfg.early_stopping_rounds)
        stage1 = xgb.train(
            params,
            d_fit,
            num_boost_round=cfg.max_rounds,
            evals=[(d_hold, HOLDOUT)],
            callbacks=[_FloorCallback(selector)],
            evals_result=log,
            verbose_eval=False,
        )
        rounds = int(selector.best_rounds) if selector.best_rounds else cfg.max_rounds
    else:
        stage1 = xgb.train(
            params,
            d_fit,
            num_boost_round=fixed_rounds,
            evals=[(d_hold, HOLDOUT)],
            evals_result=log,
            verbose_eval=False,
        )
        rounds = fixed_rounds

    hold_scores = stage1.predict(d_hold, iteration_range=(0, rounds))
    threshold = select_threshold_max_f1(holdout[LABEL].to_numpy(), hold_scores)
    hold_ap = average_precision(holdout[LABEL].to_numpy(), hold_scores)

    encoder = CategoricalEncoder().fit(train)
    spw = _scale_pos_weight(train[LABEL])
    model = xgb.train(
        _params(cfg, spw), _dmatrix(_matrix(train, encoder, cols), train[LABEL]), rounds
    )
    trained = TrainedVariant(
        encoder=encoder,
        model=model,
        threshold=threshold,
        best_rounds=rounds,
        scale_pos_weight=spw,
        holdout_average_precision=hold_ap,
        seconds=time.perf_counter() - started,
    )
    return FittedVariant(
        policy=policy,
        trained=trained,
        curve=curve_summary(log[HOLDOUT][METRIC], rounds),
        budget_thresholds=holdout_budget_thresholds(hold_scores),
    )


# --------------------------------------------------------------------------- grid definition

SEEDS_MAIN = (42, 1, 2, 3, 4)
SEEDS_SENSITIVITY = (42, 1, 2)
SENSITIVITY_POLICIES: tuple[Policy, ...] = ("floor_50", "fixed_300")
WINDOW_NAMES = ("W1", "W2", "W3")
DELTA_SUBGROUPS = ("all", "card_seen", "card_unseen", "uid_seen", "uid_unseen")
MIN_POSITIVES = 50
BOOTSTRAP_RESAMPLES = 200

# Decision rule for the unseen-uid advantage (Milestone 2.2 plan section 4), fixed before any
# Milestone 2.2 result: restricted - baseline PR-AUC on uid_unseen, early_stop_100 policy.
DECISION_SUBGROUP = "uid_unseen"
DECISION_POLICY: Policy = "early_stop_100"
MIN_POSITIVE_RUNS = 12  # of the 15 (window, seed) runs
MIN_WINDOWS_CI_ABOVE_ZERO = 2  # of 3 windows; median per-seed 95% CI lower bound > 0

# Pilot regression requirement: W3 / seed 42 / early_stop_100 must reproduce the Milestone 2.1
# gap-0 results. Expected AP values and the validation digest are read from the committed
# reports/evaluation_study.json (single source of truth; no duplicated constants) and compared
# unrounded: |observed - expected| <= REGRESSION_TOLERANCE for each variant, digest exact.
# (A 4-decimal comparison was fragile: the reference no_identifier_codes AP 0.21065249... lies
# ~2.5e-6 above the 0.21065 rounding boundary.)
REGRESSION_TARGET: dict[str, Any] = {"window": "W3", "seed": 42, "policy": "early_stop_100"}
REGRESSION_REFERENCE = REPORTS_DIR / "evaluation_study.json"
REGRESSION_REFERENCE_GAP = "0"
REGRESSION_TOLERANCE = 1e-6  # absolute, on unrounded average precision
# Stop conditions (plan section 6).
MAX_PEAK_MEMORY_MB = 8_000.0
MAX_PROJECTED_SECONDS = 2.5 * 3600

GAP_STATEMENT = (
    "All windows use gap 0 (no label-delay gap). Any gap setting in this project is a "
    "sensitivity assumption, not a measurement of chargeback latency."
)


@dataclass(frozen=True)
class RunSpec:
    window: str
    seed: int
    policy: Policy


def main_grid() -> list[RunSpec]:
    """3 windows x 5 seeds with the production round policy (15 pairs = 30 model runs)."""
    return [RunSpec(w, s, "early_stop_100") for w in WINDOW_NAMES for s in SEEDS_MAIN]


def sensitivity_grid() -> list[RunSpec]:
    """W1, W3 x 3 seeds x the two alternative policies (12 pairs = 24 model runs)."""
    return [
        RunSpec(w, s, p)
        for w in ("W1", "W3")
        for s in SEEDS_SENSITIVITY
        for p in SENSITIVITY_POLICIES
    ]


def pilot_grid() -> list[RunSpec]:
    """Regression check (W3 seed 42) + the W1 window that hosted the 6-round model."""
    return [
        RunSpec("W3", 42, "early_stop_100"),
        RunSpec("W1", 42, "early_stop_100"),
        RunSpec("W1", 1, "early_stop_100"),
    ]


@dataclass(frozen=True)
class StudySettings:
    model: ComparisonConfig = field(default_factory=ComparisonConfig)
    window_days: int = WINDOW_DAYS
    gap_seconds: int = 0
    budget_rates: tuple[float, ...] = BUDGET_RATES
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES
    min_positives: int = MIN_POSITIVES


# --------------------------------------------------------------------------- per-run evaluation


def _ratio(num: float, den: float) -> float | None:
    return float(num / den) if den else None


def _subgroup_breakdown(
    y: NumArray, alerts: BoolArray, masks: dict[str, BoolArray]
) -> dict[str, dict[str, Any]]:
    """How one window-level alert set falls into each subgroup."""
    out = {}
    for g, m in masks.items():
        n_alerts = int((alerts & m).sum())
        tp = int((alerts & m & (y == 1)).sum())
        pos = int(y[m].sum())
        out[g] = {
            "rows": int(m.sum()),
            "positives": pos,
            "alerts": n_alerts,
            "tp": tp,
            "precision": _ratio(tp, n_alerts),
            "recall": _ratio(tp, pos),
        }
    return out


def budget_results(
    y: NumArray,
    scores: NumArray,
    masks: dict[str, BoolArray],
    rates: tuple[float, ...],
    holdout_thresholds: dict[str, float],
) -> dict[str, dict[str, Any]]:
    """Mode A (top-k of window scores) and Mode B (training-holdout score threshold).

    Both alert sets come from scores only (Mode B's threshold from training-holdout scores);
    labels are read afterwards to count hits.
    """
    n = len(y)
    summary_a = {r["rate"]: r for r in alert_budget_metrics(y, scores, rates)}
    mode_a, mode_b = {}, {}
    for k in rates:
        a = budget_alert_mask(scores, k)
        mode_a[str(k)] = {
            "alerts": int(a.sum()),
            "alert_rate": _ratio(int(a.sum()), n),
            "cutoff_score": summary_a[float(k)]["cutoff_score"],
            "boundary_ties": summary_a[float(k)]["boundary_ties"],
            "tied_excluded": summary_a[float(k)]["tied_excluded"],
            "subgroups": _subgroup_breakdown(y, a, masks),
        }
        threshold = holdout_thresholds[str(k)]
        b = scores >= threshold
        mode_b[str(k)] = {
            "threshold": threshold,
            "alerts": int(b.sum()),
            "alert_rate": _ratio(int(b.sum()), n),
            "subgroups": _subgroup_breakdown(y, b, masks),
        }
    return {"mode_a_top_k": mode_a, "mode_b_holdout_threshold": mode_b}


def evaluate_pair(
    frame: pd.DataFrame,
    keys: pd.DataFrame,
    window: ValidationWindow,
    spec: RunSpec,
    settings: StudySettings,
    variants: dict[str, list[str]],
) -> dict[str, Any]:
    """Fit both variants on identical training rows and score identical validation rows."""
    started = time.perf_counter()
    train, valid = window_split(frame, window, settings.gap_seconds)
    y = valid[LABEL].to_numpy()
    day = (valid[TIME] // SECONDS_PER_DAY).to_numpy()
    masks = subgroup_masks(keys.loc[train.index], keys.loc[valid.index])
    x_valid = valid.drop(columns=LABEL)  # scoring never sees validation labels
    cfg = replace(settings.model, seed=spec.seed)
    resamples = settings.bootstrap_resamples

    results: dict[str, Any] = {}
    scores: dict[str, FloatArray] = {}
    for name, cols in variants.items():
        t0 = time.perf_counter()
        fitted = fit_variant(train, cols, cfg, spec.policy)
        t = fitted.trained
        s = score(t, x_valid, cols)
        scores[name] = s
        results[name] = {
            "n_inputs": len(cols),
            "rounds": t.best_rounds,
            "low_rounds": t.best_rounds < LOW_ROUNDS,
            "threshold": t.threshold,
            "scale_pos_weight": t.scale_pos_weight,
            "holdout_average_precision": t.holdout_average_precision,
            "curve": fitted.curve,
            "subgroups": {
                g: group_metrics(
                    y[m],
                    s[m],
                    t.threshold,
                    day[m],
                    min_positives=settings.min_positives,
                    resamples=resamples,
                    seed=spec.seed,
                )
                for g, m in masks.items()
            },
            "budgets": budget_results(y, s, masks, settings.budget_rates, fitted.budget_thresholds),
            "seconds": round(time.perf_counter() - t0, 1),
        }

    deltas: dict[str, Any] = {}
    for g in DELTA_SUBGROUPS:
        m = masks[g]
        pos = int(y[m].sum())
        if pos < settings.min_positives:
            deltas[g] = {"evaluated": False, "positives": pos}
            continue
        ap = {n: results[n]["subgroups"][g]["ranking"]["average_precision"] for n in variants}
        entry: dict[str, Any] = {
            "evaluated": True,
            "positives": pos,
            "point": ap[RESTRICTED] - ap[BASELINE],
            "ci95_low": None,
            "ci95_high": None,
        }
        if resamples:
            b = day_block_bootstrap_ap_delta(
                y[m],
                scores[BASELINE][m],
                scores[RESTRICTED][m],
                day[m],
                resamples=resamples,
                seed=spec.seed,
            )
            entry.update(
                ci95_low=b["ci95_low"],
                ci95_high=b["ci95_high"],
                share_restricted_better=b["share_b_better"],
                resamples=b["resamples"],
            )
        deltas[g] = entry

    return {
        "window": spec.window,
        "seed": spec.seed,
        "policy": spec.policy,
        "train_rows": len(train),
        "train_frauds": int(train[LABEL].sum()),
        "valid_rows": len(valid),
        "valid_frauds": int(y.sum()),
        "valid_digest": _id_digest(valid[KEY]),
        "subgroup_rows": {g: int(m.sum()) for g, m in masks.items()},
        "variants": results,
        "deltas_restricted_minus_baseline": deltas,
        "pair_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_mb": peak_memory_mb(),
    }


# --------------------------------------------------------------------------- aggregation


def _stats(values: list[float | None]) -> dict[str, Any]:
    v = np.asarray([x for x in values if x is not None and np.isfinite(x)], dtype=np.float64)
    return {
        "n": int(len(v)),
        "mean": float(v.mean()) if len(v) else None,
        "std": float(v.std(ddof=1)) if len(v) > 1 else None,
        "min": float(v.min()) if len(v) else None,
        "max": float(v.max()) if len(v) else None,
    }


def _median(values: list[float | None]) -> float | None:
    v = [x for x in values if x is not None and np.isfinite(x)]
    return float(np.median(v)) if v else None


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Across-seed summaries per window / policy / variant, and paired deltas."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in runs:
        groups.setdefault((r["window"], r["policy"]), []).append(r)

    models: dict[str, Any] = {}
    deltas: dict[str, Any] = {}
    for (w, policy), rs in sorted(groups.items()):
        seeds = sorted(r["seed"] for r in rs)
        for name in (BASELINE, RESTRICTED):
            vs = [r["variants"][name] for r in rs]
            budgets: dict[str, Any] = {}
            for mode in ("mode_a_top_k", "mode_b_holdout_threshold"):
                budgets[mode] = {}
                for k in vs[0]["budgets"][mode]:
                    entry = {
                        "alert_rate": _stats([v["budgets"][mode][k]["alert_rate"] for v in vs])
                    }
                    for g in ("all", DECISION_SUBGROUP):
                        for metric in ("precision", "recall"):
                            entry[f"{g}_{metric}"] = _stats(
                                [v["budgets"][mode][k]["subgroups"][g][metric] for v in vs]
                            )
                    budgets[mode][k] = entry
            models.setdefault(w, {}).setdefault(policy, {})[name] = {
                "seeds": seeds,
                "rounds": _stats([v["rounds"] for v in vs]),
                "low_round_runs": int(sum(v["low_rounds"] for v in vs)),
                "threshold": _stats([v["threshold"] for v in vs]),
                "average_precision": {
                    g: _stats([v["subgroups"][g]["ranking"]["average_precision"] for v in vs])
                    for g in vs[0]["subgroups"]
                },
                "budgets": budgets,
            }
        per_group = {}
        for g in DELTA_SUBGROUPS:
            ds = [r["deltas_restricted_minus_baseline"][g] for r in rs]
            per_group[g] = {
                "runs_evaluated": int(sum(d["evaluated"] for d in ds)),
                "point": _stats([d.get("point") for d in ds]),
                "positive_runs": int(sum((d.get("point") or 0) > 0 for d in ds)),
                "median_ci95_low": _median([d.get("ci95_low") for d in ds]),
                "median_ci95_high": _median([d.get("ci95_high") for d in ds]),
            }
        deltas.setdefault(w, {})[policy] = per_group
    return {"models": models, "deltas_restricted_minus_baseline": deltas}


# --------------------------------------------------------------------------- decision rule


def decide_unseen_uid_consistency(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the pre-registered rule to per-(window, seed) uid_unseen deltas.

    ``entries`` items: ``{"window", "seed", "policy", "point", "ci95_low"}``; ``point`` and
    ``ci95_low`` may be ``None`` when the subgroup was not evaluable. Consistent only if:

    1. ``point > 0`` in at least ``MIN_POSITIVE_RUNS`` (12) of the 15 required runs;
    2. the 5-seed mean ``point`` is > 0 in all three windows;
    3. at least ``MIN_WINDOWS_CI_ABOVE_ZERO`` (2) windows have a median per-seed 95% CI lower
       bound > 0 (the interval excludes 0 on the side of a restricted-model advantage).

    Missing or non-evaluable values count as not positive. A verdict needs every required
    (window, seed) run under ``DECISION_POLICY``; otherwise the result is ``insufficient_runs``.
    """
    required = {(w, s) for w in WINDOW_NAMES for s in SEEDS_MAIN}
    found = {
        (e["window"], e["seed"]): e
        for e in entries
        if e["policy"] == DECISION_POLICY and (e["window"], e["seed"]) in required
    }
    base = {
        "subgroup": DECISION_SUBGROUP,
        "policy": DECISION_POLICY,
        "runs_required": len(required),
        "runs_found": len(found),
        "rule": {
            "min_positive_runs": MIN_POSITIVE_RUNS,
            "all_window_means_positive": True,
            "min_windows_median_ci95_low_above_zero": MIN_WINDOWS_CI_ABOVE_ZERO,
        },
    }
    if len(found) < len(required):
        return {**base, "verdict": "insufficient_runs"}

    points = {k: e["point"] for k, e in found.items()}
    positive_runs = sum(p is not None and p > 0 for p in points.values())
    window_means: dict[str, float | None] = {}
    window_ci_ok: dict[str, bool] = {}
    for w in WINDOW_NAMES:
        ps = [points[(w, s)] for s in SEEDS_MAIN]
        window_means[w] = None if any(p is None for p in ps) else float(np.mean(ps))
        lows = [found[(w, s)]["ci95_low"] for s in SEEDS_MAIN]
        window_ci_ok[w] = all(x is not None for x in lows) and float(np.median(lows)) > 0
    c1 = positive_runs >= MIN_POSITIVE_RUNS
    c2 = all(m is not None and m > 0 for m in window_means.values())
    c3 = sum(window_ci_ok.values()) >= MIN_WINDOWS_CI_ABOVE_ZERO
    return {
        **base,
        "positive_runs": positive_runs,
        "window_mean_point": window_means,
        "window_median_ci95_low_above_zero": window_ci_ok,
        "criteria_met": {
            "positive_runs": c1,
            "all_window_means_positive": c2,
            "windows_ci_above_zero": c3,
        },
        "verdict": "consistent" if c1 and c2 and c3 else "not_consistent",
    }


def decision_entries(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in runs:
        d = r["deltas_restricted_minus_baseline"][DECISION_SUBGROUP]
        out.append(
            {
                "window": r["window"],
                "seed": r["seed"],
                "policy": r["policy"],
                "point": d.get("point"),
                "ci95_low": d.get("ci95_low"),
            }
        )
    return out


# --------------------------------------------------------------------------- checks and gates


def load_regression_reference(path: Path = REGRESSION_REFERENCE) -> dict[str, Any]:
    """Expected W3 / seed-42 values from the committed Milestone 2.1 evaluation report.

    Refuses a report produced with a different seed or early-stopping patience, so the gate can
    never silently compare against the wrong run.
    """
    report = json.loads(path.read_text(encoding="utf-8"))
    model = report["config"]["model"]
    if model["seed"] != REGRESSION_TARGET["seed"]:
        raise ValueError(f"reference seed {model['seed']} != {REGRESSION_TARGET['seed']}")
    if model["early_stopping_rounds"] != ComparisonConfig().early_stopping_rounds:
        raise ValueError("reference early-stopping patience differs from early_stop_100")
    variants = report["gaps"][REGRESSION_REFERENCE_GAP]["variants"]
    return {
        "source": path.name,  # file name only: no local paths in reports
        "digest": report["validation"]["id_digest"],
        "average_precision": {
            n: float(variants[n]["subgroups"]["all"]["ranking"]["average_precision"])
            for n in (BASELINE, RESTRICTED)
        },
    }


def regression_check(
    runs: list[dict[str, Any]],
    reference: dict[str, Any] | None = None,
    tolerance: float = REGRESSION_TOLERANCE,
) -> dict[str, Any]:
    """W3 / seed 42 / early_stop_100 must reproduce the Milestone 2.1 gap-0 results.

    Passes only if, for each variant, ``|observed AP - expected AP| <= tolerance`` (unrounded)
    and the validation digest matches. Fails closed: an unavailable reference is reported as
    not evaluated with ``passed=False``.
    """
    target = tuple(REGRESSION_TARGET[k] for k in ("window", "seed", "policy"))
    match = [r for r in runs if (r["window"], r["seed"], r["policy"]) == target]
    if not match:
        return {"evaluated": False, "passed": False, "reason": "target run not in this grid"}
    if reference is None:
        try:
            reference = load_regression_reference()
        except (OSError, KeyError, ValueError) as exc:
            # OSError text embeds the absolute local path; report only the file name.
            detail = REGRESSION_REFERENCE.name if isinstance(exc, OSError) else str(exc)
            return {
                "evaluated": False,
                "passed": False,
                "reason": f"reference unavailable: {type(exc).__name__}: {detail}",
            }
    r = match[0]
    observed: dict[str, float | None] = {
        n: r["variants"][n]["subgroups"]["all"]["ranking"]["average_precision"]
        for n in (BASELINE, RESTRICTED)
    }
    abs_diff = {
        n: None if observed[n] is None else abs(observed[n] - reference["average_precision"][n])
        for n in observed
    }
    ap_ok = all(d is not None and d <= tolerance for d in abs_diff.values())
    digest_ok = r["valid_digest"] == reference["digest"]
    return {
        "evaluated": True,
        "passed": bool(ap_ok and digest_ok),
        "comparison": "absolute difference of unrounded average precision",
        "tolerance": tolerance,
        "reference_source": reference["source"],
        "expected": {
            "average_precision": reference["average_precision"],
            "digest": reference["digest"],
        },
        "observed": {"average_precision": observed, "digest": r["valid_digest"]},
        "abs_diff": abs_diff,
        "checks": {"average_precision_within_tolerance": ap_ok, "digest_match": digest_ok},
    }


def gates(runs: list[dict[str, Any]], full_pairs: int) -> dict[str, Any]:
    """Stop-condition gates: peak memory and the projected full-grid runtime."""
    peaks = [r["peak_memory_mb"] for r in runs if r["peak_memory_mb"] is not None]
    peak = max(peaks) if peaks else None
    per_pair = float(np.mean([r["pair_seconds"] for r in runs])) if runs else None
    projected = per_pair * full_pairs if per_pair is not None else None
    return {
        "peak_memory_mb": peak,
        "max_peak_memory_mb": MAX_PEAK_MEMORY_MB,
        "memory_ok": None if peak is None else peak <= MAX_PEAK_MEMORY_MB,
        "mean_seconds_per_pair": per_pair,
        "full_grid_pairs": full_pairs,
        "projected_full_grid_seconds": projected,
        "max_projected_seconds": MAX_PROJECTED_SECONDS,
        "runtime_ok": None if projected is None else projected <= MAX_PROJECTED_SECONDS,
        "projection_note": (
            "mean wall time per evaluated pair x full-grid pairs; the pilot mixes W1 (smaller "
            "training set) and W3 (largest), so the projection is approximate"
        ),
    }


def collect_warnings(
    runs: list[dict[str, Any]], settings: StudySettings, decision: dict[str, Any]
) -> list[str]:
    warnings: list[str] = []
    small: set[tuple[str, str]] = set()
    for r in runs:
        for g, rows in r["subgroup_rows"].items():
            pos = r["variants"][BASELINE]["subgroups"][g]["positives"]
            if rows and pos < settings.min_positives and (r["window"], g) not in small:
                small.add((r["window"], g))
                warnings.append(
                    f"{r['window']}: subgroup {g} has {pos} frauds (< {settings.min_positives}); "
                    "its metrics are unreliable"
                )
        for name, v in r["variants"].items():
            if v["low_rounds"]:
                warnings.append(
                    f"{r['window']} seed {r['seed']} {r['policy']} {name}: selected only "
                    f"{v['rounds']} rounds (< {LOW_ROUNDS}); treat this model as unstable"
                )
    if decision["verdict"] == "insufficient_runs":
        warnings.append(
            f"decision rule not applied: {decision['runs_found']} of "
            f"{decision['runs_required']} required runs present"
        )
    return warnings


# --------------------------------------------------------------------------- runner


def _clean(o: Any) -> Any:
    """JSON-safe copy: numpy scalars to Python, non-finite floats to None."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_clean(v) for v in o]
    if isinstance(o, np.generic):
        o = o.item()
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def run_grid(
    df: pd.DataFrame,
    specs: list[RunSpec],
    settings: StudySettings | None = None,
    *,
    mode: str = "custom",
) -> dict[str, Any]:
    settings = settings or StudySettings()
    started = time.perf_counter()
    hist = HistoryConfig()
    frame = build_model_frame(df, "baseline", hist)  # point-in-time history, no labels
    keys = build_entity_keys(df)
    windows = {
        w.name: w
        for w in define_windows(
            df[TIME], valid_fraction=settings.model.valid_fraction, window_days=settings.window_days
        )
    }
    unknown = {s.window for s in specs} - set(windows)
    if unknown:
        raise ValueError(f"unknown windows in specs: {sorted(unknown)}")
    variants = study_variants(hist)

    window_info = []
    for w in windows.values():
        train, valid = window_split(frame, w, settings.gap_seconds)
        window_info.append(
            {
                "name": w.name,
                "start": w.start,
                "end": w.end,
                "train_rows": len(train),
                "valid_rows": len(valid),
                "valid_frauds": int(valid[LABEL].sum()),
                "valid_fraud_rate": float(valid[LABEL].mean()),
                "valid_digest": _id_digest(valid[KEY]),
            }
        )

    runs = []
    for spec in specs:
        run = evaluate_pair(frame, keys, windows[spec.window], spec, settings, variants)
        runs.append(run)
        logger.info(
            "pair window=%s seed=%d policy=%s ap_baseline=%.4f ap_restricted=%.4f "
            "rounds=%d/%d seconds=%.0f peak_mb=%s",
            spec.window,
            spec.seed,
            spec.policy,
            run["variants"][BASELINE]["subgroups"]["all"]["ranking"]["average_precision"],
            run["variants"][RESTRICTED]["subgroups"]["all"]["ranking"]["average_precision"],
            run["variants"][BASELINE]["rounds"],
            run["variants"][RESTRICTED]["rounds"],
            run["pair_seconds"],
            run["peak_memory_mb"],
        )

    decision = decide_unseen_uid_consistency(decision_entries(runs))
    report = {
        "dataset": "IEEE-CIS train (labelled file only)",
        "mode": mode,
        "gap_statement": GAP_STATEMENT,
        "settings": {
            **asdict(settings),
            "history_windows": dict(hist.windows),
            "identifier_inputs_removed": list(IDENTIFIER_INPUTS),
            "policies": {
                "early_stop_100": "production rule (patience = model.early_stopping_rounds)",
                "floor_50": f"best round >= {FLOOR_ROUNDS}, same patience",
                "fixed_300": f"exactly {FIXED_ROUNDS} rounds",
            },
        },
        "subgroup_definitions": SUBGROUP_DEFINITIONS,
        "windows": window_info,
        "specs": [asdict(s) for s in specs],
        "runs": runs,
        "aggregates": aggregate(runs),
        "decision_rule": decision,
        "regression_check": regression_check(runs),
        "gates": gates(runs, len(main_grid()) + len(sensitivity_grid())),
        "warnings": collect_warnings(runs, settings, decision),
        "execution": {
            "seconds_total": round(time.perf_counter() - started, 1),
            "peak_memory_mb": peak_memory_mb(),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "xgboost": xgb.__version__,
        },
    }
    cleaned: dict[str, Any] = _clean(report)
    return cleaned


# --------------------------------------------------------------------------- rendering


def _f(x: float | None, nd: int = 4) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.2f}%"


def _ms(st: dict[str, Any], nd: int = 4) -> str:
    """mean ± sd [min, max] (n)."""
    if not st["n"]:
        return "—"
    sd = "" if st["std"] is None else f" ± {st['std']:.{nd}f}"
    return f"{st['mean']:.{nd}f}{sd} [{st['min']:.{nd}f}, {st['max']:.{nd}f}] (n={st['n']})"


def render_markdown(r: dict[str, Any]) -> str:
    agg = r["aggregates"]
    lines = [
        f"# Robust evaluation ({r['mode']}): windows x seeds x early-stopping policies",
        "",
        "Generated by `python -m src.models.robust_eval`; values in the matching JSON file. "
        "Aggregates only. Experimental — the serving model is unchanged.",
        "",
        f"{r['gap_statement']}",
        "",
        "## Validation windows",
        "",
        "| Window | Start (DT) | End (DT) | Train rows | Valid rows | Frauds | Fraud rate "
        "| Digest |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for w in r["windows"]:
        end = "end of data" if w["end"] is None else f"{w['end']:,}"
        lines.append(
            f"| {w['name']} | {w['start']:,} | {end} | {w['train_rows']:,} | {w['valid_rows']:,} "
            f"| {w['valid_frauds']:,} | {_pct(w['valid_fraud_rate'])} | `{w['valid_digest']}` |"
        )

    rc, g = r["regression_check"], r["gates"]
    lines += ["", "## Checks and gates", ""]
    if rc["evaluated"]:
        lines.append(
            f"- Regression (W3, seed 42, early_stop_100): **{'PASS' if rc['passed'] else 'FAIL'}** "
            f"— unrounded AP must match `{rc['reference_source']}` within an absolute tolerance "
            f"of {rc['tolerance']:g}; digest must match exactly."
        )
        for n, obs in rc["observed"]["average_precision"].items():
            diff = rc["abs_diff"][n]
            lines.append(
                f"  - {n}: observed {obs!r}, expected {rc['expected']['average_precision'][n]!r}, "
                f"|Δ| = {'—' if diff is None else f'{diff:.3g}'}"
                f" → {'OK' if diff is not None and diff <= rc['tolerance'] else 'OUT OF TOLERANCE'}"
            )
        lines.append(
            f"  - digest: observed `{rc['observed']['digest']}`, expected "
            f"`{rc['expected']['digest']}` → {'OK' if rc['checks']['digest_match'] else 'MISMATCH'}"
        )
    else:
        lines.append(
            f"- Regression check: **NOT EVALUATED** ({rc.get('reason', 'unknown reason')}); "
            "treat as not passed."
        )
    lines += [
        f"- Peak memory: {_f(g['peak_memory_mb'], 0)} MB (limit {g['max_peak_memory_mb']:,.0f} MB) "
        f"→ {'OK' if g['memory_ok'] else 'EXCEEDED' if g['memory_ok'] is False else '—'}.",
        f"- Mean seconds per pair: {_f(g['mean_seconds_per_pair'], 1)}; projected full grid "
        f"({g['full_grid_pairs']} pairs): {_f(g['projected_full_grid_seconds'], 0)} s (limit "
        f"{g['max_projected_seconds']:,.0f} s) → "
        f"{'OK' if g['runtime_ok'] else 'EXCEEDED' if g['runtime_ok'] is False else '—'}. "
        f"{g['projection_note']}.",
    ]

    d = r["decision_rule"]
    lines += ["", "## Decision rule: unseen-uid advantage of the restricted model", ""]
    lines.append(
        f"Verdict: **{d['verdict']}** ({d['runs_found']}/{d['runs_required']} required runs, "
        f"policy {d['policy']}, subgroup {d['subgroup']})."
    )
    if "criteria_met" in d:
        lines.append(
            f"Positive runs {d['positive_runs']} (need ≥ {d['rule']['min_positive_runs']}); "
            f"window means {d['window_mean_point']}; windows with median CI low > 0: "
            f"{d['window_median_ci95_low_above_zero']}; criteria met {d['criteria_met']}."
        )

    lines += [
        "",
        "## PR-AUC across seeds (mean ± sd [min, max])",
        "",
        "| Window | Policy | Variant | All rows | uid seen | uid unseen | card unseen | Rounds "
        "| Low-round runs |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for w, pols in agg["models"].items():
        for policy, variants in pols.items():
            for name, m in variants.items():
                ap = m["average_precision"]
                lines.append(
                    f"| {w} | {policy} | {name} | {_ms(ap['all'])} | {_ms(ap['uid_seen'])} | "
                    f"{_ms(ap['uid_unseen'])} | {_ms(ap['card_unseen'])} | "
                    f"{_ms(m['rounds'], 0)} | {m['low_round_runs']} |"
                )

    lines += [
        "",
        "## Paired PR-AUC change (restricted − baseline)",
        "",
        "| Window | Policy | Subgroup | Runs | Mean ± sd [min, max] | Positive runs "
        "| Median CI95 |",
        "|---|---|---|---|---|---|---|",
    ]
    for w, pols in agg["deltas_restricted_minus_baseline"].items():
        for policy, subs in pols.items():
            for sub, x in subs.items():
                ci = (
                    "—"
                    if x["median_ci95_low"] is None
                    else f"[{x['median_ci95_low']:+.4f}, {x['median_ci95_high']:+.4f}]"
                )
                lines.append(
                    f"| {w} | {policy} | {sub} | {x['runs_evaluated']} | {_ms(x['point'])} | "
                    f"{x['positive_runs']} | {ci} |"
                )

    lines += [
        "",
        "## Alert budgets (early_stop_100, all rows; seed means)",
        "",
        "Mode A: top-k% of the window's scores (label-free, uses the window's score "
        "distribution). Mode B: threshold = training-holdout score quantile (realised alert "
        "rate may differ).",
        "",
        "| Window | Variant | Mode | Budget | Realised alert rate | Precision | Recall "
        "| uid-unseen recall |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for w, pols in agg["models"].items():
        for name, m in pols.get(DECISION_POLICY, {}).items():
            for mode, label in (("mode_a_top_k", "A"), ("mode_b_holdout_threshold", "B")):
                for k, b in m["budgets"][mode].items():
                    lines.append(
                        f"| {w} | {name} | {label} | {100 * float(k):g}% | "
                        f"{_pct(b['alert_rate']['mean'])} | {_f(b['all_precision']['mean'])} | "
                        f"{_f(b['all_recall']['mean'])} | "
                        f"{_f(b[f'{DECISION_SUBGROUP}_recall']['mean'])} |"
                    )

    lines += [
        "",
        "## Early-stopping diagnostics (stage-1 training holdout)",
        "",
        "| Window | Seed | Policy | Variant | Selected rounds | Best round | Evaluated "
        "| AUCPR@50 | Rounds within 0.005 of best |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in r["runs"]:
        for name, v in run["variants"].items():
            c = v["curve"]
            flag = " ⚠" if v["low_rounds"] else ""
            lines.append(
                f"| {run['window']} | {run['seed']} | {run['policy']} | {name} | "
                f"{c['selected_rounds']}{flag} | {c['best_round']} | {c['rounds_evaluated']} | "
                f"{_f(c['aucpr_at']['50'])} | {c['rounds_within_tolerance_of_best']} |"
            )

    lines += ["", "## Warnings", ""]
    lines += [f"- {w}" for w in r["warnings"]] or ["- none"]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI


def output_paths(out_dir: Path, mode: str) -> tuple[Path, Path]:
    stem = "robust_evaluation_pilot" if mode == "pilot" else "robust_evaluation"
    return out_dir / f"{stem}.json", out_dir / f"{stem}.md"


RUN_DETAIL = ("runs",)  # per-pair results: in the JSON artifact, not ~30,000 MLflow metrics


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Milestone 2.2 robust evaluation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true", help="3 pairs (6 model runs)")
    group.add_argument("--full", action="store_true", help="27 pairs (54 model runs)")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    parser.add_argument(
        "--overwrite", action="store_true", help="replace existing reports for this mode"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    mode = "pilot" if args.pilot else "full"
    json_path, md_path = output_paths(args.out_dir, mode)
    # Checked before loading data: a full run takes about an hour, and its registered results
    # must not be replaced by accident.
    existing = [p.name for p in (json_path, md_path) if p.exists()]
    if existing and not args.overwrite:
        parser.error(f"refusing to overwrite existing {existing}; pass --overwrite to replace")
    specs = pilot_grid() if args.pilot else main_grid() + sensitivity_grid()
    report = run_grid(load_ieee_cis(args.raw_dir, usecols=[]), specs, mode=mode)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("wrote %s", json_path)
    from src.models.tracking import flatten_metrics, log_report_run  # tracking imports us

    log_report_run(
        f"robust-evaluation-{mode}",
        [json_path, md_path],
        metrics=flatten_metrics(report, exclude=RUN_DETAIL),
        tags={"module": "src.models.robust_eval", "mode": mode},
    )
    return report


if __name__ == "__main__":
    main()
