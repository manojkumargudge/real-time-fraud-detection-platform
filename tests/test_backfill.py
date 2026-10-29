"""MLflow report runs and the back-fill (throwaway SQLite backend, never the repo's mlflow.db)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

pytest.importorskip("mlflow")

from mlflow.tracking import MlflowClient  # noqa: E402

from scripts import mlflow_backfill as bf  # noqa: E402
from src.models import tracking  # noqa: E402
from src.models.train import fit_baseline, save_bundle  # noqa: E402
from tests.test_models import FAST  # noqa: E402


def registry_state(client: MlflowClient) -> dict[str, Any]:
    versions = client.search_model_versions("name='fraud-xgboost'")
    model = client.get_registered_model("fraud-xgboost")
    return {
        "versions": sorted((v.version, v.run_id, v.current_stage, v.status) for v in versions),
        "aliases": dict(model.aliases),
    }


@pytest.fixture
def store(synthetic_frame: pd.DataFrame, tmp_path: Path) -> tuple[str, MlflowClient]:
    """A tmp store with a registered champion (version 1) and aliases, like the real one."""
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    run = fit_baseline(synthetic_frame, FAST)
    bundle = save_bundle(run.trained, run.metadata, tmp_path / "bundle")
    tracking.log_training_run(run, bundle, register=True, uri=uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    client.set_registered_model_alias("fraud-xgboost", "production", "1")
    return uri, client


def test_flatten_metrics_keeps_finite_numbers_only() -> None:
    report = {
        "a": {"b": 1, "ok": True, "nan": float("nan"), "s": "x"},
        "list": [0.5, {"c": 2}],
        "bad key!": 3,
    }
    assert tracking.flatten_metrics(report) == {
        "a.b": 1.0,
        "list.0": 0.5,
        "list.1.c": 2.0,
        "bad key_": 3.0,
    }


def test_report_run_is_idempotent_and_never_touches_the_registry(
    store: tuple[str, MlflowClient], tmp_path: Path
) -> None:
    uri, client = store
    before = registry_state(client)
    report = tmp_path / "r.json"
    report.write_text(json.dumps({"pr_auc": 0.3, "n": [1, 2]}), encoding="utf-8")
    metrics = tracking.flatten_metrics(json.loads(report.read_text(encoding="utf-8")))

    first = tracking.log_report_run("r", [report], metrics=metrics, tags={"k": "v"}, uri=uri)
    again = tracking.log_report_run("r", [report], metrics=metrics, uri=uri)
    assert again == first

    run = client.get_run(first)
    assert run.data.metrics == {"pr_auc": 0.3, "n.0": 1.0, "n.1": 2.0}
    assert run.data.tags["report_sha256"] == tracking.file_sha256(report)
    assert run.data.tags["k"] == "v"
    assert [a.path for a in client.list_artifacts(first, "report")] == ["report/r.json"]
    exp = client.get_experiment_by_name(tracking.EVALUATION_EXPERIMENT)
    assert exp is not None
    assert len(client.search_runs([exp.experiment_id])) == 1
    after = registry_state(client)
    print("registry before", before, "after", after)
    assert before["versions"] and before["aliases"]  # both sides non-empty
    assert after == before

    report.write_text(json.dumps({"pr_auc": 0.31}), encoding="utf-8")  # new content: new run
    assert tracking.log_report_run("r", [report], uri=uri) != first


def make_root(root: Path, xgb_run: str) -> None:
    reports = root / "reports"
    reports.mkdir(parents=True)
    for name in (
        "model_comparison",
        "evaluation_study",
        "robust_evaluation_pilot",
        "robust_evaluation",
    ):
        (reports / f"{name}.json").write_text(json.dumps({name: 0.25}), encoding="utf-8")
        (reports / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    for name in ("model_comparison_seeds", "leakage_audit"):
        (reports / f"{name}.json").write_text(json.dumps({"x": {"y": 1}}), encoding="utf-8")
    (reports / "model_v1_vs_v2.md").write_text("# v1 vs v2\n", encoding="utf-8")
    unknown = "f" * 32
    (reports / "imbalance_comparison.md").write_text(
        f"runs `{xgb_run}` and `{unknown}`\n", encoding="utf-8"
    )
    (reports / "gnn_stacking.md").write_text("# stacking\n", encoding="utf-8")
    (reports / "fraud_rings.md").write_text("# rings\n", encoding="utf-8")
    (root / bf.GNN_SCORES).parent.mkdir(parents=True)
    (root / bf.GNN_SCORES).write_bytes(b"scores")


def drift_row(i: int) -> dict[str, Any]:
    return {
        "id": i,
        "run_at": datetime(2026, 10, 24, tzinfo=UTC),
        "synthetic": False,
        "drifted_feature_share": 0.1 * i,
        "triggered": i % 2 == 0,
        "forced": False,
        "promotion_decision": "keep" if i % 2 == 0 else None,
        "promotion": {"ci95_low": -0.016},
        "top_drifted_features": [{"feature": "card3", "score": 0.1}],
    }


def test_backfill_logs_each_report_once_and_keeps_the_registry(
    store: tuple[str, MlflowClient], tmp_path: Path
) -> None:
    uri, client = store
    before = registry_state(client)
    xgb_exp = client.get_experiment_by_name("fraud-xgboost")
    assert xgb_exp is not None
    xgb_run = client.search_runs([xgb_exp.experiment_id])[0].info.run_id
    gnn_exp = client.create_experiment(bf.GNN_EXPERIMENT)
    stack_run = client.create_run(gnn_exp).info.run_id
    root = tmp_path / "repo"
    make_root(root, xgb_run)

    def items() -> list[bf.Item]:
        out = bf.evaluation_items(root, client) + bf.gnn_items(root, client, stack_run[:8])
        return out + bf.drift_items([drift_row(i) for i in (1, 2)], tmp_path)

    first = bf.log_items(items(), uri, root, dry_run=False)
    second = bf.log_items(items(), uri, root, dry_run=False)
    assert len(first) == 8 + 2 + 2 and second == first  # idempotent: nothing new

    runs = {
        r.info.run_name: r
        for name in (tracking.EVALUATION_EXPERIMENT, bf.DRIFT_EXPERIMENT, bf.GNN_EXPERIMENT)
        for r in client.search_runs([client.get_experiment_by_name(name).experiment_id])  # type: ignore[union-attr]
    }
    assert runs["imbalance-decision"].data.tags["linked_run_ids"] == xgb_run  # unknown id dropped
    assert runs["model-comparison"].data.metrics == {"model_comparison": 0.25}
    assert runs["model-comparison"].data.tags["source_report"] == "reports/model_comparison.json"
    assert runs["leakage-audit"].data.tags["backfilled"] == "true"
    stacking = runs["gnn-stacking-report"].data.tags
    assert stacking["linked_run_ids"] == stack_run
    assert stacking["scores_sha256"] == tracking.file_sha256(root / bf.GNN_SCORES)
    drift = runs["drift-report-2"]
    assert (
        drift.data.tags["triggered"] == "true" and drift.data.tags["promotion_decision"] == "keep"
    )
    assert drift.data.tags["source_report"] == "postgres:drift_reports/2"
    assert drift.data.metrics["drifted_feature_share"] == pytest.approx(0.2)
    assert drift.data.metrics["promotion.ci95_low"] == pytest.approx(-0.016)
    after = registry_state(client)
    print("runs", len(runs), "registry before", before, "after", after)
    assert before["versions"] and after["versions"]
    assert after == before


def test_backfill_requires_an_explicit_tracking_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI")
    with pytest.raises(SystemExit):
        bf.main(["--only", "evaluation", "--dry-run"])


def test_drift_rows_are_read_only_and_must_be_ids_1_to_9(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    seen: dict[str, Any] = {}

    class Conn:
        def __enter__(self) -> Conn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, query: str, params: tuple[int, int]) -> Conn:
            seen["query"], seen["params"] = query, params
            return self

        def fetchall(self) -> list[dict[str, Any]]:
            return [drift_row(i) for i in range(1, 9)]  # id 9 missing

        def rollback(self) -> None:
            seen["rolled_back"] = True

    def connect(dsn: str, **kwargs: Any) -> Conn:
        seen["options"] = kwargs["options"]
        return Conn()

    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="expected drift_reports ids"):
        bf.read_drift_rows("postgresql://x")
    assert seen["options"] == "-c default_transaction_read_only=on"
    assert seen["query"].lstrip().upper().startswith("SELECT") and seen["params"] == (1, 9)
