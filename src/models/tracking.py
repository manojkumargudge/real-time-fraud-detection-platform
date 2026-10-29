"""MLflow tracking for baseline training runs.

Backend: ``MLFLOW_TRACKING_URI`` if set, else ``sqlite:///<repo>/mlflow.db``. For a ``sqlite:``
URI the experiment's artifacts go to ``mlruns/`` next to the database file (both gitignored);
other URIs use the server's artifact store. Each run logs:

* params  -- XGBoost params, seed, split boundary, training fraud rate, imbalance method and
  ``scale_pos_weight`` value, chosen rounds and threshold;
* metrics -- validation PR-AUC (the decision metric), ROC-AUC (reference only), precision /
  recall at the deployed threshold and at the Milestone 2.2 alert budgets, training time;
* artifacts -- ``model/model.json``, ``model/metadata.json``, ``model/features.json``;
* tags    -- git commit (and whether the tree was dirty), imbalance method, variant.

Only the training CLI imports this module; serving never needs mlflow. The ``artifacts/``
bundle that serving loads is written exactly as before.

``log_report_run`` logs an evaluation report (no model, never the registry) to experiment
``fraud-evaluation``: the numeric leaves of its JSON as metrics, its files as artifacts, and
tags ``source_report`` / ``report_sha256`` (scripts/mlflow_backfill.py uses it for old reports).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from mlflow.entities import Metric, Param
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

from src.models.metrics import alert_budget_metrics, roc_auc
from src.models.robust_eval import BUDGET_RATES
from src.models.train import METADATA_FILE, MODEL_FILE, BaselineRun

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = "fraud-xgboost"
EVALUATION_EXPERIMENT = "fraud-evaluation"
MODEL_NAME = "fraud-xgboost"
CHAMPION_ALIAS = "champion"
ARTIFACT_PATH = "model"


def tracking_uri() -> str:
    default = f"sqlite:///{(REPO_ROOT / 'mlflow.db').as_posix()}"
    return os.environ.get("MLFLOW_TRACKING_URI") or default


def _artifact_location(uri: str) -> str | None:
    if uri.startswith("sqlite:///"):
        return (Path(uri.removeprefix("sqlite:///")).parent / "mlruns").resolve().as_uri()
    return None


def _experiment_id(client: MlflowClient, uri: str, name: str = EXPERIMENT) -> str:
    experiment = client.get_experiment_by_name(name)
    if experiment is not None:
        return str(experiment.experiment_id)
    return str(client.create_experiment(name, artifact_location=_artifact_location(uri)))


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def run_params(run: BaselineRun, imbalance_method: str) -> dict[str, Any]:
    md = run.metadata
    return {
        **{f"xgb_{k}": v for k, v in md["xgb_params"].items()},
        "seed": md["seed"],
        "variant": md["variant"],
        "train_end": md["split"]["train_end"],
        "valid_start": md["split"]["valid_start"],
        "train_rows": md["split"]["train_rows"],
        "valid_rows": md["split"]["valid_rows"],
        "train_fraud_rate": round(run.train_fraud_rate, 6),
        "imbalance_method": imbalance_method,
        "scale_pos_weight": md["scale_pos_weight"],
        "best_rounds": md["best_rounds"],
        "threshold": md["threshold"],
        "feature_count": len(md["feature_columns"]),
    }


def run_metrics(run: BaselineRun) -> dict[str, float]:
    md = run.metadata
    at = md["validation_metrics"]["at_threshold"]
    metrics: dict[str, float] = {
        "valid_pr_auc": md["validation_metrics"]["average_precision"],
        "valid_roc_auc": roc_auc(run.y_valid, run.valid_scores),
        "holdout_pr_auc": md["holdout_average_precision"],
        "valid_precision_at_threshold": at["precision"],
        "valid_recall_at_threshold": at["recall"],
        "valid_alert_rate_at_threshold": at["flagged_rate"],
        "train_seconds": md["training_seconds"],
    }
    for row in alert_budget_metrics(run.y_valid, run.valid_scores, BUDGET_RATES):
        tag = f"{float(row['rate'] or 0) * 100:g}pct"  # 0.005 -> "0.5pct"
        for name in ("precision", "recall"):
            value = row[name]
            if value is not None:
                metrics[f"valid_{name}_at_budget_{tag}"] = float(value)
    return metrics


def log_training_run(
    run: BaselineRun,
    bundle_dir: Path,
    *,
    imbalance_method: str = "scale_pos_weight",
    register: bool = False,
    uri: str | None = None,
    extra_tags: dict[str, str] | None = None,
    run_name: str | None = None,
) -> str:
    """Log one training run (and optionally register it as champion); returns the run ID."""
    uri = uri or tracking_uri()
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    commit = _git("rev-parse", "HEAD") or "unknown"
    tags = {
        "git_commit": commit,
        "git_dirty": str(bool(_git("status", "--porcelain"))).lower(),
        "imbalance_method": imbalance_method,
        "variant": str(run.metadata["variant"]),
        **(extra_tags or {}),
    }
    seed = run.metadata["seed"]
    created = client.create_run(
        _experiment_id(client, uri),
        tags=tags,
        run_name=run_name or f"baseline-{imbalance_method}-seed{seed}",
    )
    run_id = str(created.info.run_id)
    now = int(time.time() * 1000)
    try:
        client.log_batch(
            run_id,
            metrics=[Metric(k, float(v), now, 0) for k, v in run_metrics(run).items()],
            params=[Param(k, str(v)) for k, v in run_params(run, imbalance_method).items()],
        )
        client.log_artifact(run_id, str(bundle_dir / MODEL_FILE), ARTIFACT_PATH)
        client.log_artifact(run_id, str(bundle_dir / METADATA_FILE), ARTIFACT_PATH)
        client.log_dict(
            run_id,
            {"feature_columns": run.metadata["feature_columns"]},
            f"{ARTIFACT_PATH}/features.json",
        )
    except Exception:
        client.set_terminated(run_id, "FAILED")
        raise
    client.set_terminated(run_id)
    logger.info("logged MLflow run id=%s experiment=%s uri=%s", run_id, EXPERIMENT, uri)
    if register:
        register_champion(client, run_id)
    return run_id


def register_champion(client: MlflowClient, run_id: str) -> str:
    """Register the run's model directory as a new ``fraud-xgboost`` version and point the
    ``champion`` alias at it; returns the version number."""
    return register_version(client, run_id, CHAMPION_ALIAS)


def register_version(client: MlflowClient, run_id: str, alias: str) -> str:
    """Register the run's model directory as a new ``fraud-xgboost`` version and point
    ``alias`` at it; returns the version number."""
    try:
        client.create_registered_model(MODEL_NAME)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
    artifact_uri = client.get_run(run_id).info.artifact_uri
    version = client.create_model_version(
        MODEL_NAME, source=f"{artifact_uri}/{ARTIFACT_PATH}", run_id=run_id
    )
    client.set_registered_model_alias(MODEL_NAME, alias, str(version.version))
    logger.info("registered %s version=%s alias=%s", MODEL_NAME, version.version, alias)
    return str(version.version)


_METRIC_KEY_INVALID = re.compile(r"[^0-9A-Za-z_\-. /]")
MAX_KEY_LENGTH = 250
METRICS_PER_BATCH = 1000  # MLflow log_batch limits
PARAMS_PER_BATCH = 100
MAX_PARAM_LENGTH = 500


def _numeric_leaves(value: Any, key: str) -> Iterator[tuple[str, float]]:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        if math.isfinite(value):
            yield key, float(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _numeric_leaves(v, f"{key}.{k}" if key else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _numeric_leaves(v, f"{key}.{i}" if key else str(i))


def flatten_metrics(report: Any, exclude: tuple[str, ...] = ()) -> dict[str, float]:
    """Finite numeric leaves of a JSON report as ``a.b.0.c`` metric keys (booleans skipped),
    without the top-level keys in ``exclude`` (per-run detail stays in the report artifact).
    Keys are made MLflow-safe; on a clash after that, the first value wins (logged)."""
    if exclude and isinstance(report, dict):
        report = {k: v for k, v in report.items() if k not in exclude}
    out: dict[str, float] = {}
    clashes = 0
    for key, value in _numeric_leaves(report, ""):
        safe = _METRIC_KEY_INVALID.sub("_", key)[:MAX_KEY_LENGTH]
        if safe in out:
            clashes += 1
            continue
        out[safe] = value
    if clashes:
        logger.warning("flatten_metrics: %d keys clashed after sanitising; kept the first", clashes)
    return out


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_relative(path: Path) -> str:
    resolved = path.resolve()
    return (
        resolved.relative_to(REPO_ROOT).as_posix()
        if resolved.is_relative_to(REPO_ROOT)
        else str(path)
    )


def find_report_run(
    client: MlflowClient, experiment_id: str, run_name: str, report_sha256: str
) -> str | None:
    """The run already logged under this name for this report content, if any (the
    idempotency key: two different reports may have identical bytes)."""
    query = f"tags.report_sha256 = '{report_sha256}' and attributes.run_name = '{run_name}'"
    runs = client.search_runs([experiment_id], filter_string=query, max_results=1)
    return str(runs[0].info.run_id) if runs else None


def log_report_run(
    run_name: str,
    report_files: list[Path],
    *,
    metrics: dict[str, float] | None = None,
    params: dict[str, Any] | None = None,
    tags: dict[str, str] | None = None,
    experiment: str = EVALUATION_EXPERIMENT,
    report_sha256: str | None = None,
    uri: str | None = None,
) -> str:
    """Log one evaluation report as a run: metrics, params, the files as ``report/`` artifacts.

    Tracking only: no model is logged and the registry is never called. Idempotent: if a run
    in ``experiment`` named ``run_name`` already carries this ``report_sha256`` (default:
    sha256 of the first file), its ID is returned and nothing is written. Returns the run ID."""
    if not report_files:
        raise ValueError("log_report_run needs at least one report file")
    uri = uri or tracking_uri()
    client = MlflowClient(tracking_uri=uri)
    sha = report_sha256 or file_sha256(report_files[0])
    experiment_id = _experiment_id(client, uri, experiment)
    existing = find_report_run(client, experiment_id, run_name, sha)
    if existing is not None:
        logger.info("report already logged: run %s (%s, sha256 %s)", existing, run_name, sha[:12])
        return existing
    all_tags = {
        "git_commit": _git("rev-parse", "HEAD") or "unknown",
        "git_dirty": str(bool(_git("status", "--porcelain"))).lower(),
        "source_report": _repo_relative(report_files[0]),
        **(tags or {}),
        "report_sha256": sha,
    }
    run_id = str(client.create_run(experiment_id, tags=all_tags, run_name=run_name).info.run_id)
    now = int(time.time() * 1000)
    try:
        items = [Metric(k, float(v), now, 0) for k, v in (metrics or {}).items()]
        for i in range(0, len(items), METRICS_PER_BATCH):
            client.log_batch(run_id, metrics=items[i : i + METRICS_PER_BATCH])
        plist = [Param(k, str(v)[:MAX_PARAM_LENGTH]) for k, v in (params or {}).items()]
        for i in range(0, len(plist), PARAMS_PER_BATCH):
            client.log_batch(run_id, params=plist[i : i + PARAMS_PER_BATCH])
        for path in report_files:
            client.log_artifact(run_id, str(path), "report")
    except Exception:
        client.set_terminated(run_id, "FAILED")
        raise
    client.set_terminated(run_id)
    logger.info(
        "logged report run id=%s name=%s experiment=%s metrics=%d",
        run_id,
        run_name,
        experiment,
        len(items),
    )
    return run_id
