from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.data.load import load_ieee_cis
from src.features.history import HistoryConfig
from src.models import compare, robustness
from src.models.metrics import (
    average_precision,
    day_block_bootstrap_ap_delta,
    select_threshold_max_f1,
    threshold_metrics,
)
from tests.conftest import write_split

FAST = compare.ComparisonConfig(
    max_rounds=40,
    early_stopping_rounds=10,
    bootstrap_resamples=20,
    xgb_params={
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "max_depth": 3,
        "eta": 0.3,
        "tree_method": "hist",
        "nthread": 1,
    },
)


def synthetic_split(raw_dir: Path, n: int = 3_000, seed: int = 0) -> Path:
    """Loader-shaped data with a learnable signal (high amounts on a few cards are fraud)."""
    rng = np.random.default_rng(seed)
    card1 = rng.integers(1000, 1060, n)
    amt = rng.gamma(2.0, 40.0, n).round(2)
    risky = (card1 % 7 == 0) & (amt > 60)
    y = (rng.random(n) < np.where(risky, 0.6, 0.03)).astype(int)
    txn = pd.DataFrame(
        {
            "TransactionID": np.arange(n) + 1,
            "isFraud": y,
            "TransactionDT": np.sort(rng.integers(86_400, 86_400 * 60, n)),
            "TransactionAmt": amt,
            "ProductCD": rng.choice(["W", "C", "H"], n),
            "card1": card1,
            "card2": rng.choice([100.0, 200.0, np.nan], n, p=[0.5, 0.45, 0.05]),
            "card3": 150.0,
            "card4": rng.choice(["visa", "mastercard"], n),
            "card5": 226.0,
            "card6": rng.choice(["debit", "credit"], n),
            "addr1": rng.choice([300.0, 310.0, np.nan], n),
            "addr2": 87.0,
            "dist1": rng.choice([1.0, 20.0, np.nan], n),
            "P_emaildomain": rng.choice(
                np.array(["gmail.com", "yahoo.com", None], dtype=object), n
            ),
            "R_emaildomain": rng.choice(np.array(["gmail.com", None], dtype=object), n),
            "D1": rng.choice([0.0, 5.0, 30.0, np.nan], n, p=[0.5, 0.2, 0.28, 0.02]),
        }
    )
    ids = txn["TransactionID"][rng.random(n) < 0.3].tolist()
    ident = pd.DataFrame(
        {
            "TransactionID": ids,
            "DeviceType": rng.choice(["mobile", "desktop"], len(ids)),
            "DeviceInfo": rng.choice(["iOS", "Windows", "SM-G"], len(ids)),
        }
    )
    return write_split(raw_dir, txn, ident)


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> pd.DataFrame:
    return load_ieee_cis(synthetic_split(tmp_path_factory.mktemp("raw")), usecols=[])


@pytest.fixture(scope="module")
def report(data: pd.DataFrame) -> dict[str, Any]:
    return compare.run_comparison(data, FAST)


class TestMetrics:
    def test_average_precision_hand_computed(self) -> None:
        # thresholds .9/.8/.7/.1 -> (P,R) = (1,.5), (.5,.5), (2/3,1), (.5,1)
        ap = average_precision(np.array([1, 0, 1, 0]), np.array([0.9, 0.8, 0.7, 0.1]))
        assert ap == pytest.approx(0.5 * 1 + 0.5 * 2 / 3)

    def test_tied_scores_form_one_threshold(self) -> None:
        assert average_precision(np.array([1, 0]), np.array([0.5, 0.5])) == pytest.approx(0.5)
        assert average_precision(np.array([0, 1]), np.array([0.5, 0.5])) == pytest.approx(0.5)

    def test_perfect_ranking(self) -> None:
        assert average_precision(np.array([0, 1, 1]), np.array([0.1, 0.8, 0.9])) == 1.0

    @pytest.mark.parametrize(
        ("y", "s"),
        [([0, 0], [0.1, 0.2]), ([1, 2], [0.1, 0.2]), ([1, 0], [np.nan, 0.2]), ([1], [0.1, 0.2])],
    )
    def test_invalid_inputs(self, y: list[Any], s: list[Any]) -> None:
        with pytest.raises(ValueError):
            average_precision(np.array(y), np.array(s))

    def test_threshold_maximises_f1(self) -> None:
        y, s = np.array([1, 1, 0, 0]), np.array([0.9, 0.8, 0.7, 0.1])
        assert select_threshold_max_f1(y, s) == 0.8

    def test_threshold_tie_prefers_fewer_alerts(self) -> None:
        # >=.9 -> F1 2/3; >=.5 -> TP2 FP2 -> F1 2/3: tie, choose the higher threshold.
        y, s = np.array([1, 0, 0, 1]), np.array([0.9, 0.7, 0.6, 0.5])
        assert select_threshold_max_f1(y, s) == 0.9

    def test_threshold_metrics_confusion(self) -> None:
        m = threshold_metrics(np.array([1, 0, 1, 0, 0]), np.array([0.9, 0.8, 0.2, 0.1, 0.6]), 0.6)
        assert (m.tp, m.fp, m.fn, m.tn) == (1, 2, 1, 1)
        assert m.flagged == 3 and m.flagged_rate == 0.6 and m.prevalence == 0.4
        assert m.precision == pytest.approx(1 / 3) and m.recall == 0.5

    def test_bootstrap_with_no_usable_resamples_returns_nan(self) -> None:
        y, s, day = np.array([1, 0]), np.array([0.6, 0.4]), np.array([0, 1])
        b = day_block_bootstrap_ap_delta(y, s, s, day, resamples=0, seed=0)
        assert b["resamples"] == 0 and np.isnan(b["delta_mean"]) and np.isnan(b["ci95_low"])

    def test_bootstrap_identical_scores_give_zero_delta(self) -> None:
        rng = np.random.default_rng(0)
        y = rng.integers(0, 2, 200)
        s = rng.random(200)
        day = np.repeat(np.arange(10), 20)
        b = day_block_bootstrap_ap_delta(y, s, s, day, resamples=30, seed=1)
        assert b["delta_mean"] == 0.0 and b["days"] == 10 and b["resamples"] == 30
        assert b == day_block_bootstrap_ap_delta(y, s, s, day, resamples=30, seed=1)


class TestFeatureFrames:
    def test_variants_share_columns_and_exclude_unsafe_inputs(self, data: pd.DataFrame) -> None:
        cols = compare.feature_columns()
        base = compare.build_model_frame(data, "baseline", HistoryConfig())
        cand = compare.build_model_frame(data, "candidate", HistoryConfig())
        assert list(base.columns) == list(cand.columns)
        assert base.index.equals(data.index) and cand.index.equals(data.index)
        assert not set(compare.EXCLUDED) & set(cols)
        assert "D1" not in base.columns and "D1" not in cand.columns

    def test_variants_differ_only_in_history_features(self, data: pd.DataFrame) -> None:
        cfg = HistoryConfig()
        base = compare.build_model_frame(data, "baseline", cfg)
        cand = compare.build_model_frame(data, "candidate", cfg)
        shared = [c for c in base.columns if c not in cfg.feature_names()]
        pd.testing.assert_frame_equal(base[shared], cand[shared])
        assert not base[cfg.feature_names()].equals(cand[cfg.feature_names()])

    def test_unknown_variant(self, data: pd.DataFrame) -> None:
        with pytest.raises(ValueError, match="variant"):
            compare.build_model_frame(data, "other", HistoryConfig())


class TestComparison:
    def test_shared_split_and_no_dropped_rows(
        self, data: pd.DataFrame, report: dict[str, Any]
    ) -> None:
        sp = report["split"]
        assert sp["train_rows"] + sp["valid_rows"] == len(data)
        expected_valid = int((data["TransactionDT"] >= sp["valid_start"]).sum())
        for v in compare.VARIANTS:
            t = report["results"][v]["at_threshold"]
            assert t["tp"] + t["fp"] + t["tn"] + t["fn"] == expected_valid == sp["valid_rows"]
            assert t["prevalence"] == pytest.approx(sp["valid_fraud_prevalence"])

    def test_model_learns_signal(self, report: dict[str, Any]) -> None:
        prevalence = report["split"]["valid_fraud_prevalence"]
        for v in compare.VARIANTS:
            assert report["results"][v]["average_precision"] > 2 * prevalence

    def test_validation_labels_do_not_affect_training(self, data: pd.DataFrame) -> None:
        """Permuting validation labels must leave thresholds, rounds and scores bit-identical."""
        from src.features.split import time_cutoff

        cutoff = time_cutoff(data["TransactionDT"], FAST.valid_fraction)
        valid = data["TransactionDT"] >= cutoff
        shuffled = data.copy()
        rng = np.random.default_rng(3)
        shuffled.loc[valid, "isFraud"] = rng.permutation(data.loc[valid, "isFraud"].to_numpy())
        cfg = HistoryConfig()
        cols = compare.feature_columns(cfg)
        outs = []
        for d in (data, shuffled):
            frame = compare.build_model_frame(d, "baseline", cfg)
            trained = compare.train_variant(frame[~valid], cols, FAST)
            scores = compare.score(trained, frame[valid].drop(columns="isFraud"), cols)
            outs.append((trained.threshold, trained.best_rounds, trained.scale_pos_weight, scores))
        assert outs[0][:3] == outs[1][:3]
        np.testing.assert_array_equal(outs[0][3], outs[1][3])

    def test_threshold_comes_from_training_holdout(self, data: pd.DataFrame) -> None:
        cfg = HistoryConfig()
        frame = compare.build_model_frame(data, "baseline", cfg)
        train = frame[frame["TransactionDT"] < frame["TransactionDT"].quantile(0.8)]
        trained = compare.train_variant(train, compare.feature_columns(cfg), FAST)
        # Threshold is an observed holdout score, so it must lie in (0, 1].
        assert 0 < trained.threshold <= 1
        assert trained.scale_pos_weight == pytest.approx(
            (train["isFraud"] == 0).sum() / train["isFraud"].sum()
        )

    def test_report_is_aggregate_only(self, report: dict[str, Any]) -> None:
        text = json.dumps(report)
        assert "|" not in text.replace("| ", "")  # no entity keys like 1111|321|...
        assert set(report["results"]) == set(compare.VARIANTS)
        assert report["features"]["model_inputs"] == compare.feature_columns()

    def test_markdown_renders(self, report: dict[str, Any]) -> None:
        md = compare.render_markdown(report)
        assert "baseline" in md and "candidate" in md and "PR-AUC" in md


def test_main_writes_reports(tmp_path: Path) -> None:
    raw = synthetic_split(tmp_path / "raw", n=1_500, seed=1)
    compare.main(raw, tmp_path / "out", FAST)
    saved = json.loads((tmp_path / "out" / "model_comparison.json").read_text(encoding="utf-8"))
    assert saved["split"]["valid_rows"] > 0
    assert (tmp_path / "out" / "model_comparison.md").is_file()


def test_seed_robustness_summary(data: pd.DataFrame) -> None:
    result = robustness.run_seeds(data, (1, 2), resamples=5, base=FAST)
    assert result["seeds"] == [1, 2] and set(result["per_seed"]) == {1, 2}
    for s in result["per_seed"].values():
        assert s["ap_delta"] == pytest.approx(
            s["candidate"]["average_precision"] - s["baseline"]["average_precision"]
        )
    deltas = [s["ap_delta"] for s in result["per_seed"].values()]
    assert result["ap_delta_min"] == min(deltas) and result["ap_delta_max"] == max(deltas)


def test_same_seed_is_deterministic(data: pd.DataFrame) -> None:
    a = compare.run_comparison(data, FAST)["results"]
    b = compare.run_comparison(data, FAST)["results"]
    for v in compare.VARIANTS:
        assert a[v]["average_precision"] == b[v]["average_precision"]
        assert a[v]["at_threshold"] == b[v]["at_threshold"]


class TestEncoderFitScope:
    """Encoders must be fitted on training rows only (labels are not needed to leak features)."""

    MARKER = "validation-only.example"

    def with_validation_only_category(
        self, data: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.Series[Any]]:
        from src.features.split import time_cutoff

        valid = data["TransactionDT"] >= time_cutoff(data["TransactionDT"], FAST.valid_fraction)
        marked = data.copy()
        domain = marked["P_emaildomain"].astype(object)
        domain[valid.to_numpy()] = self.MARKER
        marked["P_emaildomain"] = domain
        return marked, valid

    def test_train_variant_vocab_excludes_validation_only_values(self, data: pd.DataFrame) -> None:
        marked, valid = self.with_validation_only_category(data)
        cfg = HistoryConfig()
        frame = compare.build_model_frame(marked, "baseline", cfg)
        trained = compare.train_variant(frame[~valid], compare.feature_columns(cfg), FAST)
        vocab = trained.encoder.vocab_
        assert vocab is not None and self.MARKER not in vocab["P_emaildomain"]
        codes = trained.encoder.transform(frame[valid])["P_emaildomain"]
        assert (codes == -1).all()  # unseen at training time -> the documented UNSEEN code

    def test_saved_bundle_vocab_excludes_validation_only_values(self, data: pd.DataFrame) -> None:
        from src.models.train import train_baseline

        marked, _ = self.with_validation_only_category(data)
        _, metadata = train_baseline(marked, FAST)
        assert self.MARKER not in metadata["encoder"]["vocab"]["P_emaildomain"]


class TestLeakageAudit:
    def test_entity_overlap_hand_computed(self) -> None:
        from src.models.leakage_audit import entity_overlap

        train_key = pd.Series(["a", "a", "b", None])
        train_y = pd.Series([0, 1, 0, 1])  # "a" had fraud; the None-key fraud is ignored
        valid_key = pd.Series(["a", "b", "c", None])
        valid_y = pd.Series([1, 0, 0, 0])
        o = entity_overlap(train_key, valid_key, train_y, valid_y)
        assert o["valid_rows_entity_seen_in_train"] == 0.5  # a, b of 4 rows
        assert o["valid_entities"] == 3 and o["valid_entities_seen_in_train"] == round(2 / 3, 4)
        assert o["valid_rows_entity_had_train_fraud"] == 0.25
        assert o["valid_fraud_rate_if_entity_had_train_fraud"] == 1.0
        assert o["valid_fraud_rate_otherwise"] == 0.0

    def test_audit_runs_and_removes_only_identifier_inputs(self, data: pd.DataFrame) -> None:
        from src.models.leakage_audit import IDENTIFIER_INPUTS, run_audit

        r = run_audit(data, FAST)
        a = r["identifier_ablation"]
        assert a["all_inputs"]["n_inputs"] - a["without_identifier_inputs"]["n_inputs"] == len(
            IDENTIFIER_INPUTS
        )
        assert set(r["entity_overlap"]) == {"card_id", "uid"}
        assert "|" not in json.dumps(r)  # no entity key strings in the report


class TestAlertBudget:
    def test_hand_computed_top_k(self) -> None:
        from src.models.metrics import alert_budget_metrics

        y = np.array([1, 0, 1, 0, 0, 0, 1, 0, 0, 0])
        s = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05])
        r20, r50 = alert_budget_metrics(y, s, [0.2, 0.5])
        assert (r20["alerts"], r20["tp"], r20["fp"]) == (2, 1, 1)
        assert r20["precision"] == 0.5 and r20["recall"] == pytest.approx(1 / 3)
        assert r20["cutoff_score"] == 0.8 and r20["boundary_ties"] == 1
        assert r50["alerts"] == 5 and r50["tp"] == 2 and r50["recall"] == pytest.approx(2 / 3)

    def test_ties_at_cutoff_are_reported_and_deterministic(self) -> None:
        from src.models.metrics import alert_budget_metrics

        y = np.array([0, 1, 0, 1])
        s = np.array([0.9, 0.5, 0.5, 0.5])  # three rows tie at the cut-off
        (r,) = alert_budget_metrics(y, s, [0.5])  # 2 alerts: 0.9 + first tied row (stable)
        assert r["alerts"] == 2 and r["tp"] == 1
        assert r["boundary_ties"] == 3 and r["tied_excluded"] == 2
        assert alert_budget_metrics(y, s, [0.5]) == [r]

    def test_small_budget_rounds_up_and_float_artefacts(self) -> None:
        from src.models.metrics import alert_budget_metrics

        s = np.linspace(1, 0, 100)
        y = np.zeros(100, dtype=int)
        y[0] = 1
        out = alert_budget_metrics(y, s, [0.001, 0.01, 0.07])
        assert [r["alerts"] for r in out] == [1, 1, 7]  # 0.07 * 100 must not become 8

    def test_empty_group_and_no_positives(self) -> None:
        from src.models.metrics import alert_budget_metrics

        (empty,) = alert_budget_metrics(np.array([], dtype=int), np.array([]), [0.05])
        assert empty["alerts"] == 0 and empty["precision"] is None and empty["recall"] is None
        (neg,) = alert_budget_metrics(np.zeros(4, dtype=int), np.array([0.4, 0.3, 0.2, 0.1]), [0.5])
        assert neg["precision"] == 0.0 and neg["recall"] is None and neg["fp"] == 2

    def test_alert_set_does_not_depend_on_labels(self) -> None:
        from src.models.metrics import alert_budget_metrics

        rng = np.random.default_rng(0)
        s = rng.random(200)
        a = alert_budget_metrics(rng.integers(0, 2, 200), s, [0.1])[0]
        b = alert_budget_metrics(rng.integers(0, 2, 200), s, [0.1])[0]
        assert (a["alerts"], a["cutoff_score"], a["boundary_ties"]) == (
            b["alerts"],
            b["cutoff_score"],
            b["boundary_ties"],
        )

    @pytest.mark.parametrize(
        ("y", "s", "rates"),
        [
            ([0, 1], [0.1, 0.2], [0.0]),
            ([0, 1], [0.1, 0.2], [1.5]),
            ([0, 1], [0.1, 0.2], []),
            ([0, 2], [0.1, 0.2], [0.5]),
            ([0, 1], [0.1, np.nan], [0.5]),
            ([0, 1], [0.1], [0.5]),
        ],
    )
    def test_invalid_inputs(self, y: list[Any], s: list[Any], rates: list[Any]) -> None:
        from src.models.metrics import alert_budget_metrics

        with pytest.raises(ValueError):
            alert_budget_metrics(np.array(y), np.array(s), rates)
