from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.features.entity import CARD_ID, USER_ID
from src.features.history import HistoryConfig
from src.features.split import time_cutoff
from src.models import evaluation_study as es
from src.models.compare import build_model_frame
from src.models.leakage_audit import IDENTIFIER_INPUTS
from src.models.metrics import average_precision, day_block_bootstrap_ap
from tests.test_models import FAST, synthetic_split

DAY = 86_400
STUDY = es.StudyConfig(gaps_days=(0, 2), bootstrap_resamples=20, min_positives=5, model=FAST)


@pytest.fixture(scope="module")
def frame(synthetic_frame: pd.DataFrame) -> pd.DataFrame:
    return build_model_frame(synthetic_frame, "baseline", HistoryConfig())


@pytest.fixture(scope="module")
def report(synthetic_frame: pd.DataFrame) -> dict[str, Any]:
    return es.run_study(synthetic_frame, STUDY)


class TestGapSplit:
    def test_boundaries_and_excluded_gap(self, frame: pd.DataFrame) -> None:
        v = time_cutoff(frame["TransactionDT"], 0.2)
        for gap in (0, DAY, 5 * DAY):
            train, valid, b = es.gap_split(frame, v, gap)
            assert b.valid_start == v and b.train_end == v - gap
            assert train["TransactionDT"].max() < v - gap
            assert valid["TransactionDT"].min() >= v
            in_gap = (frame["TransactionDT"] >= v - gap) & (frame["TransactionDT"] < v)
            assert not set(frame.index[in_gap]) & (set(train.index) | set(valid.index))
            assert len(train) + len(valid) + int(in_gap.sum()) == len(frame)

    def test_validation_rows_identical_across_gaps(self, frame: pd.DataFrame) -> None:
        v = time_cutoff(frame["TransactionDT"], 0.2)
        idx = [es.gap_split(frame, v, g * DAY)[1].index for g in (0, 1, 7)]
        assert idx[0].equals(idx[1]) and idx[0].equals(idx[2])

    def test_larger_gap_never_adds_training_rows(self, frame: pd.DataFrame) -> None:
        v = time_cutoff(frame["TransactionDT"], 0.2)
        sizes = [len(es.gap_split(frame, v, g * DAY)[0]) for g in (0, 1, 3, 7)]
        assert sizes == sorted(sizes, reverse=True)

    def test_invalid_gaps(self, frame: pd.DataFrame) -> None:
        v = time_cutoff(frame["TransactionDT"], 0.2)
        with pytest.raises(ValueError, match=">= 0"):
            es.gap_split(frame, v, -1)
        with pytest.raises(ValueError, match="empty side"):
            es.gap_split(frame, v, 10_000 * DAY)  # no training rows left

    def test_training_features_ignore_rows_after_train_end(
        self, synthetic_frame: pd.DataFrame, frame: pd.DataFrame
    ) -> None:
        """Gap and validation rows cannot influence training-row features."""
        v = time_cutoff(frame["TransactionDT"], 0.2)
        train, _, b = es.gap_split(frame, v, 3 * DAY)
        past = synthetic_frame[synthetic_frame["TransactionDT"] < b.train_end]
        only_past = build_model_frame(past, "baseline", HistoryConfig())
        names = HistoryConfig().feature_names()
        pd.testing.assert_frame_equal(only_past.loc[train.index, names], train[names])


class TestSubgroups:
    def keys(self, cards: Sequence[str | None], uids: Sequence[str | None]) -> pd.DataFrame:
        return pd.DataFrame({CARD_ID: cards, USER_ID: uids})

    def test_definitions(self) -> None:
        train = self.keys(["c1", "c2"], ["c1|a", "c2|a"])
        valid = self.keys(["c1", "c1", "c3", None, "c2"], ["c1|a", "c1|b", "c3|a", None, "c2|a"])
        m = es.subgroup_masks(train, valid)
        assert m["card_seen"].tolist() == [True, True, False, False, True]
        assert m["card_unseen"].tolist() == [False, False, True, False, False]
        assert m["uid_seen"].tolist() == [True, False, False, False, True]
        assert m["card_seen_uid_unseen"].tolist() == [False, True, False, False, False]
        assert m["no_entity_key"].tolist() == [False, False, False, True, False]

    def test_seen_means_training_rows_only(self) -> None:
        # An entity seen only in the gap is not passed as a training key -> unseen.
        m = es.subgroup_masks(self.keys(["c1"], ["c1|a"]), self.keys(["gap_only"], ["gap_only|a"]))
        assert m["card_unseen"].tolist() == [True] and m["uid_unseen"].tolist() == [True]

    def test_partitions_and_implications(self, report: dict[str, Any]) -> None:
        rows = report["gaps"]["0"]["subgroup_rows"]
        assert rows["card_seen"] + rows["card_unseen"] + rows["no_entity_key"] == rows["all"]
        assert rows["uid_seen"] + rows["uid_unseen"] + rows["no_entity_key"] == rows["all"]
        # card unseen implies uid unseen, so uid_unseen = card_unseen + known-card/new-uid.
        assert rows["uid_unseen"] == rows["card_unseen"] + rows["card_seen_uid_unseen"]


class TestGroupMetrics:
    def test_hand_computed_at_threshold(self) -> None:
        y = np.array([1, 0, 1, 0, 0])
        s = np.array([0.9, 0.8, 0.2, 0.1, 0.6])
        m = es.group_metrics(y, s, 0.6, np.zeros(5), min_positives=1, resamples=0, seed=0)
        t = m["at_threshold"]
        assert (t["tp"], t["fp"], t["fn"], t["flagged"]) == (1, 2, 1, 3)
        assert t["precision"] == pytest.approx(1 / 3) and t["recall"] == 0.5
        assert m["ranking"]["average_precision"] == pytest.approx(average_precision(y, s))

    def test_no_positives_and_empty_groups(self) -> None:
        m = es.group_metrics(
            np.array([0, 0]),
            np.array([0.9, 0.1]),
            0.5,
            np.zeros(2),
            min_positives=1,
            resamples=10,
            seed=0,
        )
        assert m["ranking"]["average_precision"] is None and m["reliable"] is False
        assert m["at_threshold"]["recall"] is None and m["at_threshold"]["fp"] == 1
        empty = es.group_metrics(
            np.array([], dtype=int),
            np.array([]),
            0.5,
            np.array([]),
            min_positives=1,
            resamples=10,
            seed=0,
        )
        assert empty["rows"] == 0 and empty["prevalence"] is None

    def test_small_groups_flagged_unreliable(self) -> None:
        y = np.array([1, 0, 0])
        m = es.group_metrics(
            y, np.array([0.9, 0.2, 0.1]), 0.5, np.zeros(3), min_positives=50, resamples=0, seed=0
        )
        assert m["reliable"] is False and m["ranking"]["average_precision"] == 1.0

    def test_bootstrap_ap_interval_is_deterministic_and_contains_point(self) -> None:
        rng = np.random.default_rng(0)
        y, s = rng.integers(0, 2, 400), rng.random(400)
        day = np.repeat(np.arange(20), 20)
        a = day_block_bootstrap_ap(y, s, day, resamples=50, seed=3)
        assert a == day_block_bootstrap_ap(y, s, day, resamples=50, seed=3)
        assert a["ci95_low"] <= average_precision(y, s) <= a["ci95_high"]


class TestStudy:
    def test_same_rows_and_consistent_protocol(self, report: dict[str, Any]) -> None:
        g0, g2 = report["gaps"]["0"], report["gaps"]["2"]
        assert (
            g0["subgroup_rows"]["all"] == g2["subgroup_rows"]["all"] == report["validation"]["rows"]
        )
        assert g2["train_rows"] < g0["train_rows"] and g2["gap_rows_excluded"] > 0
        assert g0["gap_rows_excluded"] == 0
        v = g0["variants"]
        assert v[es.BASELINE]["n_inputs"] - v[es.RESTRICTED]["n_inputs"] == len(IDENTIFIER_INPUTS)

    def test_validation_labels_do_not_change_training_outcomes(
        self, synthetic_frame: pd.DataFrame, report: dict[str, Any]
    ) -> None:
        cutoff = time_cutoff(synthetic_frame["TransactionDT"], 0.2)
        valid = synthetic_frame["TransactionDT"] >= cutoff
        shuffled = synthetic_frame.copy()
        shuffled.loc[valid, "isFraud"] = np.random.default_rng(5).permutation(
            synthetic_frame.loc[valid, "isFraud"].to_numpy()
        )
        other = es.run_study(shuffled, replace(STUDY, bootstrap_resamples=0))
        for gap in ("0", "2"):
            for name in (es.BASELINE, es.RESTRICTED):
                a, b = report["gaps"][gap]["variants"][name], other["gaps"][gap]["variants"][name]
                for k in ("threshold", "best_rounds", "scale_pos_weight"):
                    assert a[k] == b[k]
                fa = a["subgroups"]["all"]["at_threshold"]["flagged"]
                assert fa == b["subgroups"]["all"]["at_threshold"]["flagged"]  # same scores

    def test_report_is_aggregate_only(self, report: dict[str, Any]) -> None:
        text = json.dumps(report)
        assert "|" not in text  # no entity key strings

        def longest_list(o: object) -> int:
            if isinstance(o, list):
                return max([len(o), *(longest_list(x) for x in o)])
            if isinstance(o, dict):
                return max([0, *(longest_list(x) for x in o.values())])
            return 0

        # Row-level arrays (scores, IDs, labels) would be as long as the validation set.
        assert longest_list(report) < 50 < report["validation"]["rows"]

    def test_markdown_and_main(self, tmp_path: Path, report: dict[str, Any]) -> None:
        md = es.render_markdown(report)
        assert "Seen vs unseen" in md and "Label-delay" in md and "not measured chargeback" in md
        raw = synthetic_split(tmp_path / "raw", n=1_500, seed=2)
        es.main(raw, tmp_path / "out", replace(STUDY, gaps_days=(0, 1), bootstrap_resamples=5))
        assert (tmp_path / "out" / "evaluation_study.json").is_file()
        assert (tmp_path / "out" / "evaluation_study.md").is_file()


def test_markdown_flags_low_round_models_and_formats_empty_groups(report: dict[str, Any]) -> None:
    import copy

    r = copy.deepcopy(report)
    r["gaps"]["2"]["variants"][es.RESTRICTED]["best_rounds"] = 3
    md = es.render_markdown(r)
    assert "gap 2 / no_identifier_codes (3 rounds)" in md and "unreliable" in md
    assert "—%" not in md  # empty subgroups (no_entity_key) render as "—"
