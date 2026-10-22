"""MLflow run tracking against a throwaway SQLite backend (never the repo's mlflow.db)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.models.metrics import roc_auc

pytest.importorskip("mlflow")

from mlflow.tracking import MlflowClient  # noqa: E402

from src.models import tracking  # noqa: E402
from src.models.train import fit_baseline, save_bundle  # noqa: E402
from tests.test_models import FAST  # noqa: E402


def _repo_state() -> dict[str, float | None]:
    """Modification time of each default store in the repo (None = absent)."""
    paths = [tracking.REPO_ROOT / "mlflow.db", tracking.REPO_ROOT / "mlruns"]
    return {str(p): p.stat().st_mtime if p.exists() else None for p in paths}


class TestRocAuc:
    def test_hand_computed_with_ties(self) -> None:
        # Pairs (pos, neg): (0.8,0.1) win, (0.8,0.4) win, (0.4,0.1) win, (0.4,0.4) tie -> 3.5/4.
        assert roc_auc(np.array([1, 1, 0, 0]), np.array([0.8, 0.4, 0.4, 0.1])) == pytest.approx(
            0.875
        )

    def test_perfect_and_inverted(self) -> None:
        assert roc_auc(np.array([0, 0, 1]), np.array([0.1, 0.2, 0.9])) == 1.0
        assert roc_auc(np.array([0, 0, 1]), np.array([0.9, 0.8, 0.1])) == 0.0

    def test_needs_both_classes(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            roc_auc(np.array([1, 1]), np.array([0.2, 0.3]))


def test_training_run_is_logged_and_registered(
    synthetic_frame: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _repo_state()
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    run = fit_baseline(synthetic_frame, FAST)
    bundle = save_bundle(run.trained, run.metadata, tmp_path / "bundle")

    run_id = tracking.log_training_run(run, bundle, register=True)

    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    logged = client.get_run(run_id)
    pr_auc = logged.data.metrics["valid_pr_auc"]
    assert pr_auc == pytest.approx(run.metadata["validation_metrics"]["average_precision"])
    assert 0.0 <= logged.data.metrics["valid_roc_auc"] <= 1.0
    assert "valid_precision_at_budget_1pct" in logged.data.metrics
    params = logged.data.params
    assert params["imbalance_method"] == "scale_pos_weight"
    assert float(params["scale_pos_weight"]) == pytest.approx(run.metadata["scale_pos_weight"])
    assert params["seed"] == str(FAST.seed) and params["train_end"]
    assert logged.data.tags["imbalance_method"] == "scale_pos_weight"
    assert logged.data.tags["git_commit"]
    artifacts = {a.path for a in client.list_artifacts(run_id, tracking.ARTIFACT_PATH)}
    assert artifacts == {"model/model.json", "model/metadata.json", "model/features.json"}
    assert (tmp_path / "mlruns").is_dir()  # artifacts next to the temp database

    champion = client.get_model_version_by_alias(tracking.MODEL_NAME, tracking.CHAMPION_ALIAS)
    assert champion.run_id == run_id and str(champion.version) == "1"
    assert _repo_state() == before  # nothing written to the repo's mlflow.db or mlruns/


def test_training_fraud_rate_uses_training_rows_only(synthetic_frame: pd.DataFrame) -> None:
    run = fit_baseline(synthetic_frame, FAST)
    split = run.metadata["split"]
    train_rows = synthetic_frame["TransactionDT"] < split["train_end"]  # train_end is exclusive
    assert int(train_rows.sum()) == split["train_rows"]
    expected = float(synthetic_frame.loc[train_rows, "isFraud"].mean())
    assert run.train_fraud_rate == pytest.approx(expected)
    assert len(run.y_valid) == split["valid_rows"] == len(run.valid_scores)
    assert np.isfinite(run.valid_scores).all()


class TestImbalanceWeight:
    def test_scale_pos_weight_is_negatives_over_positives(self) -> None:
        from src.models.compare import imbalance_weight

        assert imbalance_weight(pd.Series([0, 0, 0, 1]), "scale_pos_weight") == 3.0
        assert imbalance_weight(pd.Series([0, 0, 0, 1]), "none") == 1.0
        with pytest.raises(ValueError, match="unknown imbalance method"):
            imbalance_weight(pd.Series([0, 1]), "smote")

    def test_uses_training_rows_only(self, synthetic_frame: pd.DataFrame) -> None:
        """The stored weight equals the training-window ratio, not the full-data ratio."""
        df = synthetic_frame.copy()
        run = fit_baseline(df, FAST)
        train = df["TransactionDT"] < run.metadata["split"]["train_end"]
        df.loc[~train, "isFraud"] = 1  # validation labels changed: the weight must not move
        y_train = df.loc[train, "isFraud"]
        expected = float((y_train == 0).sum() / y_train.sum())
        assert run.metadata["scale_pos_weight"] == pytest.approx(expected)
        relabelled = fit_baseline(df, FAST)
        assert relabelled.metadata["scale_pos_weight"] == pytest.approx(expected)
        full = float((df["isFraud"] == 0).sum() / df["isFraud"].sum())
        assert full != pytest.approx(expected)

    def test_none_trains_with_weight_one(self, synthetic_frame: pd.DataFrame) -> None:
        from dataclasses import replace

        run = fit_baseline(synthetic_frame, replace(FAST, imbalance="none"))
        assert run.metadata["scale_pos_weight"] == 1.0
        assert run.metadata["config"]["imbalance"] == "none"


def test_champion_moves_only_beyond_the_seed_spread() -> None:
    from src.models.imbalance_comparison import RunResult, decide

    def runs(current: list[float], challenger: list[float]) -> list[RunResult]:
        return [
            RunResult(m, i, "r", 1.0, {"valid_pr_auc": v})
            for m, vals in (("scale_pos_weight", current), ("none", challenger))
            for i, v in enumerate(vals)
        ]

    small = decide(runs([0.250, 0.252, 0.248], [0.253, 0.255, 0.251]))  # +0.003 < spread 0.004
    assert not small.move_champion and small.seed_spread == pytest.approx(0.004)
    large = decide(runs([0.250, 0.252, 0.248], [0.260, 0.261, 0.259]))  # +0.010 > spread 0.004
    assert large.move_champion and large.difference == pytest.approx(0.010)
