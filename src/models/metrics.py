"""Imbalanced-classification metrics in NumPy (no scikit-learn dependency).

``average_precision`` follows the scikit-learn definition: ``sum_k (R_k - R_{k-1}) * P_k`` over
distinct score thresholds, so tied scores are treated as one threshold.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from src.array_types import BoolArray, FloatArray, IntArray, NumArray


def _check(y: NumArray, scores: NumArray) -> tuple[IntArray, FloatArray]:
    labels = np.asarray(y)
    s = np.asarray(scores, dtype=np.float64)
    if labels.shape != s.shape or labels.ndim != 1:
        raise ValueError("labels and scores must be 1-D arrays of equal length")
    if not np.isin(labels, (0, 1)).all():
        raise ValueError("labels must be 0/1")
    if np.isnan(s).any():
        raise ValueError("scores must not contain NaN")
    if labels.sum() == 0:
        raise ValueError("at least one positive label is required")
    return labels.astype(np.int64), s


def _curve(y: IntArray, scores: FloatArray) -> tuple[IntArray, IntArray, FloatArray]:
    """Cumulative TP/FP at each distinct threshold (descending) and those thresholds."""
    order = np.argsort(-scores, kind="mergesort")
    s, ys = scores[order], y[order]
    last_of_tie = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]
    tp = np.cumsum(ys)[last_of_tie]
    fp = (last_of_tie + 1) - tp
    return tp, fp, s[last_of_tie]


def average_precision(y: NumArray, scores: NumArray) -> float:
    y, scores = _check(y, scores)
    tp, fp, _ = _curve(y, scores)
    precision = tp / (tp + fp)
    recall = tp / y.sum()
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def roc_auc(y: NumArray, scores: NumArray) -> float:
    """Mann-Whitney ROC-AUC; tied scores count half. Reported for reference only: with ~3.5%
    positives it is dominated by easy negatives, so PR-AUC drives model decisions."""
    y, scores = _check(y, scores)
    pos = int(y.sum())
    neg = len(y) - pos
    if neg == 0:
        raise ValueError("at least one negative label is required")
    _, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    ranks = ((ends - counts + 1 + ends) / 2.0)[inverse]  # average 1-based rank per tie group
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def select_threshold_max_f1(y: NumArray, scores: NumArray) -> float:
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


def threshold_metrics(y: NumArray, scores: NumArray, threshold: float) -> ThresholdMetrics:
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
    y: NumArray,
    scores_a: NumArray,
    scores_b: NumArray,
    day: NumArray,
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
    y: NumArray, scores: NumArray, day: NumArray, *, resamples: int, seed: int
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


def budget_size(n: int, rate: float) -> int:
    """Alerts allowed for ``n`` rows at ``rate``: ``ceil(rate * n)``, robust to float artefacts."""
    if not 0 < rate <= 1:
        raise ValueError("rate must be in (0, 1]")
    # round() guards against float artefacts such as 0.07 * 100 = 7.000000000000001.
    return int(np.ceil(round(rate * n, 9))) if n else 0


def budget_alert_mask(scores: NumArray, rate: float) -> BoolArray:
    """Boolean mask of the top ``budget_size`` scores; ties broken by input order (stable)."""
    s = np.asarray(scores, dtype=np.float64)
    mask = np.zeros(len(s), dtype=bool)
    mask[np.argsort(-s, kind="stable")[: budget_size(len(s), rate)]] = True
    return mask


def alert_budget_metrics(
    y: NumArray, scores: NumArray, rates: tuple[float, ...] | list[float]
) -> list[dict[str, float | int | None]]:
    """Precision@k / recall@k when alerting on the top ``ceil(rate * n)`` scores.

    The alert set depends only on ``scores`` (labels are read afterwards to count hits). Ties are
    broken deterministically by input order (stable sort); ``boundary_ties`` counts rows sharing
    the cut-off score and ``tied_excluded`` how many of those fell outside the budget. Empty
    inputs and groups without positives are allowed (undefined metrics are ``None``).
    """
    y = np.asarray(y)
    s = np.asarray(scores, dtype=np.float64)
    if y.shape != s.shape or y.ndim != 1:
        raise ValueError("labels and scores must be 1-D arrays of equal length")
    if len(y) and not np.isin(y, (0, 1)).all():
        raise ValueError("labels must be 0/1")
    if np.isnan(s).any():
        raise ValueError("scores must not contain NaN")
    if not rates or any(not 0 < k <= 1 for k in rates):
        raise ValueError("rates must be non-empty and each in (0, 1]")

    n, pos = len(y), int(y.sum())
    out: list[dict[str, float | int | None]] = []
    for k in rates:
        mask = budget_alert_mask(s, k)
        m = int(mask.sum())
        row: dict[str, float | int | None] = {"rate": float(k), "rows": n, "positives": pos}
        if m == 0:
            row.update(
                alerts=0,
                tp=0,
                fp=0,
                precision=None,
                recall=None,
                cutoff_score=None,
                boundary_ties=0,
                tied_excluded=0,
            )
            out.append(row)
            continue
        chosen = s[mask]
        cutoff = chosen.min()
        tp = int(y[mask].sum())
        ties = int((s == cutoff).sum())
        row.update(
            alerts=m,
            tp=tp,
            fp=m - tp,
            precision=tp / m,
            recall=tp / pos if pos else None,
            cutoff_score=float(cutoff),
            boundary_ties=ties,
            tied_excluded=ties - int((chosen == cutoff).sum()),
        )
        out.append(row)
    return out
