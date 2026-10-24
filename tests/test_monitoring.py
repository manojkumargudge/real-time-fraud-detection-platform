"""Drift job: config, trigger rule, label-delay filter, promotion, overlap guard, end-to-end run."""

from __future__ import annotations

import copy
import math
import tomllib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.data.load import KEY, LABEL
from src.features.entity import SECONDS_PER_DAY
from src.features.history import TIME, HistoryConfig
from src.models.compare import build_model_frame
from src.monitoring import run_drift as job
from src.monitoring.config import (
    DEFAULT_CONFIG,
    ConfigError,
    DriftConfig,
    load_config,
    parse_config,
)
from src.monitoring.report_store import DriftReportRow
from src.monitoring.retrain import (
    CHAMPION,
    PRODUCTION,
    ProtectedAliasError,
    apply_promotion,
)
from src.monitoring.rules import CheckResults, decide_promotion, evaluate_trigger
from src.monitoring.windows import (
    Window,
    WindowOverlapError,
    day_window,
    label_cutoff,
    plan_retrain,
    usable_labels,
)
from src.serving.model import ModelBundle

DAY = SECONDS_PER_DAY


def config_dict() -> dict[str, Any]:
    return tomllib.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def config_with(**sections: dict[str, Any]) -> DriftConfig:
    data = config_dict()
    for name, values in sections.items():
        data[name] = {**data[name], **values}
    return parse_config(data)


CFG = load_config()


# --- config -----------------------------------------------------------------------------------


class TestConfig:
    def test_repo_config_is_valid(self) -> None:
        assert CFG.labels.delay_days == 30
        assert CFG.promotion.resamples == 1000

    @pytest.mark.parametrize(
        ("section", "key"),
        [("labels", "delay_days"), ("promotion", "margin"), ("feature_drift", "share_threshold")],
    )
    def test_missing_value_fails(self, section: str, key: str) -> None:
        data = config_dict()
        del data[section][key]
        with pytest.raises(ConfigError, match=key):
            parse_config(data)

    def test_missing_section_fails(self) -> None:
        data = config_dict()
        del data["retrain"]
        with pytest.raises(ConfigError, match="retrain"):
            parse_config(data)

    @pytest.mark.parametrize(
        ("section", "values"),
        [
            ("feature_drift", {"share_threshold": 1.5}),
            ("labels", {"delay_days": 0}),
            ("labels", {"delay_days": "30"}),  # strings are not coerced
            ("retrain", {"imbalance": "smote"}),
            ("promotion", {"unit": "week"}),
            ("promotion", {"resamples": 10}),
            ("prediction_drift", {"method": "ks"}),
            ("labels", {"typo_key": 1}),  # unknown keys are rejected
        ],
    )
    def test_invalid_value_fails(self, section: str, values: dict[str, Any]) -> None:
        with pytest.raises(ConfigError):
            config_with(**{section: values})

    def test_missing_or_broken_file_fails(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.toml")
        bad = tmp_path / "bad.toml"
        bad.write_text("[labels\ndelay_days = 30\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(bad)


# --- trigger rule -----------------------------------------------------------------------------


def checks(**overrides: Any) -> CheckResults:
    quiet: dict[str, Any] = {
        "drifted_feature_share": 0.1,
        "prediction_drift_detected": False,
        "prediction_drift_score": 0.05,
        "delayed_pr_auc": 0.29,
        "champion_valid_pr_auc": 0.30,
    }
    return CheckResults(**{**quiet, **overrides})


class TestTrigger:
    def test_nothing_fires_under_all_thresholds(self) -> None:
        decision = evaluate_trigger(checks(), CFG)
        assert not decision.fired and decision.reasons == []

    def test_feature_drift_alone_fires(self) -> None:
        decision = evaluate_trigger(checks(drifted_feature_share=0.51), CFG)
        assert decision.fired and len(decision.reasons) == 1
        assert decision.reasons[0].startswith("feature drift")

    def test_share_equal_to_threshold_does_not_fire(self) -> None:
        assert not evaluate_trigger(checks(drifted_feature_share=0.5), CFG).fired

    def test_prediction_drift_alone_fires(self) -> None:
        decision = evaluate_trigger(
            checks(prediction_drift_detected=True, prediction_drift_score=0.3), CFG
        )
        assert decision.fired and decision.reasons[0].startswith("prediction drift")

    def test_performance_drop_alone_fires(self) -> None:
        decision = evaluate_trigger(checks(delayed_pr_auc=0.2499), CFG)  # 0.30 - 0.05 = 0.25
        assert decision.fired and decision.reasons[0].startswith("performance")
        assert not evaluate_trigger(checks(delayed_pr_auc=0.25), CFG).fired

    def test_unavailable_performance_does_not_fire(self) -> None:
        assert not evaluate_trigger(checks(delayed_pr_auc=None), CFG).fired

    def test_all_conditions_are_reported(self) -> None:
        decision = evaluate_trigger(
            checks(drifted_feature_share=0.9, prediction_drift_detected=True, delayed_pr_auc=0.1),
            CFG,
        )
        assert len(decision.reasons) == 3


# --- label-delay filter -----------------------------------------------------------------------


class TestLabelDelay:
    def test_cutoff_is_now_minus_delay_in_transaction_time(self) -> None:
        assert label_cutoff(100 * DAY, 30) == 70 * DAY

    def test_no_label_newer_than_cutoff_and_edge_is_included(self) -> None:
        cutoff = 70 * DAY
        frame = pd.DataFrame(
            {
                KEY: [1, 2, 3, 4],
                TIME: [cutoff - 1, cutoff, cutoff + 1, cutoff + DAY],
                LABEL: [0, 1, 1, 1],
            }
        )
        kept = usable_labels(frame, cutoff)
        assert kept[KEY].tolist() == [1, 2]  # exactly at the cutoff is usable
        assert (kept[TIME] <= cutoff).all()


# --- promotion --------------------------------------------------------------------------------


def boot(low: float, mean: float, high: float) -> dict[str, float | int]:
    return {"resamples": 1000, "days": 7, "delta_mean": mean, "ci95_low": low, "ci95_high": high}


class FakeRegistry:
    def __init__(self) -> None:
        self.aliases = {CHAMPION: "2", PRODUCTION: "2"}
        self.calls: list[tuple[str, str]] = []

    def alias_version(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    def set_alias(self, alias: str, version: str) -> None:
        if alias == PRODUCTION:
            raise ProtectedAliasError(alias)
        self.calls.append((alias, version))
        self.aliases[alias] = version


class TestPromotion:
    def test_challenger_worse_keeps_champion(self) -> None:
        decision = decide_promotion(
            challenger_pr_auc=0.25,
            champion_pr_auc=0.30,
            bootstrap=boot(-0.08, -0.05, -0.02),
            margin=0.01,
        )
        registry = FakeRegistry()
        aliases = apply_promotion(decision, registry, "3")
        assert not decision.promote and "<= champion" in decision.reason
        assert registry.calls == [] and aliases["champion_after"] == "2"
        assert aliases["production"] == "2"

    def test_better_but_ci_under_margin_keeps_champion(self) -> None:
        decision = decide_promotion(
            challenger_pr_auc=0.32,
            champion_pr_auc=0.30,
            bootstrap=boot(0.005, 0.02, 0.04),
            margin=0.01,
        )
        registry = FakeRegistry()
        apply_promotion(decision, registry, "3")
        assert not decision.promote and "lower bound" in decision.reason
        assert registry.aliases == {CHAMPION: "2", PRODUCTION: "2"}

    def test_clearly_better_moves_champion_only(self) -> None:
        decision = decide_promotion(
            challenger_pr_auc=0.36,
            champion_pr_auc=0.30,
            bootstrap=boot(0.03, 0.06, 0.09),
            margin=0.01,
        )
        registry = FakeRegistry()
        aliases = apply_promotion(decision, registry, "3")
        assert decision.promote
        assert registry.calls == [(CHAMPION, "3")]
        assert aliases == {"champion_before": "2", "champion_after": "3", "production": "2"}

    def test_decision_logs_point_estimates_and_ci(self) -> None:
        d = decide_promotion(
            challenger_pr_auc=0.36,
            champion_pr_auc=0.30,
            bootstrap=boot(0.03, 0.06, 0.09),
            margin=0.01,
        ).to_dict()
        assert d["challenger_pr_auc"] == 0.36 and d["champion_pr_auc"] == 0.30
        assert (d["ci95_low"], d["ci95_high"], d["delta_mean"]) == (0.03, 0.09, 0.06)

    def test_no_usable_bootstrap_keeps_champion(self) -> None:
        nan = float("nan")
        decision = decide_promotion(
            challenger_pr_auc=0.4, champion_pr_auc=0.3, bootstrap=boot(nan, nan, nan), margin=0.0
        )
        assert not decision.promote

    def test_a_moved_production_alias_fails_the_job(self) -> None:
        class Sneaky(FakeRegistry):
            def set_alias(self, alias: str, version: str) -> None:
                super().set_alias(alias, version)
                self.aliases[PRODUCTION] = version  # simulate something else moving it

        decision = decide_promotion(
            challenger_pr_auc=0.36,
            champion_pr_auc=0.30,
            bootstrap=boot(0.03, 0.06, 0.09),
            margin=0.01,
        )
        with pytest.raises(ProtectedAliasError, match="production alias changed"):
            apply_promotion(decision, Sneaky(), "3")


# --- overlap guard ----------------------------------------------------------------------------


class TestOverlapGuard:
    def test_holdout_after_champion_training_is_accepted(self) -> None:
        w = plan_retrain(
            champion_train=Window("champion_train", 1 * DAY, 141 * DAY - 1),
            train_dt_min=1 * DAY,
            cutoff=153 * DAY,
            holdout_days=7,
        )
        assert w.holdout.start == 146 * DAY + 1 and w.holdout.end == 153 * DAY
        assert w.challenger_train.end == w.holdout.start - 1
        assert not w.challenger_train.overlaps(w.holdout)

    def test_champion_training_overlapping_holdout_fails(self) -> None:
        with pytest.raises(WindowOverlapError, match="champion_train"):
            plan_retrain(
                champion_train=Window("champion_train", 1 * DAY, 141 * DAY - 1),
                train_dt_min=1 * DAY,
                cutoff=145 * DAY,  # holdout (138, 145] overlaps champion training
                holdout_days=7,
            )

    def test_closed_window_edges(self) -> None:
        a, b = Window("a", 0, 10), Window("b", 10, 20)
        assert a.overlaps(b)  # sharing one second is an overlap
        assert not Window("a", 0, 9).overlaps(b)
        assert day_window("d", 1, 2) == Window("d", DAY, 2 * DAY - 1)
        with pytest.raises(ValueError, match="before start"):
            Window("bad", 5, 4)


# --- end-to-end on synthetic data (Postgres write captured) -------------------------------------


@pytest.fixture(scope="module")
def scenario(synthetic_frame: pd.DataFrame, bundle_dir: Path) -> dict[str, Any]:
    """Scored = every row after the champion's training window, scored by the champion."""
    champion = ModelBundle.load(bundle_dir)
    frame = build_model_frame(synthetic_frame, "baseline", HistoryConfig())
    train_end = int(champion.metadata["split"]["train_end"])
    after = frame.loc[frame[TIME] >= train_end]
    scored = pd.DataFrame(
        {
            "transaction_id": after[KEY].to_numpy(),
            "transaction_dt": after[TIME].to_numpy(),
            "score": champion.predict(after),
            "model_version": champion.model_version,
        }
    )
    return {"raw": synthetic_frame, "scored": scored, "champion": champion}


def small_cfg(**sections: dict[str, Any]) -> DriftConfig:
    """The repo config with windows sized for 60 days of synthetic data."""
    base: dict[str, dict[str, Any]] = {
        "labels": {"delay_days": 3},
        "reference": {"days": 10},
        "current": {"default_days": 5},
        "performance": {"window_days": 6, "min_positives": 1},
        "retrain": {"holdout_days": 2},
        "promotion": {"resamples": 200},
    }
    for name, values in sections.items():
        base[name] = {**base.get(name, {}), **values}
    return config_with(**base)


def run_captured(
    scenario: dict[str, Any],
    cfg: DriftConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw: pd.DataFrame | None = None,
    **opts: Any,
) -> DriftReportRow:
    rows: list[DriftReportRow] = []

    def capture(dsn: str, row: DriftReportRow) -> int:
        rows.append(row)
        return len(rows)

    monkeypatch.setattr(job, "insert_report", capture)
    inputs = job.JobInputs(
        raw=scenario["raw"] if raw is None else raw,
        scored=scenario["scored"],
        champion=scenario["champion"],
    )
    options = job.JobOptions(reports_dir=tmp_path, **{"retrain": False, **opts})
    job.run_job(inputs, cfg, options, dsn="unused")
    return rows[0]


def test_job_writes_a_complete_row_and_html(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = run_captured(scenario, small_cfg(), tmp_path, monkeypatch)
    assert row.current_rows > 0 and row.reference_rows > 0
    assert row.now_dt == int(scenario["scored"]["transaction_dt"].max())
    assert row.label_cutoff_dt == row.now_dt - 3 * DAY
    assert row.performance_end_dt == row.label_cutoff_dt
    assert 0.0 <= row.drifted_feature_share <= 1.0
    assert row.feature_count == len(scenario["champion"].feature_columns)
    assert not row.synthetic and row.retrain_run_id is None
    assert set(row.windows) >= {"champion_train", "challenger_train", "holdout", "current"}
    html = Path(row.report_path).read_text(encoding="utf-8")
    assert "<title>Drift report" in html


def test_labels_after_the_cutoff_never_change_the_result(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poison every label newer than the cutoff: the delayed-label check must not move."""
    cfg = small_cfg()
    clean = run_captured(scenario, cfg, tmp_path, monkeypatch)
    poisoned_raw = scenario["raw"].copy()
    late = poisoned_raw[TIME] > clean.label_cutoff_dt
    assert late.any()
    poisoned_raw.loc[late, LABEL] = 1 - poisoned_raw.loc[late, LABEL]
    poisoned = run_captured(scenario, cfg, tmp_path, monkeypatch, raw=poisoned_raw)
    assert poisoned.delayed_rows == clean.delayed_rows
    assert poisoned.delayed_positives == clean.delayed_positives
    assert poisoned.delayed_pr_auc == clean.delayed_pr_auc


def test_retraining_only_sees_labels_up_to_the_cutoff(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_retrain(labelled: pd.DataFrame, windows: Any, *args: Any, **kwargs: Any) -> Any:
        seen["max_dt"] = int(labelled[TIME].max())
        seen["holdout_end"] = windows.holdout.end
        raise RuntimeError("stop after capture")

    import src.monitoring.retrain as retrain_mod

    monkeypatch.setattr(retrain_mod, "retrain_and_compare", fake_retrain)
    monkeypatch.setattr(retrain_mod, "MlflowRegistry", lambda uri: None)
    cfg = small_cfg(feature_drift={"share_threshold": 0.01})  # make sure it fires
    with pytest.raises(RuntimeError, match="stop after capture"):
        run_captured(scenario, cfg, tmp_path, monkeypatch, retrain=True, mlflow_uri="unused")
    now = int(scenario["scored"]["transaction_dt"].max())
    assert seen["max_dt"] <= now - 3 * DAY == seen["holdout_end"]


def test_overlap_fails_the_job_when_retraining_is_enabled(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = small_cfg(labels={"delay_days": 20})  # cutoff falls inside the champion's training
    with pytest.raises(WindowOverlapError):
        run_captured(scenario, cfg, tmp_path, monkeypatch, retrain=True)
    row = run_captured(scenario, cfg, tmp_path, monkeypatch, retrain=False)  # checks only
    assert row.retrain_run_id is None


def test_synthetic_shift_is_marked_and_detected(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = small_cfg()
    row = run_captured(scenario, cfg, tmp_path, monkeypatch, synthetic_amount_factor=3.0)
    assert row.synthetic and row.synthetic_note and "x3.0" in row.synthetic_note
    assert row.report_path.endswith("_synthetic.html")
    assert "<title>SYNTHETIC=TRUE" in Path(row.report_path).read_text(encoding="utf-8")
    amount = [f for f in row.top_drifted_features if f["feature"] == "TransactionAmt"]
    assert amount, row.top_drifted_features


def test_bootstrap_unit_transaction_resamples_rows() -> None:
    from src.monitoring.retrain import bootstrap_delta

    rng = np.random.default_rng(0)
    y = (rng.random(400) < 0.2).astype(np.int64)
    a = rng.random(400)
    b = np.where(y == 1, a + 0.5, a)  # b is clearly better
    dt = np.arange(400) * 60
    cfg = config_with(promotion={"unit": "transaction", "resamples": 200})
    out = bootstrap_delta(y, a, b, dt, cfg)
    assert out["days"] == 400 and out["resamples"] == 200
    assert float(out["ci95_low"]) > 0


def test_mlflow_registry_refuses_production(tmp_path: Path) -> None:
    pytest.importorskip("mlflow")
    from src.monitoring.retrain import MlflowRegistry

    registry = MlflowRegistry(f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}")
    with pytest.raises(ProtectedAliasError):
        registry.set_alias(PRODUCTION, "1")


def test_reference_scores_are_out_of_sample(scenario: dict[str, Any]) -> None:
    """The recipe refit never sees reference-window rows."""
    champion = scenario["champion"]
    frame = build_model_frame(scenario["raw"], "baseline", HistoryConfig())
    train = job.champion_train_window(champion)
    reference = Window("reference", train.end + 1 - 10 * DAY, train.end)
    scores = job.reference_scores(frame, champion, train, reference)
    assert len(scores) == int(reference.mask(frame[TIME]).sum())
    assert not math.isnan(float(scores.mean()))


def test_config_section_copies_are_independent() -> None:
    data = copy.deepcopy(config_dict())
    data["labels"]["delay_days"] = 5
    assert parse_config(data).labels.delay_days == 5 and CFG.labels.delay_days == 30


# --- top-gain feature rule (added after runs 1-3) -------------------------------------------


class TestTopGainRule:
    def test_drifted_top_feature_alone_fires(self) -> None:
        top = ("TransactionAmt", "card1", "addr1", "uid_prior_txn_count", "card6")
        decision = evaluate_trigger(
            checks(top_gain_features=top, drifted_top_gain_features=("TransactionAmt",)), CFG
        )
        assert decision.fired and len(decision.reasons) == 1
        assert decision.reasons[0].startswith("top-feature drift: TransactionAmt")

    def test_no_drifted_top_feature_does_not_fire(self) -> None:
        top = ("TransactionAmt", "card1", "addr1", "uid_prior_txn_count", "card6")
        assert not evaluate_trigger(checks(top_gain_features=top), CFG).fired

    def test_top_features_are_read_from_the_champion_booster(self, bundle_dir: Path) -> None:
        champion = ModelBundle.load(bundle_dir)
        raw_gains = champion.booster.get_score(importance_type="gain")
        gains = {f: v for f, v in raw_gains.items() if isinstance(v, float)}
        top = job.top_gain_features(champion, 5)
        assert len(top) == 5 and set(top) <= set(champion.feature_columns)
        values = [gains[f] for f in top]
        assert values == sorted(values, reverse=True)
        assert all(v <= values[-1] for f, v in gains.items() if f not in top)

    def test_row_stores_top_features_and_the_drifted_ones(
        self, scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = run_captured(
            scenario, small_cfg(), tmp_path, monkeypatch, synthetic_amount_factor=3.0
        )
        assert row.top_gain_features == job.top_gain_features(scenario["champion"], 5)
        assert set(row.drifted_top_gain_features) <= set(row.top_gain_features)
        flagged = {f["feature"] for f in row.top_drifted_features}
        for f in row.drifted_top_gain_features:
            assert f in flagged or len(flagged) == CFG.feature_drift.top_n
        if row.drifted_top_gain_features:
            assert row.triggered and any(r.startswith("top-feature") for r in row.trigger_reasons)


# --- --force-retrain ------------------------------------------------------------------------


def quiet_cfg() -> DriftConfig:
    """Thresholds no check can reach, so nothing fires on its own."""
    return small_cfg(
        feature_drift={"column_threshold": 0.99, "share_threshold": 0.99},
        prediction_drift={"threshold": 1e9},
        performance={"max_drop": 0.99},
    )


def test_force_retrain_runs_the_retrain_path_and_marks_the_row(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.monitoring.retrain as retrain_mod
    from src.monitoring.retrain import RetrainResult

    calls: list[dict[str, str]] = []

    def fake_retrain(labelled: pd.DataFrame, windows: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["tags"])
        decision = decide_promotion(
            challenger_pr_auc=0.2, champion_pr_auc=0.3, bootstrap=boot(-0.2, -0.1, 0.0), margin=0.01
        )
        aliases: dict[str, str | None] = {
            "champion_before": "1",
            "champion_after": "1",
            "production": "1",
        }
        return RetrainResult("run-x", "2", decision, aliases, 10, 2, boot(-0.2, -0.1, 0.0))

    monkeypatch.setattr(retrain_mod, "retrain_and_compare", fake_retrain)
    monkeypatch.setattr(retrain_mod, "MlflowRegistry", lambda uri: None)
    row = run_captured(
        scenario,
        quiet_cfg(),
        tmp_path,
        monkeypatch,
        retrain=True,
        force_retrain=True,
        mlflow_uri="unused",
    )
    assert not row.triggered and row.forced
    assert calls and calls[0]["forced"] == "true"
    assert row.retrain_run_id == "run-x" and row.challenger_version == "2"
    assert row.promotion_decision
    assert row.promotion_decision.startswith("forced retrain (no check fired):")
    assert row.promotion and row.promotion["aliases"]["production"] == "1"


def test_forced_is_stored_even_when_a_check_also_fired(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.monitoring.retrain as retrain_mod
    from src.monitoring.retrain import RetrainResult

    def fake_retrain(*args: Any, **kwargs: Any) -> Any:
        decision = decide_promotion(
            challenger_pr_auc=0.2, champion_pr_auc=0.3, bootstrap=boot(-0.2, -0.1, 0.0), margin=0.01
        )
        aliases: dict[str, str | None] = {"champion_before": "1", "champion_after": "1"}
        return RetrainResult("run-y", "3", decision, aliases, 10, 2, boot(-0.2, -0.1, 0.0))

    monkeypatch.setattr(retrain_mod, "retrain_and_compare", fake_retrain)
    monkeypatch.setattr(retrain_mod, "MlflowRegistry", lambda uri: None)
    cfg = small_cfg(feature_drift={"share_threshold": 0.01})  # fires on its own
    row = run_captured(
        scenario, cfg, tmp_path, monkeypatch, retrain=True, force_retrain=True, mlflow_uri="unused"
    )
    assert row.triggered and row.forced
    assert row.promotion_decision and row.promotion_decision.startswith("forced retrain: ")


def test_without_force_a_quiet_run_does_not_retrain(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.monitoring.retrain as retrain_mod

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("retrain must not run")

    monkeypatch.setattr(retrain_mod, "retrain_and_compare", boom)
    row = run_captured(scenario, quiet_cfg(), tmp_path, monkeypatch, retrain=True)
    assert not row.triggered and not row.forced and row.retrain_run_id is None


def test_force_retrain_with_no_retrain_is_rejected(
    scenario: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="force-retrain"):
        run_captured(
            scenario, quiet_cfg(), tmp_path, monkeypatch, retrain=False, force_retrain=True
        )
    with pytest.raises(SystemExit):
        job.main(["--force-retrain", "--no-retrain"])


# --- challenger reuse guard -----------------------------------------------------------------


def test_same_training_rows_reuse_the_challenger(
    synthetic_frame: pd.DataFrame, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("mlflow")
    import logging

    from mlflow.tracking import MlflowClient

    from src.models.tracking import log_training_run
    from src.models.train import fit_baseline, save_bundle
    from src.monitoring.retrain import (
        TRAIN_FINGERPRINT_TAG,
        MlflowRegistry,
        retrain_and_compare,
    )
    from tests.test_models import FAST

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    run = fit_baseline(synthetic_frame, FAST)
    bundle = save_bundle(run.trained, run.metadata, tmp_path / "champion")
    log_training_run(run, bundle, imbalance_method="none", register=True, uri=uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    client.set_registered_model_alias("fraud-xgboost", "production", "1")
    registry = MlflowRegistry(uri)
    champion = ModelBundle.load(bundle)

    frame = build_model_frame(synthetic_frame, "baseline", HistoryConfig())
    now = int(frame[TIME].max())
    cfg = small_cfg()
    cutoff = label_cutoff(now, cfg.labels.delay_days)
    labelled = usable_labels(frame, cutoff)
    train_window = job.champion_train_window(champion)

    def compare(holdout_days: int) -> Any:
        windows = plan_retrain(
            champion_train=train_window,
            train_dt_min=train_window.start,
            cutoff=cutoff,
            holdout_days=holdout_days,
        )
        return retrain_and_compare(labelled, windows, champion, cfg, registry, tags={})

    def versions() -> list[str]:
        return sorted(str(m.version) for m in client.search_model_versions("name='fraud-xgboost'"))

    first = compare(2)
    assert not first.reused and first.challenger_version == "2"
    assert client.get_run(first.run_id).data.tags[TRAIN_FINGERPRINT_TAG]

    with caplog.at_level(logging.INFO, logger="src.monitoring.retrain"):
        second = compare(2)  # same window, same rows -> no new fit, no new version
    assert second.reused and second.challenger_version == "2" and second.run_id == first.run_id
    assert "reused challenger v2" in caplog.text
    assert versions() == ["1", "2"]
    assert second.decision.challenger_pr_auc == first.decision.challenger_pr_auc

    third = compare(3)  # different training window -> retrained
    assert not third.reused and third.challenger_version == "3"
    assert versions() == ["1", "2", "3"]
    assert str(client.get_model_version_by_alias("fraud-xgboost", "production").version) == "1"


def test_fingerprint_changes_with_rows_labels_and_window() -> None:
    from src.monitoring.retrain import training_fingerprint

    train = pd.DataFrame({KEY: [3, 1, 2], LABEL: [0, 1, 0]})
    base = training_fingerprint(train, 0, 100, CFG)
    assert training_fingerprint(train.sample(frac=1, random_state=1), 0, 100, CFG) == base
    flipped = train.assign(**{LABEL: [1, 1, 0]})
    assert training_fingerprint(flipped, 0, 100, CFG) != base
    assert training_fingerprint(train.iloc[:2], 0, 100, CFG) != base
    assert training_fingerprint(train, 0, 101, CFG) != base
    other_seed = config_with(retrain={"seed": 7})
    assert training_fingerprint(train, 0, 100, other_seed) != base
