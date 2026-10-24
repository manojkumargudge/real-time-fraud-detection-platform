"""Pure decision rules: when drift fires, and when a challenger may become ``champion``.

The ``production`` alias is outside both rules: nothing in the drift job moves it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from src.monitoring.config import DriftConfig


@dataclass(frozen=True)
class CheckResults:
    drifted_feature_share: float
    prediction_drift_detected: bool
    prediction_drift_score: float
    delayed_pr_auc: float | None  # None: not enough matured labels (check not available)
    champion_valid_pr_auc: float
    # The champion's top features by XGBoost gain, and those of them flagged as drifted (same
    # per-feature test and threshold as the share rule).
    top_gain_features: tuple[str, ...] = ()
    drifted_top_gain_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerDecision:
    fired: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_trigger(checks: CheckResults, cfg: DriftConfig) -> TriggerDecision:
    """Fires if any one condition holds (each is reported, including the ones that did not)."""
    reasons: list[str] = []
    share_limit = cfg.feature_drift.share_threshold
    if checks.drifted_feature_share > share_limit:
        reasons.append(
            f"feature drift: {checks.drifted_feature_share:.3f} of features drifted > {share_limit}"
        )
    if checks.drifted_top_gain_features:
        reasons.append(
            f"top-feature drift: {', '.join(checks.drifted_top_gain_features)} drifted (among the "
            f"champion's top {len(checks.top_gain_features)} features by gain)"
        )
    if checks.prediction_drift_detected:
        reasons.append(
            f"prediction drift: {cfg.prediction_drift.method} {checks.prediction_drift_score:.4f}"
            f" >= {cfg.prediction_drift.threshold}"
        )
    if checks.delayed_pr_auc is not None:
        floor = checks.champion_valid_pr_auc - cfg.performance.max_drop
        if checks.delayed_pr_auc < floor:
            reasons.append(
                f"performance: delayed-label PR-AUC {checks.delayed_pr_auc:.4f} < champion "
                f"validation {checks.champion_valid_pr_auc:.4f} - {cfg.performance.max_drop}"
            )
    return TriggerDecision(fired=bool(reasons), reasons=reasons)


@dataclass(frozen=True)
class PromotionDecision:
    promote: bool
    reason: str
    challenger_pr_auc: float
    champion_pr_auc: float
    delta_mean: float
    ci95_low: float
    ci95_high: float
    margin: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_promotion(
    *,
    challenger_pr_auc: float,
    champion_pr_auc: float,
    bootstrap: dict[str, float | int],
    margin: float,
) -> PromotionDecision:
    """Promote only if the lower 95% bound of PR-AUC(challenger) - PR-AUC(champion) > margin."""
    low, high = float(bootstrap["ci95_low"]), float(bootstrap["ci95_high"])
    delta = float(bootstrap["delta_mean"])
    common = {
        "challenger_pr_auc": challenger_pr_auc,
        "champion_pr_auc": champion_pr_auc,
        "delta_mean": delta,
        "ci95_low": low,
        "ci95_high": high,
        "margin": margin,
    }
    if math.isnan(low):
        return PromotionDecision(
            False, "bootstrap had no usable resamples; champion kept", **common
        )
    if low > margin:
        reason = f"promoted: CI lower bound {low:+.4f} > margin {margin}"
        return PromotionDecision(True, reason, **common)
    if challenger_pr_auc <= champion_pr_auc:
        reason = (
            f"kept champion: challenger PR-AUC {challenger_pr_auc:.4f} <= champion "
            f"{champion_pr_auc:.4f}"
        )
    else:
        reason = (
            f"kept champion: challenger better by {challenger_pr_auc - champion_pr_auc:+.4f} but "
            f"CI lower bound {low:+.4f} <= margin {margin}"
        )
    return PromotionDecision(False, reason, **common)
