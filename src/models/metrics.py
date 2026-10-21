"""Imbalanced-classification metrics in NumPy (no scikit-learn dependency).

``average_precision`` follows the scikit-learn definition: ``sum_k (R_k - R_{k-1}) * P_k`` over
distinct score thresholds, so tied scores are treated as one threshold.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


def _check(y: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y)
    scores = np.asarray(scores, dtype=np.float64)
    if y.shape != scores.shape or y.ndim != 1:
        raise ValueError("labels and scores must be 1-D arrays of equal length")
    if not np.isin(y, (0, 1)).all():
        raise ValueError("labels must be 0/1")
    if np.isnan(scores).any():
        raise ValueError("scores must not contain NaN")
    if y.sum() == 0:
        raise ValueError("at least one positive label is required")
    return y.astype(np.int64), scores


def _curve(y: np.ndarray, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cumulative TP/FP at each distinct threshold (descending) and those thresholds."""
    order = np.argsort(-scores, kind="mergesort")
    s, ys = scores[order], y[order]
    last_of_tie = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]
    tp = np.cumsum(ys)[last_of_tie]
    fp = (last_of_tie + 1) - tp
    return tp, fp, s[last_of_tie]


def average_precision(y: np.ndarray, scores: np.ndarray) -> float:
    y, scores = _check(y, scores)
    tp, fp, _ = _curve(y, scores)
    precision = tp / (tp + fp)
    recall = tp / y.sum()
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def select_threshold_max_f1(y: np.ndarray, scores: np.ndarray) -> float:
    """Score threshold maximising F1 for the rule ``score >= threshold``.

    Ties in F1 resolve to the highest threshold (fewest alerts).
    """
    y, scores = _check(y, scores)
    tp, fp, thresholds = _curve(y, scores)
    f1 = 2 * tp / (tp + fp + y.sum())
    return float(thresholds[int(np.argmax(f1))])  # argmax returns the first = highest threshold


@dataclass(frozen=True)
class ThresholdMetrics:
    threshold: float
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float
    recall: float
    f1: float
    flagged: int
    flagged_rate: float
    prevalence: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)


def threshold_metrics(y: np.ndarray, scores: np.ndarray, threshold: float) -> ThresholdMetrics:
    y, scores = _check(y, scores)
    pred = scores >= threshold
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    tn = int((~pred & (y == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ThresholdMetrics(
        threshold=float(threshold),
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        flagged=int(pred.sum()),
        flagged_rate=float(pred.mean()),
        prevalence=float(y.mean()),
    )


def day_block_bootstrap_ap_delta(
    y: np.ndarray,
    scores_a: np.ndarray,
    scores_b: np.ndarray,
    day: np.ndarray,
    *,
    resamples: int,
    seed: int,
) -> dict[str, float | int]:
    """Paired bootstrap of AP(b) - AP(a), resampling whole days to respect time correlation."""
    days, inverse = np.unique(day, return_inverse=True)
    members = [np.flatnonzero(inverse == i) for i in range(len(days))]
    rng = np.random.default_rng(seed)
    deltas = []
    for _ in range(resamples):
        idx = np.concatenate([members[i] for i in rng.integers(0, len(days), len(days))])
        if y[idx].sum() == 0:
            continue
        ap_a = average_precision(y[idx], scores_a[idx])
        deltas.append(average_precision(y[idx], scores_b[idx]) - ap_a)
    d = np.asarray(deltas)
    if not len(d):  # resamples=0, or every resample lacked a positive
        nan = float("nan")
        return {
            "resamples": 0,
            "days": len(days),
            "delta_mean": nan,
            "ci95_low": nan,
            "ci95_high": nan,
            "share_b_better": nan,
        }
    return {
        "resamples": len(d),
        "days": len(days),
        "delta_mean": float(d.mean()),
        "ci95_low": float(np.quantile(d, 0.025)),
        "ci95_high": float(np.quantile(d, 0.975)),
        "share_b_better": float((d > 0).mean()),
    }


def day_block_bootstrap_ap(
    y: np.ndarray, scores: np.ndarray, day: np.ndarray, *, resamples: int, seed: int
) -> dict[str, float | int]:
    """95% interval for AP, resampling whole days. Resamples without positives are skipped."""
    days, inverse = np.unique(day, return_inverse=True)
    members = [np.flatnonzero(inverse == i) for i in range(len(days))]
    rng = np.random.default_rng(seed)
    aps = []
    for _ in range(resamples):
        idx = np.concatenate([members[i] for i in rng.integers(0, len(days), len(days))])
        if y[idx].sum() == 0:
            continue
        aps.append(average_precision(y[idx], scores[idx]))
    a = np.asarray(aps)
    return {
        "resamples": len(a),
        "days": len(days),
        "ci95_low": float(np.quantile(a, 0.025)) if len(a) else float("nan"),
        "ci95_high": float(np.quantile(a, 0.975)) if len(a) else float("nan"),
    }
