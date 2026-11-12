"""S2: the served bundle reports its MLflow registry version (registry.json, GET /model)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from mlflow.tracking import MlflowClient

from src.models import link_registry, tracking
from src.models.train import fit_baseline, save_bundle
from src.serving.api import create_app
from src.serving.model import REGISTRY_FILE, ModelBundle, ModelBundleError
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore
from tests.test_models import FAST


def copy_bundle(bundle_dir: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "bundle"
    shutil.copytree(bundle_dir, dst)
    return dst


def link_record(sha: str, **overrides: Any) -> dict[str, Any]:
    record = {
        "name": "fraud-xgboost",
        "version": "2",
        "alias": "production",
        "run_id": "71c4f1bd820548c984341a06e51e33fc",
        "model_sha256": sha,
        "linked_at": "2026-11-12T00:00:00+00:00",
    }
    return {**record, **overrides}


def served_sha(d: Path) -> str:
    return str(json.loads((d / "metadata.json").read_text(encoding="utf-8"))["model_sha256"])


# --- loader --------------------------------------------------------------------------------------


def test_unlinked_bundle_loads_with_no_registry(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    assert ModelBundle.load(d).registry is None


def test_linked_bundle_reports_registry_name_and_version(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    (d / REGISTRY_FILE).write_text(json.dumps(link_record(served_sha(d))), encoding="utf-8")
    link = ModelBundle.load(d).registry
    assert link is not None
    assert (link.name, link.version, link.alias) == ("fraud-xgboost", "2", "production")


def test_startup_fails_when_the_link_hash_differs(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    (d / REGISTRY_FILE).write_text(json.dumps(link_record("0" * 64)), encoding="utf-8")
    with pytest.raises(ModelBundleError, match="link_registry"):
        ModelBundle.load(d)


def test_startup_fails_on_a_malformed_link(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    (d / REGISTRY_FILE).write_text(json.dumps({"name": "fraud-xgboost"}), encoding="utf-8")
    with pytest.raises(ModelBundleError, match="malformed"):
        ModelBundle.load(d)


# --- API -----------------------------------------------------------------------------------------


def api_get(d: Path, path: str) -> dict[str, Any]:
    scorer = Scorer(ModelBundle.load(d), InMemoryStore())
    with TestClient(create_app(scorer)) as c:
        r = c.get(path)
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    return body


def test_model_endpoint_linked_and_unlinked(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    unlinked = api_get(d, "/model")
    assert unlinked["linked"] is False and unlinked["registry_version"] is None
    assert unlinked["model_sha256"] == served_sha(d)

    (d / REGISTRY_FILE).write_text(json.dumps(link_record(served_sha(d))), encoding="utf-8")
    linked = api_get(d, "/model")
    assert linked["linked"] is True
    assert (linked["model_name"], linked["registry_version"], linked["alias"]) == (
        "fraud-xgboost",
        "2",
        "production",
    )
    assert linked["model_version"] == unlinked["model_version"]


def test_health_body_is_unchanged_by_the_link(bundle_dir: Path, tmp_path: Path) -> None:
    d = copy_bundle(bundle_dir, tmp_path)
    before = api_get(d, "/health")
    (d / REGISTRY_FILE).write_text(json.dumps(link_record(served_sha(d))), encoding="utf-8")
    after = api_get(d, "/health")
    assert before == after
    assert set(after) == {"status", "model_version", "event_schema_version", "store", "store_ok"}


# --- link CLI against a throwaway MLflow store ----------------------------------------------------


@pytest.fixture
def registry(
    synthetic_frame: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    """A temp registry whose champion = production = v1, and that version's bundle on disk."""
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    run = fit_baseline(synthetic_frame, FAST)
    bundle = save_bundle(run.trained, run.metadata, tmp_path / "served")
    run_id = tracking.log_training_run(run, bundle, register=True)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    client.set_registered_model_alias(tracking.MODEL_NAME, "production", "1")
    assert run_id
    return uri, bundle


def aliases(uri: str) -> dict[str, str]:
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    rm = client.get_registered_model(tracking.MODEL_NAME)
    return {k: str(v) for k, v in rm.aliases.items()}


def test_link_writes_registry_json_and_never_moves_aliases(registry: tuple[str, Path]) -> None:
    uri, bundle = registry
    before = aliases(uri)
    assert link_registry.main(["--alias", "production", "--bundle", str(bundle)]) == 0
    record = json.loads((bundle / REGISTRY_FILE).read_text(encoding="utf-8"))
    assert record["version"] == "1" and record["alias"] == "production"
    assert record["model_sha256"] == served_sha(bundle)
    assert aliases(uri) == before == {"champion": "1", "production": "1"}
    assert ModelBundle.load(bundle).registry is not None  # the written link passes the load check


def test_check_mode_writes_nothing(registry: tuple[str, Path]) -> None:
    _, bundle = registry
    assert link_registry.main(["--alias", "production", "--bundle", str(bundle), "--check"]) == 0
    assert not (bundle / REGISTRY_FILE).exists()


def test_link_refuses_when_the_served_model_differs(
    registry: tuple[str, Path], tmp_path: Path
) -> None:
    _, bundle = registry
    other_dir = tmp_path / "other"
    shutil.copytree(bundle, other_dir)
    model = other_dir / "model.json"
    model.write_bytes(model.read_bytes() + b" ")  # served model.json no longer equals v1's
    with pytest.raises(link_registry.LinkError, match="not linking"):
        link_registry.registry_link("production", other_dir)
    assert link_registry.main(["--alias", "production", "--bundle", str(other_dir)]) == 1
    assert not (other_dir / REGISTRY_FILE).exists()


def test_link_refuses_an_unknown_alias(registry: tuple[str, Path]) -> None:
    _, bundle = registry
    with pytest.raises(link_registry.LinkError, match="not found"):
        link_registry.registry_link("nonexistent", bundle)


def test_missing_link_file_serves_with_a_warning_and_null_registry(
    bundle_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Defined behaviour: no registry.json -> the API starts, /model has nulls, one WARNING."""
    import logging

    d = copy_bundle(bundle_dir, tmp_path)
    assert not (d / REGISTRY_FILE).exists()
    with caplog.at_level(logging.WARNING, logger="src.serving.model"):
        body = api_get(d, "/model")
        health = api_get(d, "/health")
    assert health["status"] == "ok"
    assert body["linked"] is False
    assert all(
        body[k] is None for k in ("model_name", "registry_version", "alias", "run_id", "linked_at")
    )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "not linked" in warnings[0].getMessage()
    assert "link_registry" in warnings[0].getMessage()


def test_present_link_file_logs_no_warning(
    bundle_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    d = copy_bundle(bundle_dir, tmp_path)
    (d / REGISTRY_FILE).write_text(json.dumps(link_record(served_sha(d))), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.serving.model"):
        body = api_get(d, "/model")
    assert body["linked"] is True and body["registry_version"] == "2"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
