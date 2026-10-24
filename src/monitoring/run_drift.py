"""Drift job: feature drift, prediction drift, delayed-label performance, retraining trigger.

Usage::

    python -m src.monitoring.run_drift [--window-start-day D --window-end-day D] [--no-retrain]
        [--synthetic-amount-factor F] [--config configs/drift.toml]

Needs ``DATABASE_URL`` (reads ``scored_transactions``, writes ``drift_reports``) and the
IEEE-CIS files (features via the same point-in-time code as serving, and the labels that the
label delay allows). Writes the Evidently HTML report to ``reports/drift/`` (gitignored).

* now       = latest ``transaction_dt`` in ``scored_transactions`` (transaction time).
* reference = last ``reference.days`` of the champion's training window; features from the
  batch feature code; scores from the champion's recipe refit on the training rows before the
  reference window, so the reference score distribution is out-of-sample, like production.
* current   = scored transactions in the window; features recomputed with the same code
  (equal to what Feast served, see docs/feature_store.md); scores as stored by production.
* delayed-label PR-AUC = stored scores vs labels in ``(cutoff - performance.window_days,
  cutoff]``, cutoff = now - ``labels.delay_days``.

Retraining runs on the host only (``--no-retrain`` for the Compose ``monitoring`` service).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.history import TIME, HistoryConfig
from src.features.preprocess import DEFAULT_CATEGORICAL
from src.models.compare import ComparisonConfig, build_model_frame, score, train_variant
from src.models.metrics import average_precision
from src.monitoring.config import DEFAULT_CONFIG, DriftConfig, load_config
from src.monitoring.drift import SCORE, run_drift, save_html
from src.monitoring.report_store import DriftReportRow, insert_report, load_scored
from src.monitoring.rules import CheckResults, evaluate_trigger
from src.monitoring.windows import (
    RetrainWindows,
    Window,
    WindowOverlapError,
    day_window,
    days,
    label_cutoff,
    plan_retrain,
    usable_labels,
)
from src.serving.model import ModelBundle

logger = logging.getLogger("src.monitoring.run_drift")

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_ROOT / "reports" / "drift"
SERVED_BUNDLE = REPO_ROOT / "artifacts" / "baseline"


@dataclass(frozen=True)
class JobInputs:
    """Everything the job reads, so tests can run it on synthetic data."""

    raw: pd.DataFrame  # loader-shaped IEEE-CIS frame, labels included
    scored: pd.DataFrame  # transaction_id, transaction_dt, score, model_version
    champion: ModelBundle


@dataclass(frozen=True)
class JobOptions:
    window: tuple[float, float] | None = None  # [start_day, end_day)
    retrain: bool = True
    synthetic_amount_factor: float | None = None
    reports_dir: Path = REPORTS_DIR
    mlflow_uri: str | None = None
    champion_source: str = "unknown"


def champion_train_window(champion: ModelBundle) -> Window:
    split = champion.metadata["split"]
    return Window("champion_train", int(split["train_dt_min"]), int(split["train_end"]) - 1)


def reference_scores(
    frame: pd.DataFrame, champion: ModelBundle, train_window: Window, reference: Window
) -> np.ndarray[Any, Any]:
    """Out-of-sample scores on the reference window: the champion's recipe refit on its
    training rows before the reference window (in-sample scores would be overconfident)."""
    md = champion.metadata
    cfg = ComparisonConfig(seed=int(md["seed"]), imbalance=str(md["config"]["imbalance"]))
    before = frame.loc[(frame[TIME] >= train_window.start) & (frame[TIME] < reference.start)]
    trained = train_variant(before, champion.feature_columns, cfg)
    ref_rows = frame.loc[reference.mask(frame[TIME])]
    return score(trained, ref_rows.drop(columns=LABEL, errors="ignore"), champion.feature_columns)


def run_job(inputs: JobInputs, cfg: DriftConfig, opts: JobOptions, dsn: str) -> int:
    """Run all checks, retrain if triggered (and allowed), store the row; returns its id."""
    scored = inputs.scored
    if scored.empty:
        raise RuntimeError("scored_transactions is empty: nothing to monitor")
    champion = inputs.champion
    now = int(scored["transaction_dt"].max())
    cutoff = label_cutoff(now, cfg.labels.delay_days)
    champ_train = champion_train_window(champion)
    reference = Window("reference", champ_train.end + 1 - days(cfg.reference.days), champ_train.end)
    if opts.window is not None:
        current = day_window("current", *opts.window)
    else:
        current = Window("current", now - days(cfg.current.default_days) + 1, now)
    performance = Window("performance", cutoff - days(cfg.performance.window_days) + 1, cutoff)
    retrain_windows = _retrain_windows(champ_train, cutoff, cfg, strict=opts.retrain)

    factor = opts.synthetic_amount_factor
    synthetic = factor is not None
    raw = inputs.raw
    if factor is not None:
        raw = raw.copy()
        in_window = current.mask(raw[TIME])
        raw.loc[in_window, "TransactionAmt"] *= factor
    hist_cfg = HistoryConfig()
    frame = build_model_frame(raw, "baseline", hist_cfg)
    features = champion.feature_columns

    # Current window: scored rows only, with their stored production scores.
    cur_scored = scored.loc[current.mask(scored["transaction_dt"])]
    cur = frame.merge(cur_scored[["transaction_id", SCORE]], left_on=KEY, right_on="transaction_id")
    if cur.empty:
        raise RuntimeError(f"no scored transactions in {current.describe()}")
    if synthetic:  # shifted copy: score it with the champion like production would
        cur[SCORE] = champion.predict(cur)
    ref = frame.loc[reference.mask(frame[TIME])].copy()
    ref[SCORE] = reference_scores(frame, champion, champ_train, reference)

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

    # Delayed labels: only rows at or before the cutoff are ever joined to a label.
    labels = usable_labels(raw[[KEY, TIME, LABEL]], cutoff)
    perf = scored.loc[performance.mask(scored["transaction_dt"])].merge(
        labels, left_on="transaction_id", right_on=KEY
    )
    positives = int(perf[LABEL].sum()) if not perf.empty else 0
    delayed_ap = (
        average_precision(perf[LABEL].to_numpy(), perf[SCORE].to_numpy())
        if positives >= cfg.performance.min_positives and positives < len(perf)
        else None
    )
    champion_ap = float(champion.metadata["validation_metrics"]["average_precision"])
    checks = CheckResults(
        drifted_feature_share=drift.drifted_share,
        prediction_drift_detected=drift.prediction_drift_detected,
        prediction_drift_score=drift.prediction_score,
        delayed_pr_auc=delayed_ap,
        champion_valid_pr_auc=champion_ap,
    )
    trigger = evaluate_trigger(checks, cfg)

    windows = {
        w.name: w.to_dict()
        for w in (champ_train, reference, current, performance, *retrain_windows.all()[1:])
    }
    windows["now"] = {"dt": now, "day": round(now / 86400, 3)}
    windows["label_cutoff"] = {"dt": cutoff, "day": round(cutoff / 86400, 3)}
    for w in (champ_train, retrain_windows.challenger_train, retrain_windows.holdout, current):
        logger.info("window %s", w.describe())
    logger.info("now=%d label_cutoff=%d (delay %d days)", now, cutoff, cfg.labels.delay_days)

    retrain_run_id = challenger_version = decision_text = None
    promotion: dict[str, Any] | None = None
    if trigger.fired and opts.retrain:
        from src.models.tracking import tracking_uri
        from src.monitoring.retrain import MlflowRegistry, retrain_and_compare

        # With retraining enabled, inputs.champion is the MLflow champion (see _load_champion).
        registry = MlflowRegistry(opts.mlflow_uri or tracking_uri())
        labelled = usable_labels(frame, cutoff)
        result = retrain_and_compare(
            labelled,
            retrain_windows,
            champion,
            cfg,
            registry,
            tags={"trigger": "; ".join(trigger.reasons), "synthetic": str(synthetic).lower()},
        )
        retrain_run_id, challenger_version = result.run_id, result.challenger_version
        decision_text = result.decision.reason
        promotion = result.summary()
    elif trigger.fired:
        decision_text = "not run: retraining disabled (--no-retrain; host-only step)"

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    title = (
        f"{'SYNTHETIC=TRUE ' if synthetic else ''}Drift report {stamp}: current "
        f"day {current.start / 86400:.1f}-{(current.end + 1) / 86400:.1f} vs reference "
        f"day {reference.start / 86400:.1f}-{(reference.end + 1) / 86400:.1f}"
        + (f" (TransactionAmt x{opts.synthetic_amount_factor} on a copy)" if synthetic else "")
    )
    name = f"drift_{stamp}{'_synthetic' if synthetic else ''}.html"
    report_path = save_html(drift.snapshot, opts.reports_dir / name, title)

    row = DriftReportRow(
        synthetic=synthetic,
        synthetic_note=(
            f"TransactionAmt x{opts.synthetic_amount_factor} on a copy of the current window"
            if synthetic
            else None
        ),
        now_dt=now,
        label_cutoff_dt=cutoff,
        window_start_dt=current.start,
        window_end_dt=current.end,
        reference_start_dt=reference.start,
        reference_end_dt=reference.end,
        current_rows=len(cur),
        reference_rows=len(ref),
        feature_count=len(drift.columns),
        drifted_feature_count=drift.drifted_count,
        drifted_feature_share=drift.drifted_share,
        top_drifted_features=drift.top_drifted(cfg.feature_drift.top_n),
        prediction_drift_method=drift.prediction_method,
        prediction_drift_score=drift.prediction_score,
        prediction_drift_detected=drift.prediction_drift_detected,
        performance_start_dt=performance.start,
        performance_end_dt=performance.end,
        delayed_rows=len(perf),
        delayed_positives=positives,
        delayed_pr_auc=delayed_ap,
        champion_valid_pr_auc=champion_ap,
        triggered=trigger.fired,
        trigger_reasons=trigger.reasons,
        retrain_run_id=retrain_run_id,
        challenger_version=challenger_version,
        promotion_decision=decision_text,
        promotion=promotion,
        windows=windows,
        report_path=str(report_path),
        config={**cfg.model_dump(), "champion_source": opts.champion_source},
    )
    report_id = insert_report(dsn, row)
    logger.info(
        "drift report id=%d triggered=%s reasons=%s share=%.3f prediction %s=%.4f delayed_pr_auc=%s"
        " report=%s",
        report_id,
        trigger.fired,
        trigger.reasons,
        drift.drifted_share,
        drift.prediction_method,
        drift.prediction_score,
        "n/a" if delayed_ap is None else f"{delayed_ap:.4f}",
        report_path,
    )
    return report_id


def _retrain_windows(
    champ_train: Window, cutoff: int, cfg: DriftConfig, *, strict: bool
) -> RetrainWindows:
    """Planned retrain windows. With retraining enabled an overlap fails the job; without it
    (drift checks only) the windows are only recorded."""
    try:
        return plan_retrain(
            champion_train=champ_train,
            train_dt_min=champ_train.start,
            cutoff=cutoff,
            holdout_days=cfg.retrain.holdout_days,
        )
    except WindowOverlapError:
        if strict:
            raise
        logger.warning("retrain windows overlap the holdout; recorded only (--no-retrain)")
        holdout = Window("holdout", cutoff - days(cfg.retrain.holdout_days) + 1, cutoff)
        challenger = Window(
            "challenger_train", champ_train.start, max(champ_train.start, holdout.start - 1)
        )
        return RetrainWindows(champ_train, challenger, holdout)


def _load_champion(
    retrain: bool, bundle_dir: Path, mlflow_uri: str | None
) -> tuple[ModelBundle, str]:
    if not retrain:
        return ModelBundle.load(bundle_dir), f"bundle {bundle_dir}"
    from src.models.tracking import tracking_uri
    from src.monitoring.retrain import MlflowRegistry

    registry = MlflowRegistry(mlflow_uri or tracking_uri())
    version = registry.alias_version("champion")
    with tempfile.TemporaryDirectory() as tmp:
        bundle = ModelBundle.load(registry.download("champion", Path(tmp)))
    return bundle, f"mlflow fraud-xgboost@champion (version {version})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--window-start-day", type=float)
    parser.add_argument("--window-end-day", type=float)
    parser.add_argument("--no-retrain", action="store_true", help="drift checks only (container)")
    parser.add_argument(
        "--champion-bundle",
        type=Path,
        default=SERVED_BUNDLE,
        help="champion bundle for --no-retrain (with retraining, MLflow champion is used)",
    )
    parser.add_argument("--synthetic-amount-factor", type=float)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if (args.window_start_day is None) != (args.window_end_day is None):
        parser.error("--window-start-day and --window-end-day go together")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is not set")
    cfg = load_config(args.config)
    retrain = not args.no_retrain
    mlflow_uri = os.environ.get("MLFLOW_TRACKING_URI")
    champion, source = _load_champion(retrain, args.champion_bundle, mlflow_uri)
    raw = load_ieee_cis(args.raw_dir, usecols=[])
    inputs = JobInputs(raw=raw, scored=load_scored(dsn), champion=champion)
    window = None if args.window_start_day is None else (args.window_start_day, args.window_end_day)
    opts = JobOptions(
        window=window,
        retrain=retrain,
        synthetic_amount_factor=args.synthetic_amount_factor,
        reports_dir=args.reports_dir,
        mlflow_uri=mlflow_uri,
        champion_source=source,
    )
    report_id = run_job(inputs, cfg, opts, dsn)
    print(json.dumps({"drift_report_id": report_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
