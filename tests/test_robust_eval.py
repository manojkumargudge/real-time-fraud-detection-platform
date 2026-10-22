from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.array_types import BoolArray
from src.data.load import DEFAULT_RAW_DIR, load_transactions
from src.features.history import HistoryConfig
from src.features.split import time_cutoff
from src.models import robust_eval as re_
from src.models.compare import (
    ComparisonConfig,
    _id_digest,
    build_model_frame,
    feature_columns,
    score,
    train_variant,
)
from tests.test_models import FAST

DAY = 86_400
SYNTH_WINDOW_DAYS = 5  # synthetic data spans ~60 days; the real study uses 30-day windows
# Long enough for the floor and fixed policies to be meaningful on synthetic data.
LONG = replace(FAST, max_rounds=120, early_stopping_rounds=10)
COLS = feature_columns(HistoryConfig())


@pytest.fixture(scope="module")
def frame(synthetic_frame: pd.DataFrame) -> pd.DataFrame:
    return build_model_frame(synthetic_frame, "baseline", HistoryConfig())


@pytest.fixture(scope="module")
def windows(frame: pd.DataFrame) -> list[re_.ValidationWindow]:
    return re_.define_windows(frame["TransactionDT"], window_days=SYNTH_WINDOW_DAYS)


class TestWindows:
    def test_three_contiguous_windows_ending_at_the_m2_cutoff(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow]
    ) -> None:
        w1, w2, w3 = windows
        assert [w.name for w in windows] == ["W1", "W2", "W3"]
        assert w3.start == time_cutoff(frame["TransactionDT"], 0.2) and w3.end is None
        assert w1.end == w2.start and w2.end == w3.start
        assert w2.start - w1.start == w3.start - w2.start == SYNTH_WINDOW_DAYS * DAY

    @pytest.mark.parametrize("gap_days", [0, 2])
    def test_split_is_strictly_chronological_and_disjoint(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow], gap_days: int
    ) -> None:
        for w in windows:
            train, valid = re_.window_split(frame, w, gap_days * DAY)
            assert train["TransactionDT"].max() < w.start - gap_days * DAY
            assert valid["TransactionDT"].min() >= w.start
            if w.end is not None:
                assert valid["TransactionDT"].max() < w.end  # later windows never leak in
            assert not set(train.index) & set(valid.index)

    def test_windows_do_not_overlap(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow]
    ) -> None:
        sets = [set(re_.window_split(frame, w)[1].index) for w in windows]
        assert not (sets[0] & sets[1]) and not (sets[1] & sets[2]) and not (sets[0] & sets[2])

    def test_gap_rows_are_in_neither_side(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow]
    ) -> None:
        w = windows[1]
        train, valid = re_.window_split(frame, w, 2 * DAY)
        t = frame["TransactionDT"]
        in_gap = (t >= w.start - 2 * DAY) & (t < w.start)
        assert in_gap.any()
        assert not set(frame.index[in_gap]) & (set(train.index) | set(valid.index))

    def test_training_features_ignore_rows_after_window_start(
        self,
        synthetic_frame: pd.DataFrame,
        frame: pd.DataFrame,
        windows: list[re_.ValidationWindow],
    ) -> None:
        names = HistoryConfig().feature_names()
        for w in windows:
            train, _ = re_.window_split(frame, w)
            past = synthetic_frame[synthetic_frame["TransactionDT"] < w.start]
            only_past = build_model_frame(past, "baseline", HistoryConfig())
            pd.testing.assert_frame_equal(only_past.loc[train.index, names], train[names])

    def test_invalid_definitions(self, frame: pd.DataFrame) -> None:
        with pytest.raises(ValueError, match="no training data"):
            re_.define_windows(frame["TransactionDT"], window_days=1_000)
        with pytest.raises(ValueError, match="positive"):
            re_.define_windows(frame["TransactionDT"], window_days=0)
        with pytest.raises(ValueError, match=">= 0"):
            re_.window_masks(frame["TransactionDT"], re_.ValidationWindow("W", 10, None), -1)
        beyond = re_.ValidationWindow("late", int(frame["TransactionDT"].max()) + 1, None)
        with pytest.raises(ValueError, match="empty side"):
            re_.window_split(frame, beyond)


class TestFloorPatience:
    def run(self, curve: list[float], floor: int, patience: int) -> tuple[int | None, int]:
        sel = re_.FloorPatience(floor, patience)
        for i, v in enumerate(curve, start=1):
            if sel.update(i, v):
                return sel.best_rounds, i
        return sel.best_rounds, len(curve)

    def test_early_peak_below_floor_cannot_be_selected_or_stop_training(self) -> None:
        curve = [0.30 if i == 6 else 0.20 + 0.0001 * i for i in range(1, 200)]  # peak at round 6
        best, stopped = self.run(curve, floor=50, patience=10)
        assert best is not None and best >= 50 and stopped >= 50

    def test_strict_improvement_and_patience_like_xgboost(self) -> None:
        curve = [0.1, 0.2, 0.2, 0.15, 0.15, 0.15]  # tie at round 3 is not an improvement
        best, stopped = self.run(curve, floor=1, patience=3)
        assert best == 2 and stopped == 5  # 3 rounds without improvement after round 2

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            re_.FloorPatience(0, 10)


class TestPolicies:
    @pytest.fixture(scope="class")
    def split(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        return re_.window_split(frame, windows[2])

    def test_early_stop_100_is_bit_identical_to_train_variant(
        self, split: tuple[pd.DataFrame, pd.DataFrame]
    ) -> None:
        train, valid = split
        ref = train_variant(train, COLS, LONG)
        new = re_.fit_variant(train, COLS, LONG, "early_stop_100").trained
        assert (new.best_rounds, new.threshold, new.scale_pos_weight) == (
            ref.best_rounds,
            ref.threshold,
            ref.scale_pos_weight,
        )
        assert new.holdout_average_precision == ref.holdout_average_precision
        x = valid.drop(columns="isFraud")
        np.testing.assert_array_equal(score(new, x, COLS), score(ref, x, COLS))

    def test_floor_never_selects_below_floor(
        self, split: tuple[pd.DataFrame, pd.DataFrame]
    ) -> None:
        fitted = re_.fit_variant(split[0], COLS, LONG, "floor_50", floor_rounds=30)
        assert fitted.trained.best_rounds >= 30
        assert fitted.curve["selected_rounds"] == fitted.trained.best_rounds

    def test_floor_of_one_reproduces_early_stopping(
        self, split: tuple[pd.DataFrame, pd.DataFrame]
    ) -> None:
        a = re_.fit_variant(split[0], COLS, LONG, "early_stop_100").trained
        b = re_.fit_variant(split[0], COLS, LONG, "floor_50", floor_rounds=1).trained
        assert (a.best_rounds, a.threshold) == (b.best_rounds, b.threshold)

    def test_fixed_rounds_are_exact(self, split: tuple[pd.DataFrame, pd.DataFrame]) -> None:
        fitted = re_.fit_variant(split[0], COLS, LONG, "fixed_300", fixed_rounds=25)
        assert fitted.trained.best_rounds == 25
        assert fitted.trained.model.num_boosted_rounds() == 25
        assert fitted.curve["rounds_evaluated"] == 25

    def test_default_policy_constants(self) -> None:
        assert (re_.FLOOR_ROUNDS, re_.FIXED_ROUNDS) == (50, 300)
        assert re_.POLICIES == ("early_stop_100", "floor_50", "fixed_300")

    def test_invalid_policy_arguments(self, split: tuple[pd.DataFrame, pd.DataFrame]) -> None:
        with pytest.raises(ValueError, match="unknown policy"):
            re_.fit_variant(split[0], COLS, LONG, "patience_7")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="floor_rounds"):
            re_.fit_variant(split[0], COLS, LONG, "floor_50", floor_rounds=LONG.max_rounds)

    @pytest.mark.parametrize("policy", re_.POLICIES)
    def test_validation_labels_never_affect_fitting(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow], policy: re_.Policy
    ) -> None:
        w = windows[1]
        _, valid_mask = re_.window_masks(frame["TransactionDT"], w)
        shuffled = frame.copy()
        shuffled.loc[valid_mask, "isFraud"] = np.random.default_rng(1).permutation(
            frame.loc[valid_mask, "isFraud"].to_numpy()
        )
        kw = {"floor_rounds": 20, "fixed_rounds": 25}
        outs = []
        for f in (frame, shuffled):
            train, valid = re_.window_split(f, w)
            fitted = re_.fit_variant(train, COLS, LONG, policy, **kw)
            t = fitted.trained
            outs.append((t.best_rounds, t.threshold, score(t, valid.drop(columns="isFraud"), COLS)))
        assert outs[0][:2] == outs[1][:2]
        np.testing.assert_array_equal(outs[0][2], outs[1][2])


class TestCurveSummary:
    def test_hand_computed(self) -> None:
        curve = [0.1, 0.3, 0.298, 0.25, 0.3]
        s = re_.curve_summary(curve, selected_rounds=4)
        assert s["best_round"] == 2 and s["best_aucpr"] == 0.3  # first maximum
        assert s["selected_aucpr"] == 0.25 and s["rounds_within_tolerance_of_best"] == 3
        assert s["aucpr_at"]["10"] is None and s["selected_below_low_rounds"] is True
        assert s["rounds_evaluated"] == 5

    def test_summary_is_aggregate_only(
        self, frame: pd.DataFrame, windows: list[re_.ValidationWindow]
    ) -> None:
        fitted = re_.fit_variant(re_.window_split(frame, windows[2])[0], COLS, LONG)
        values = list(fitted.curve.values())
        assert all(not isinstance(v, list | np.ndarray) for v in values)


@pytest.mark.real_data
@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_real_data_windows_match_the_milestone_2_split() -> None:
    txn = load_transactions(DEFAULT_RAW_DIR / "train_transaction.csv", usecols=[])
    w1, w2, w3 = re_.define_windows(txn["TransactionDT"])
    assert w3.start == 12_192_900
    counts = [int(re_.window_masks(txn["TransactionDT"], w)[1].sum()) for w in (w1, w2, w3)]
    assert counts == [97_676, 88_846, 118_108]
    train_rows = [int(re_.window_masks(txn["TransactionDT"], w)[0].sum()) for w in (w1, w2, w3)]
    assert train_rows == [285_910, 383_586, 472_432]
    w3_ids = txn.loc[re_.window_masks(txn["TransactionDT"], w3)[1], "TransactionID"]
    assert _id_digest(w3_ids) == "5ee0ab4d5e34f968"  # identical to the M2 / M2.1 validation set


# =========================================================================== stage 2

GRID_SETTINGS = re_.StudySettings(
    model=FAST, window_days=SYNTH_WINDOW_DAYS, bootstrap_resamples=10, min_positives=5
)
VOLATILE = {"pair_seconds", "seconds", "peak_memory_mb", "execution", "gates"}


def _strip(o: object) -> object:
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


@pytest.fixture(scope="module")
def grid_report(synthetic_frame: pd.DataFrame) -> dict[str, Any]:
    return re_.run_grid(synthetic_frame, re_.pilot_grid(), GRID_SETTINGS, mode="pilot")


class TestBudgetModes:
    def masks(self, n: int) -> dict[str, BoolArray]:
        first = np.arange(n) < n // 2
        return {"all": np.ones(n, dtype=bool), "card_seen": first, "card_unseen": ~first}

    def test_holdout_thresholds_hand_computed(self) -> None:
        hold = np.array([0.1, 0.9, 0.5, 0.7, 0.3, 0.8, 0.2, 0.6, 0.4, 0.05])
        t = re_.holdout_budget_thresholds(hold, (0.2, 0.05))
        assert t == {"0.2": 0.8, "0.05": 0.9}  # 2nd highest; ceil(0.5) = 1 -> highest
        with pytest.raises(ValueError):
            re_.holdout_budget_thresholds(np.array([]))

    def test_mode_a_matches_alert_budget_metrics_and_mode_b_uses_threshold(self) -> None:
        from src.models.metrics import alert_budget_metrics

        rng = np.random.default_rng(0)
        y, s = rng.integers(0, 2, 40), rng.random(40)
        out = re_.budget_results(y, s, self.masks(40), (0.1,), {"0.1": 0.95})
        a, b = out["mode_a_top_k"]["0.1"], out["mode_b_holdout_threshold"]["0.1"]
        ref = alert_budget_metrics(y, s, [0.1])[0]
        assert a["alerts"] == ref["alerts"] == 4 and a["cutoff_score"] == ref["cutoff_score"]
        assert a["subgroups"]["all"]["tp"] == ref["tp"]
        assert b["alerts"] == int((s >= 0.95).sum()) and b["alert_rate"] == b["alerts"] / 40

    def test_subgroup_breakdown_partitions_alerts(self) -> None:
        rng = np.random.default_rng(1)
        y, s = rng.integers(0, 2, 50), rng.random(50)
        out = re_.budget_results(y, s, self.masks(50), (0.2,), {"0.2": 0.5})
        for mode in out.values():
            sub = mode["0.2"]["subgroups"]
            assert sub["card_seen"]["alerts"] + sub["card_unseen"]["alerts"] == sub["all"]["alerts"]
            assert sub["card_seen"]["tp"] + sub["card_unseen"]["tp"] == sub["all"]["tp"]

    def test_empty_subgroup_and_no_positive_subgroup(self) -> None:
        y = np.array([0, 0, 1, 0])
        s = np.array([0.9, 0.1, 0.8, 0.2])
        masks = {
            "all": np.ones(4, dtype=bool),
            "empty": np.zeros(4, dtype=bool),
            "negatives": np.array([True, True, False, False]),
        }
        sub = re_.budget_results(y, s, masks, (0.5,), {"0.5": 0.5})["mode_a_top_k"]["0.5"]
        assert sub["subgroups"]["empty"] == {
            "rows": 0,
            "positives": 0,
            "alerts": 0,
            "tp": 0,
            "precision": None,
            "recall": None,
        }
        assert sub["subgroups"]["negatives"]["recall"] is None

    def test_alert_sets_do_not_depend_on_labels(self) -> None:
        rng = np.random.default_rng(2)
        s = rng.random(60)
        outs = [
            re_.budget_results(
                rng.integers(0, 2, 60), s, self.masks(60), (0.05, 0.2), {"0.05": 0.9, "0.2": 0.7}
            )
            for _ in range(2)
        ]
        for mode in outs[0]:
            for k in outs[0][mode]:
                a, b = outs[0][mode][k], outs[1][mode][k]
                assert a["alerts"] == b["alerts"]
                assert {g: v["alerts"] for g, v in a["subgroups"].items()} == {
                    g: v["alerts"] for g, v in b["subgroups"].items()
                }


def entries(
    points: Mapping[tuple[str, int], float | None],
    ci_lows: Mapping[tuple[str, int], float | None] | None = None,
    policy: str = "early_stop_100",
) -> list[dict[str, Any]]:
    """points / ci_lows: {(window, seed): value}; defaults fill all 15 required runs."""
    return [
        {
            "window": w,
            "seed": s,
            "policy": policy,
            "point": p,
            "ci95_low": (ci_lows or {}).get((w, s), 0.01),
        }
        for (w, s), p in points.items()
    ]


ALL_RUNS = [(w, s) for w in re_.WINDOW_NAMES for s in re_.SEEDS_MAIN]


class TestDecisionRule:
    def test_all_positive_is_consistent(self) -> None:
        d = re_.decide_unseen_uid_consistency(entries(dict.fromkeys(ALL_RUNS, 0.02)))
        assert d["verdict"] == "consistent" and d["positive_runs"] == 15

    def test_exactly_twelve_positive_runs_is_enough(self) -> None:
        pts = dict.fromkeys(ALL_RUNS, 0.03)
        for k in [("W1", 1), ("W2", 2), ("W3", 3)]:
            pts[k] = -0.01  # one negative per window keeps every window mean > 0
        d = re_.decide_unseen_uid_consistency(entries(pts))
        assert d["positive_runs"] == 12 and d["verdict"] == "consistent"

    def test_eleven_positive_runs_fails(self) -> None:
        pts = dict.fromkeys(ALL_RUNS, 0.05)
        for k in [("W1", 1), ("W1", 2), ("W2", 2), ("W3", 3)]:
            pts[k] = -0.01
        d = re_.decide_unseen_uid_consistency(entries(pts))
        assert d["positive_runs"] == 11 and d["verdict"] == "not_consistent"
        assert d["criteria_met"]["positive_runs"] is False

    def test_a_non_positive_window_mean_fails(self) -> None:
        pts = dict.fromkeys(ALL_RUNS, 0.02)
        pts[("W2", 42)] = -0.2  # W2 mean < 0 while 14/15 runs stay positive
        d = re_.decide_unseen_uid_consistency(entries(pts))
        assert d["positive_runs"] == 14 and d["criteria_met"]["all_window_means_positive"] is False
        assert d["verdict"] == "not_consistent"

    def test_ci_criterion_needs_two_windows_and_strictly_positive_median(self) -> None:
        pts = dict.fromkeys(ALL_RUNS, 0.02)
        lows = {k: (0.01 if k[0] == "W1" else -0.01) for k in ALL_RUNS}
        d = re_.decide_unseen_uid_consistency(entries(pts, lows))
        assert d["criteria_met"]["windows_ci_above_zero"] is False  # only W1
        lows = {k: (0.01 if k[0] == "W1" else 0.0) for k in ALL_RUNS}  # median exactly 0
        d = re_.decide_unseen_uid_consistency(entries(pts, lows))
        assert d["window_median_ci95_low_above_zero"] == {"W1": True, "W2": False, "W3": False}
        lows = {k: (-0.01 if k[0] == "W3" else 0.01) for k in ALL_RUNS}
        assert re_.decide_unseen_uid_consistency(entries(pts, lows))["verdict"] == "consistent"

    def test_missing_or_non_evaluable_runs(self) -> None:
        pts: dict[tuple[str, int], float | None] = dict.fromkeys(ALL_RUNS, 0.02)
        del pts[("W3", 4)]
        assert re_.decide_unseen_uid_consistency(entries(pts))["verdict"] == "insufficient_runs"
        pts = dict.fromkeys(ALL_RUNS, 0.02)
        pts[("W1", 1)] = None  # not evaluable: counts as not positive, and W1 mean undefined
        d = re_.decide_unseen_uid_consistency(entries(pts))
        assert d["positive_runs"] == 14 and d["window_mean_point"]["W1"] is None
        assert d["verdict"] == "not_consistent"

    def test_other_policies_do_not_count(self) -> None:
        fake = entries(dict.fromkeys(ALL_RUNS, 0.02), policy="floor_50")
        assert re_.decide_unseen_uid_consistency(fake)["verdict"] == "insufficient_runs"

    def test_pilot_grid_cannot_produce_a_verdict(self, grid_report: dict[str, Any]) -> None:
        assert grid_report["decision_rule"]["verdict"] == "insufficient_runs"
        assert any("decision rule not applied" in w for w in grid_report["warnings"])


class TestGridAndAggregation:
    def test_grids_match_the_plan(self) -> None:
        assert len(re_.pilot_grid()) == 3 and len(re_.main_grid()) == 15
        assert len(re_.sensitivity_grid()) == 12
        assert {s.policy for s in re_.main_grid()} == {"early_stop_100"}
        assert {(s.window, s.seed) for s in re_.pilot_grid()} == {("W3", 42), ("W1", 42), ("W1", 1)}

    def test_runs_use_identical_rows_for_both_variants(self, grid_report: dict[str, Any]) -> None:
        windows = {w["name"]: w for w in grid_report["windows"]}
        for run in grid_report["runs"]:
            assert run["valid_digest"] == windows[run["window"]]["valid_digest"]
            assert run["valid_rows"] == windows[run["window"]]["valid_rows"]
            base, rest = run["variants"]["baseline"], run["variants"]["no_identifier_codes"]
            assert base["subgroups"]["all"]["rows"] == rest["subgroups"]["all"]["rows"]
            assert base["n_inputs"] - rest["n_inputs"] == 6

    def test_aggregates_match_runs(self, grid_report: dict[str, Any]) -> None:
        agg = grid_report["aggregates"]["models"]["W1"]["early_stop_100"]["baseline"]
        w1 = [r for r in grid_report["runs"] if r["window"] == "W1"]
        aps = [
            r["variants"]["baseline"]["subgroups"]["all"]["ranking"]["average_precision"]
            for r in w1
        ]
        assert agg["seeds"] == [1, 42] and agg["average_precision"]["all"]["n"] == 2
        assert agg["average_precision"]["all"]["mean"] == pytest.approx(np.mean(aps))
        d = grid_report["aggregates"]["deltas_restricted_minus_baseline"]["W1"]["early_stop_100"]
        points = [r["deltas_restricted_minus_baseline"]["all"].get("point") for r in w1]
        assert d["all"]["positive_runs"] == sum((p or 0) > 0 for p in points)

    def test_stats_helper(self) -> None:
        assert re_._stats([1.0, None, 3.0]) == {
            "n": 2,
            "mean": 2.0,
            "std": pytest.approx(1.4142135),
            "min": 1.0,
            "max": 3.0,
        }
        assert re_._stats([5.0])["std"] is None and re_._stats([])["mean"] is None

    def test_deterministic_output(
        self, synthetic_frame: pd.DataFrame, grid_report: dict[str, Any]
    ) -> None:
        again = re_.run_grid(synthetic_frame, re_.pilot_grid(), GRID_SETTINGS, mode="pilot")
        assert _strip(again) == _strip(grid_report)

    def test_validation_labels_do_not_affect_fitting_or_alert_sets(
        self, synthetic_frame: pd.DataFrame, grid_report: dict[str, Any]
    ) -> None:
        # W3 rows are never training rows for any window, so permuting them isolates W3.
        w3 = next(w for w in grid_report["windows"] if w["name"] == "W3")
        in_w3 = synthetic_frame["TransactionDT"] >= w3["start"]
        shuffled = synthetic_frame.copy()
        shuffled.loc[in_w3, "isFraud"] = np.random.default_rng(9).permutation(
            synthetic_frame.loc[in_w3, "isFraud"].to_numpy()
        )
        spec = [re_.RunSpec("W3", 42, "early_stop_100")]
        a = re_.run_grid(synthetic_frame, spec, GRID_SETTINGS)["runs"][0]
        b = re_.run_grid(shuffled, spec, GRID_SETTINGS)["runs"][0]
        for name in a["variants"]:
            va, vb = a["variants"][name], b["variants"][name]
            assert (va["rounds"], va["threshold"]) == (vb["rounds"], vb["threshold"])
            for mode in va["budgets"]:
                for k in va["budgets"][mode]:
                    x, y = va["budgets"][mode][k], vb["budgets"][mode][k]
                    assert x["alerts"] == y["alerts"]
                    assert x.get("threshold") == y.get("threshold")
                    assert x.get("cutoff_score") == y.get("cutoff_score")

    def test_unknown_window_rejected(self, synthetic_frame: pd.DataFrame) -> None:
        with pytest.raises(ValueError, match="unknown windows"):
            re_.run_grid(synthetic_frame, [re_.RunSpec("W9", 1, "early_stop_100")], GRID_SETTINGS)


class TestChecksAndGates:
    def fake_run(self, digest: str, base_ap: float, rest_ap: float, **kw: object) -> dict[str, Any]:
        def variant(ap: float) -> dict[str, Any]:
            return {"subgroups": {"all": {"ranking": {"average_precision": ap}}}}

        return {
            "window": "W3",
            "seed": 42,
            "policy": "early_stop_100",
            "valid_digest": digest,
            "variants": {"baseline": variant(base_ap), "no_identifier_codes": variant(rest_ap)},
            "pair_seconds": 60.0,
            "peak_memory_mb": 1000.0,
            **kw,
        }

    # Expected values as stored in the committed reports/evaluation_study.json (gap 0, seed 42).
    BASE, REST, DIGEST = 0.2504409401014052, 0.21065249648871145, "5ee0ab4d5e34f968"
    REF = {
        "source": "evaluation_study.json",
        "digest": DIGEST,
        "average_precision": {"baseline": BASE, "no_identifier_codes": REST},
    }

    def check(self, base: float, rest: float, digest: str = DIGEST) -> dict[str, Any]:
        return re_.regression_check([self.fake_run(digest, base, rest)], reference=self.REF)

    def test_reference_is_read_from_the_committed_report(self) -> None:
        ref = re_.load_regression_reference()
        assert ref["average_precision"] == {"baseline": self.BASE, "no_identifier_codes": self.REST}
        assert ref["digest"] == self.DIGEST and ref["source"] == "evaluation_study.json"
        assert re_.REGRESSION_TOLERANCE == 1e-6

    def test_exact_and_within_tolerance_pass(self) -> None:
        exact = self.check(self.BASE, self.REST)
        assert exact["passed"] and exact["abs_diff"] == {
            "baseline": 0.0,
            "no_identifier_codes": 0.0,
        }
        near = self.check(self.BASE + 9e-7, self.REST - 9e-7)
        assert near["passed"] and near["checks"]["average_precision_within_tolerance"]
        assert near["tolerance"] == 1e-6 and near["comparison"].startswith("absolute difference")

    def test_outside_tolerance_fails_per_variant(self) -> None:
        base_off = self.check(self.BASE + 2e-6, self.REST)
        assert base_off["passed"] is False
        assert base_off["abs_diff"]["baseline"] == pytest.approx(2e-6)
        rest_off = self.check(self.BASE, self.REST - 2e-6)
        assert rest_off["passed"] is False

    def test_rounding_no_longer_masks_drift(self) -> None:
        # 0.21072 rounds to 0.2107 (the old check passed it) but is 6.5e-5 from the reference.
        assert self.check(self.BASE, 0.21072)["passed"] is False
        # 0.2106 (a rounding flip) is only ~5e-5 away yet also clearly out of tolerance.
        assert self.check(self.BASE, 0.2106)["passed"] is False

    def test_digest_mismatch_fails_even_with_exact_ap(self) -> None:
        r = self.check(self.BASE, self.REST, digest="deadbeefdeadbeef")
        assert r["passed"] is False
        assert r["checks"] == {"average_precision_within_tolerance": True, "digest_match": False}

    def test_missing_ap_fails(self) -> None:
        r = self.check(self.BASE, None)  # type: ignore[arg-type]
        assert r["passed"] is False and r["abs_diff"]["no_identifier_codes"] is None

    def test_absent_target_and_unavailable_reference_fail_closed(self, tmp_path: Path) -> None:
        absent = re_.regression_check([])
        assert absent["evaluated"] is False and absent["passed"] is False
        missing = tmp_path / "missing.json"
        with pytest.raises(OSError):
            re_.load_regression_reference(missing)
        wrong_seed = tmp_path / "wrong_seed.json"
        wrong_seed.write_text(
            json.dumps({"config": {"model": {"seed": 7, "early_stopping_rounds": 100}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="seed"):
            re_.load_regression_reference(wrong_seed)

    def test_unavailable_reference_is_not_evaluated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def broken() -> dict[str, Any]:
            raise FileNotFoundError(2, "No such file", "C:/Users/someone/private/x.json")

        monkeypatch.setattr(re_, "load_regression_reference", broken)
        r = re_.regression_check([self.fake_run(self.DIGEST, self.BASE, self.REST)])
        assert r["evaluated"] is False and r["passed"] is False and "unavailable" in r["reason"]
        # The OSError text contains an absolute path; only the file name may reach the report.
        assert "Users" not in r["reason"] and "evaluation_study.json" in r["reason"]

    def test_markdown_documents_tolerance_and_values(self, grid_report: dict[str, Any]) -> None:
        import copy

        r = copy.deepcopy(grid_report)
        r["regression_check"] = self.check(self.BASE + 2e-6, self.REST)
        md = re_.render_markdown(r)
        assert "absolute tolerance of 1e-06" in md and "evaluation_study.json" in md
        assert f"expected {self.BASE!r}" in md and "OUT OF TOLERANCE" in md
        r["regression_check"] = {
            "evaluated": False,
            "passed": False,
            "reason": "reference unavailable",
        }
        assert "NOT EVALUATED" in re_.render_markdown(r)

    def test_gates(self) -> None:
        over = self.fake_run("x", 0.1, 0.1, pair_seconds=400.0, peak_memory_mb=9000.0)
        g = re_.gates([over], 27)
        assert g["projected_full_grid_seconds"] == 400.0 * 27  # 10,800 s > 9,000 s limit
        assert g["memory_ok"] is False and g["runtime_ok"] is False
        within = self.fake_run("x", 0.1, 0.1, pair_seconds=300.0, peak_memory_mb=8000.0)
        g = re_.gates([within], 27)  # 8,100 s and exactly 8,000 MB are both within limits
        assert g["memory_ok"] is True and g["runtime_ok"] is True
        assert re_.gates([], 27)["runtime_ok"] is None

    def test_low_round_and_small_subgroup_warnings(self, grid_report: dict[str, Any]) -> None:
        import copy

        r = copy.deepcopy(grid_report)
        r["runs"][0]["variants"]["baseline"]["low_rounds"] = True
        r["runs"][0]["variants"]["baseline"]["rounds"] = 6
        w = re_.collect_warnings(r["runs"], GRID_SETTINGS, r["decision_rule"])
        assert any("selected only 6 rounds" in x for x in w)
        small = [x for x in w if "frauds (<" in x]
        assert len(small) == len(set(small))  # deduplicated per (window, subgroup)


class TestReportPrivacy:
    def test_json_is_strict_and_aggregate_only(self, grid_report: dict[str, Any]) -> None:
        text = json.dumps(grid_report, allow_nan=False)  # raises on NaN / inf
        assert "|" not in text  # no entity key strings

        def walk(o: object) -> tuple[int, set[str]]:
            if isinstance(o, list):
                inner = [walk(x) for x in o]
                return max([len(o), *(i[0] for i in inner)]), set().union(*(i[1] for i in inner))
            if isinstance(o, dict):
                inner = [walk(x) for x in o.values()]
                return max([0, *(i[0] for i in inner)]), set(o).union(*(i[1] for i in inner))
            return 0, set()

        longest, keys = walk(grid_report)
        assert longest < 50  # row-level arrays would be as long as a validation window
        assert not keys & {"scores", "predictions", "TransactionID", "card_id", "uid"}

    def test_markdown_sections_and_statements(self, grid_report: dict[str, Any]) -> None:
        md = re_.render_markdown(grid_report)
        for heading in (
            "Validation windows",
            "Checks and gates",
            "Decision rule",
            "Paired PR-AUC change",
            "Alert budgets",
            "Early-stopping diagnostics",
            "Warnings",
        ):
            assert heading in md
        assert "not a measurement of chargeback latency" in md and "—%" not in md
        assert "Regression (W3, seed 42" in md

    def test_cli_requires_a_mode_and_names_outputs(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            re_.main([])
        with pytest.raises(SystemExit):
            re_.main(["--pilot", "--full"])
        assert re_.output_paths(tmp_path, "pilot")[0].name == "robust_evaluation_pilot.json"
        assert re_.output_paths(tmp_path, "full")[1].name == "robust_evaluation.md"

    @pytest.mark.parametrize("mode", ["pilot", "full"])
    def test_cli_refuses_to_overwrite_existing_reports(self, tmp_path: Path, mode: str) -> None:
        """The guard fires before any data is read (raw dir does not exist) and keeps files."""
        json_path, _ = re_.output_paths(tmp_path, mode)
        json_path.write_text('{"kept": true}\n', encoding="utf-8")
        with pytest.raises(SystemExit):
            re_.main([f"--{mode}", "--out-dir", str(tmp_path), "--raw-dir", str(tmp_path / "x")])
        assert json_path.read_text(encoding="utf-8") == '{"kept": true}\n'

    def test_cli_overwrite_flag_passes_the_guard(self, tmp_path: Path) -> None:
        re_.output_paths(tmp_path, "pilot")[1].write_text("old\n", encoding="utf-8")
        # Past the guard the loader runs, and fails on the missing raw directory.
        with pytest.raises(FileNotFoundError):
            re_.main(
                ["--pilot", "--overwrite", "--out-dir", str(tmp_path), "--raw-dir", str(tmp_path)]
            )


def test_seed_reaches_xgboost_when_sampling_is_enabled(synthetic_frame: pd.DataFrame) -> None:
    """Guards against a 5-seed study silently collapsing into one repeated run."""
    sampled = replace(
        FAST, xgb_params={**FAST.xgb_params, "subsample": 0.8, "colsample_bytree": 0.8}
    )
    settings = replace(GRID_SETTINGS, model=sampled, bootstrap_resamples=0)
    specs = [re_.RunSpec("W3", 42, "early_stop_100"), re_.RunSpec("W3", 1, "early_stop_100")]
    a, b = re_.run_grid(synthetic_frame, specs, settings)["runs"]
    ap = [
        r["variants"]["baseline"]["subgroups"]["all"]["ranking"]["average_precision"]
        for r in (a, b)
    ]
    curves = [r["variants"]["baseline"]["curve"]["best_aucpr"] for r in (a, b)]
    assert ap[0] != ap[1] or curves[0] != curves[1]
    assert ComparisonConfig().xgb_params["subsample"] < 1  # production samples rows
