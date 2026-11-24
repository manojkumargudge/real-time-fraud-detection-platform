"""Register the verified baseline bundle on an empty deployment's private MLflow server."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

NAME = "fraud-xgboost"
VERSION = "baseline-9fdad27ea3b0"
MODEL_SHA = "9fdad27ea3b07d5c29159bae75acc3ecaa7bf671943ab543bf9385145b89ba88"


def main() -> None:
    bundle = Path("/app/artifacts/baseline")
    model = bundle / "model.json"
    metadata_file = bundle / "metadata.json"
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    link_file = bundle / "registry.json"
    link = json.loads(link_file.read_text(encoding="utf-8")) if link_file.is_file() else {}
    if (
        hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA
        or metadata["model_sha256"] != MODEL_SHA
        or metadata["model_version"] != VERSION
        or metadata["threshold"] != 0.12806208431720734
    ):
        raise SystemExit("Champion model or metadata differs from the validated baseline")

    uri = os.environ["MLFLOW_TRACKING_URI"]
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    try:
        client.get_model_version_by_alias(NAME, "champion")
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_DOES_NOT_EXIST":
            raise
    else:
        raise SystemExit("A champion already exists. Use the initial import on a fresh registry.")
    experiment = client.get_experiment_by_name(NAME)
    experiment_id = experiment.experiment_id if experiment else client.create_experiment(NAME)
    run = client.create_run(
        experiment_id,
        tags={"model_version": VERSION, "source_run_id": link.get("run_id", "unlinked")},
    )
    run_id = run.info.run_id
    try:
        client.log_artifact(run_id, str(model), "model")
        client.log_artifact(run_id, str(metadata_file), "model")
        client.log_dict(
            run_id, {"feature_columns": metadata["feature_columns"]}, "model/features.json"
        )
        client.log_metric(
            run_id, "validation_pr_auc", metadata["validation_metrics"]["average_precision"]
        )
        client.log_param(run_id, "threshold", metadata["threshold"])
        with tempfile.TemporaryDirectory() as temp:
            for source in (model, metadata_file):
                downloaded = Path(client.download_artifacts(run_id, f"model/{source.name}", temp))
                if downloaded.read_bytes() != source.read_bytes():
                    raise RuntimeError("Downloaded artifact differs from the source bundle")
        try:
            client.create_registered_model(NAME)
        except MlflowException as exc:
            if exc.error_code != "RESOURCE_ALREADY_EXISTS":
                raise
        version = client.create_model_version(
            NAME, source=f"{run.info.artifact_uri}/model", run_id=run_id
        )
        client.set_registered_model_alias(NAME, "champion", str(version.version))
    except Exception:
        client.set_terminated(run_id, "FAILED")
        raise
    client.set_terminated(run_id, "FINISHED")
    print(f"Imported {VERSION} as {NAME} version {version.version}")


if __name__ == "__main__":
    main()
