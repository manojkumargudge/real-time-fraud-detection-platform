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
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
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


def _experiment_id(client: MlflowClient, uri: str) -> str:
    experiment = client.get_experiment_by_name(EXPERIMENT)
    if experiment is not None:
        return str(experiment.experiment_id)
    return str(client.create_experiment(EXPERIMENT, artifact_location=_artifact_location(uri)))


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
            params=[
                Param(k, str(v))  # type: ignore[no-untyped-call]  # mlflow leaves __init__ untyped
                for k, v in run_params(run, imbalance_method).items()
            ],
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
    try:
        client.create_registered_model(MODEL_NAME)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
    artifact_uri = client.get_run(run_id).info.artifact_uri
    version = client.create_model_version(
        MODEL_NAME, source=f"{artifact_uri}/{ARTIFACT_PATH}", run_id=run_id
    )
    client.set_registered_model_alias(MODEL_NAME, CHAMPION_ALIAS, str(version.version))
    logger.info("registered %s version=%s alias=%s", MODEL_NAME, version.version, CHAMPION_ALIAS)
    return str(version.version)
