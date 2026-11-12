"""Drift-trigger calibration on drift-free windows (M4; measurement only, changes no rule).

Usage (repo root, IEEE-CIS data present; reads the served bundle for the recipe, writes nothing
to any database or MLflow)::

    python -m scripts.drift_calibration --out docs/evidence/<date>-drift-calibration

Question: how often do the frozen drift rules (``configs/drift.toml``, incl. the 1,000-row
floor) fire on windows where nothing was injected and the model is not stale?

Windows (fixed in code before any run, ``plan_windows``): every non-overlapping 7-day window
inside the champion's training period whose start ``s`` leaves 30 days of training rows for the
earliest model plus the 30-day reference window, i.e. ``s = train_start + 60 days, +7 days,
...`` while the window ends inside the training period. For each window, exactly as the drift
job does for production (``src/monitoring/run_drift.py``), but rolled back in time:

* reference = the 30 days before ``s``; its scores come from the v2 recipe refit on the rows
  before the reference (model A, out-of-sample), like ``reference_scores``;
* current = the window; its scores come from the v2 recipe refit on all rows before ``s``
  (model B, the window's "champion": out-of-sample, at most 7 days old);
* top-5-by-gain features are read from model B's booster;
* delayed labels: ``now`` = last transaction in the window, cutoff = now - 30 days, labels in
  (cutoff - 7 days, cutoff]; those rows precede ``s - 23 days``, so they are scored with model A
  (out-of-sample) and compared with model A's own holdout PR-AUC (the job compares with the
  champion's validation PR-AUC);
* the same ``run_drift`` / ``evaluate_trigger`` / ``insufficient_data`` code and thresholds.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.history import TIME, HistoryConfig
from src.features.preprocess import DEFAULT_CATEGORICAL
from src.models.compare import ComparisonConfig, build_model_frame, score, train_variant
from src.models.metrics import average_precision
from src.monitoring.config import DEFAULT_CONFIG, DriftConfig, load_config
from src.monitoring.drift import SCORE, run_drift
from src.monitoring.rules import CheckResults, evaluate_trigger
from src.monitoring.run_drift import SERVED_BUNDLE, champion_train_window, insufficient_data
from src.monitoring.windows import Window, days
from src.serving.model import ModelBundle

logger = logging.getLogger("scripts.drift_calibration")

WINDOW_DAYS = 7
MIN_TRAIN_DAYS = 30  # training rows for the earliest model A, before its reference window


@dataclass(frozen=True)
class WindowResult:
    start_day: float
    end_day: float  # exclusive
    current_rows: int
    reference_rows: int
    sufficient: bool
    drifted_feature_share: float
    drifted_features: list[str]
    prediction_psi: float
    prediction_drift_detected: bool
    top_gain_features: list[str]
    drifted_top_gain_features: list[str]
    delayed_rows: int
    delayed_positives: int
    delayed_pr_auc: float | None
    comparator_pr_auc: float
    fired: bool
    reasons: list[str]
    seconds: float


def plan_windows(train: Window, reference_days: int) -> list[Window]:
    """Non-overlapping 7-day windows inside ``train`` that start after MIN_TRAIN_DAYS of
    training plus the reference window. Fixed before any run; no data is looked at."""
    first = train.start + days(MIN_TRAIN_DAYS + reference_days)
    out: list[Window] = []
    s = first
    while s + days(WINDOW_DAYS) - 1 <= train.end:
        out.append(Window(f"cal-{len(out) + 1}", s, s + days(WINDOW_DAYS) - 1))
        s += days(WINDOW_DAYS)
    return out


def evaluate_window(
    frame: pd.DataFrame,
    window: Window,
    cfg: DriftConfig,
    recipe: ComparisonConfig,
    features: list[str],
    train_start: int,
) -> WindowResult:
    started = time.perf_counter()
    reference = Window("reference", window.start - days(cfg.reference.days), window.start - 1)
    rows_a = frame.loc[(frame[TIME] >= train_start) & (frame[TIME] < reference.start)]
    rows_b = frame.loc[(frame[TIME] >= train_start) & (frame[TIME] < window.start)]
    model_a = train_variant(rows_a, features, recipe)
    model_b = train_variant(rows_b, features, recipe)

    def scored(rows: pd.DataFrame, model: Any) -> pd.DataFrame:
        out = rows.copy()
        out[SCORE] = score(model, rows.drop(columns=LABEL, errors="ignore"), features)
        return out

    cur = scored(frame.loc[window.mask(frame[TIME])], model_b)
    ref = scored(frame.loc[reference.mask(frame[TIME])], model_a)
    categorical = [c for c in DEFAULT_CATEGORICAL if c in features]
    numerical = [c for c in features if c not in categorical]
    drift = run_drift(
        ref,
        cur,
        numerical=numerical,
        categorical=categorical,
        feature_cfg=cfg.feature_drift,
        prediction_cfg=cfg.prediction_drift,
    )

    now = int(cur[TIME].max())
    cutoff = now - days(cfg.labels.delay_days)
    perf_rows = frame.loc[
        (frame[TIME] > cutoff - days(cfg.performance.window_days)) & (frame[TIME] <= cutoff)
    ]
    perf = scored(perf_rows, model_a)
    positives = int(perf[LABEL].sum())
    delayed_ap = (
        average_precision(perf[LABEL].to_numpy(), perf[SCORE].to_numpy())
        if positives >= cfg.performance.min_positives and positives < len(perf)
        else None
    )

    gains = {
        k: float(v)
        for k, v in model_b.model.get_score(importance_type="gain").items()
        if isinstance(v, int | float)
    }
    top_gain = sorted(gains, key=lambda f: (-gains[f], f))[: cfg.feature_drift.top_gain_features]
    drifted = sorted(c.column for c in drift.columns if c.drifted)
    checks = CheckResults(
        drifted_feature_share=drift.drifted_share,
        prediction_drift_detected=drift.prediction_drift_detected,
        prediction_drift_score=drift.prediction_score,
        delayed_pr_auc=delayed_ap,
        champion_valid_pr_auc=model_a.holdout_average_precision,
        top_gain_features=tuple(top_gain),
        drifted_top_gain_features=tuple(f for f in top_gain if f in drifted),
    )
    trigger = evaluate_trigger(checks, cfg)
    shortfall = insufficient_data(len(cur), len(ref), cfg.current.min_rows)
    fired = trigger.fired and shortfall is None
    return WindowResult(
        start_day=window.start / 86400,
        end_day=(window.end + 1) / 86400,
        current_rows=len(cur),
        reference_rows=len(ref),
        sufficient=shortfall is None,
        drifted_feature_share=drift.drifted_share,
        drifted_features=drifted,
        prediction_psi=drift.prediction_score,
        prediction_drift_detected=drift.prediction_drift_detected,
        top_gain_features=top_gain,
        drifted_top_gain_features=[f for f in top_gain if f in drifted],
        delayed_rows=len(perf),
        delayed_positives=positives,
        delayed_pr_auc=delayed_ap,
        comparator_pr_auc=model_a.holdout_average_precision,
        fired=fired,
        reasons=trigger.reasons if shortfall is None else [shortfall],
        seconds=round(time.perf_counter() - started, 1),
    )


def rule_of(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def render(results: list[WindowResult], meta: dict[str, Any]) -> str:
    fires = sum(r.fired for r in results)
    lines = [
        "# Drift-trigger calibration on drift-free windows",
        "",
        f"Run {meta['finished']} from commit `{meta['git_commit']}` "
        f"(`python -m scripts.drift_calibration`), {meta['seconds']:.0f} s. Frozen rules "
        f"(`configs/drift.toml` sha256 `{meta['config_sha256'][:12]}…`), min_rows "
        f"{meta['min_rows']:,}. Windows fixed in code before the run (`plan_windows`).",
        "",
        f"**False fires: {fires} of {len(results)} windows ({fires / len(results):.0%}).**",
        "",
        "| Window (days, end excl.) | Current rows | Reference rows | Drifted share | PSI | "
        "Delayed PR-AUC / comparator | Drifted top-gain | Fired | Reasons |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        ap = "n/a" if r.delayed_pr_auc is None else f"{r.delayed_pr_auc:.4f}"
        lines.append(
            f"| {r.start_day:.0f}-{r.end_day:.0f} | {r.current_rows:,} | {r.reference_rows:,} | "
            f"{r.drifted_feature_share:.3f} | {r.prediction_psi:.4f} | {ap} / "
            f"{r.comparator_pr_auc:.4f} | {', '.join(r.drifted_top_gain_features) or '-'} | "
            f"{'**yes**' if r.fired else 'no'} | {'; '.join(r.reasons) or '-'} |"
        )
    counts: dict[str, int] = {}
    for r in results:
        for reason in r.reasons if r.fired else []:
            counts[rule_of(reason)] = counts.get(rule_of(reason), 0) + 1
    feats: dict[str, int] = {}
    for r in results:
        for f in r.drifted_top_gain_features:
            feats[f] = feats.get(f, 0) + 1
    lines += [
        "",
        "Fires per rule: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"),
        "",
        "Top-gain features that drifted (windows): "
        + (
            ", ".join(f"{k} {v}" for k, v in sorted(feats.items(), key=lambda kv: -kv[1])) or "none"
        ),
        "",
    ]
    return "\n".join(lines)


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    import hashlib
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--bundle", type=Path, default=SERVED_BUNDLE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    started = time.perf_counter()
    cfg = load_config(DEFAULT_CONFIG)
    champion = ModelBundle.load(args.bundle)
    train = champion_train_window(champion)
    windows = plan_windows(train, cfg.reference.days)
    logger.info(
        "planned %d windows: %s",
        len(windows),
        ", ".join(f"{w.start / 86400:.0f}-{(w.end + 1) / 86400:.0f}" for w in windows),
    )
    md = champion.metadata
    recipe = ComparisonConfig(seed=int(md["seed"]), imbalance=str(md["config"]["imbalance"]))
    raw = load_ieee_cis(args.raw_dir, usecols=[])
    frame = build_model_frame(raw, "baseline", HistoryConfig())
    results: list[WindowResult] = []
    for w in windows:
        r = evaluate_window(frame, w, cfg, recipe, champion.feature_columns, train.start)
        logger.info(
            "window %.0f-%.0f rows %d/%d share %.3f psi %.4f fired=%s %s (%.0f s)",
            r.start_day,
            r.end_day,
            r.current_rows,
            r.reference_rows,
            r.drifted_feature_share,
            r.prediction_psi,
            r.fired,
            r.reasons,
            r.seconds,
        )
        results.append(r)
    if not results:
        raise SystemExit("no windows planned")
    meta = {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "config_sha256": hashlib.sha256(DEFAULT_CONFIG.read_bytes()).hexdigest(),
        "min_rows": cfg.current.min_rows,
        "recipe": {"seed": recipe.seed, "imbalance": recipe.imbalance},
        "champion_train_days": [train.start / 86400, (train.end + 1) / 86400],
        "windows": len(results),
        "fires": sum(r.fired for r in results),
        "seconds": round(time.perf_counter() - started, 1),
        "finished": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "windows": [asdict(r) for r in results]}
    (args.out / "calibration.json").write_text(
        json.dumps(payload, indent=2, default=float) + "\n", encoding="utf-8"
    )
    (args.out / "calibration.md").write_text(render(results, meta), encoding="utf-8")
    logger.info("false fires %d of %d windows -> %s", meta["fires"], meta["windows"], args.out)
    print(json.dumps({k: meta[k] for k in ("windows", "fires", "seconds", "git_commit")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
