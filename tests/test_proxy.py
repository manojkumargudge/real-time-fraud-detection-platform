from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.load import SchemaError
from src.features import proxy_report
from src.features.entity import (
    CARD_START_ID,
    MISSING_TOKEN,
    USER_START_ID,
    account_start_day,
    build_account_start_keys,
    build_entity_keys,
)
from src.features.history import compute_change_flags, compute_history_features
from tests.conftest import make_identity, make_transactions, write_split
from tests.test_features import frame, random_frame

DAY = 86_400


def with_d1(rows: list[dict]) -> pd.DataFrame:
    return frame([{"D1": 0.0, **r} for r in rows])


class TestAccountStartDay:
    def test_start_day_is_transaction_day_minus_d1(self) -> None:
        df = with_d1([{"TransactionDT": 10 * DAY + 5, "D1": 3.0}, {"TransactionDT": DAY - 1}])
        assert account_start_day(df).tolist() == [7.0, 0.0]

    def test_same_account_on_different_days_shares_a_key(self) -> None:
        df = with_d1(
            [{"TransactionDT": 5 * DAY, "D1": 2.0}, {"TransactionDT": 9 * DAY + 100, "D1": 6.0}]
        )
        keys = build_account_start_keys(df)
        assert keys.loc[0, USER_START_ID] == keys.loc[1, USER_START_ID]
        assert keys.loc[0, USER_START_ID].endswith("|315|3")

    def test_different_start_days_split_a_uid(self) -> None:
        df = with_d1([{"TransactionDT": 5 * DAY, "D1": 2.0}, {"TransactionDT": 5 * DAY, "D1": 4.0}])
        keys = build_account_start_keys(df)
        assert keys[USER_START_ID].nunique() == 2
        uid = build_entity_keys(df)["uid"]
        assert uid.loc[0] == uid.loc[1]

    def test_missing_d1_falls_back_to_token(self) -> None:
        df = with_d1([{"D1": np.nan}])
        assert np.isnan(account_start_day(df).iloc[0])
        assert build_account_start_keys(df).loc[0, CARD_START_ID].endswith(f"|{MISSING_TOKEN}")

    def test_missing_card1_gives_no_entity(self) -> None:
        keys = build_account_start_keys(with_d1([{"card1": np.nan, "D1": 1.0}]))
        assert keys.isna().all(axis=None)

    @pytest.mark.parametrize("d1", [-1.0, 2.5])
    def test_invalid_d1_rejected(self, d1: float) -> None:
        with pytest.raises(SchemaError, match="D1"):
            account_start_day(with_d1([{"D1": d1}]))

    def test_missing_d1_column_rejected(self) -> None:
        with pytest.raises(SchemaError, match="D1"):
            account_start_day(frame([{}]))


class TestAccountKeyHasNoLeakage:
    def test_key_is_row_local(self) -> None:
        df = random_frame(seed=7).assign(D1=lambda d: (d["TransactionDT"] // 1_800) % 5)
        full = build_account_start_keys(df)
        past = df[df["TransactionDT"] < 20 * 1_800]
        pd.testing.assert_frame_equal(build_account_start_keys(past), full.loc[past.index])
        single = build_account_start_keys(df.iloc[[3]])
        assert single.iloc[0].equals(full.iloc[3])

    def test_key_ignores_labels(self) -> None:
        df = random_frame(seed=8).assign(D1=1.0)
        base = build_account_start_keys(df)
        pd.testing.assert_frame_equal(build_account_start_keys(df.assign(isFraud=0)), base)
        pd.testing.assert_frame_equal(build_account_start_keys(df.drop(columns="isFraud")), base)

    def test_default_features_unchanged_by_d1(self) -> None:
        df = random_frame(seed=9)
        base = compute_history_features(df)
        pd.testing.assert_frame_equal(compute_history_features(df.assign(D1=3.0)), base)


class TestChangeFlagsOnAnyEntity:
    def test_matches_card_level_features(self) -> None:
        df = random_frame(seed=10)
        feats = compute_history_features(df)
        flags = compute_change_flags(df, feats["card_id"], ("DeviceInfo", "addr1"), "card")
        pd.testing.assert_frame_equal(
            flags, feats[["card_deviceinfo_changed", "card_addr1_changed"]]
        )

    def test_finer_entity_does_not_compare_across_accounts(self) -> None:
        df = with_d1(
            [
                {"TransactionDT": 5 * DAY, "D1": 2.0, "DeviceInfo": "a"},
                {"TransactionDT": 6 * DAY, "D1": 0.0, "DeviceInfo": "b"},  # new account
            ]
        )
        keys = pd.concat([build_entity_keys(df), build_account_start_keys(df)], axis=1)
        coarse = compute_change_flags(df, keys["card_id"], ("DeviceInfo",), "f")
        fine = compute_change_flags(df, keys[CARD_START_ID], ("DeviceInfo",), "f")
        assert coarse.loc[1, "f_deviceinfo_changed"] == 1.0
        assert np.isnan(fine.loc[1, "f_deviceinfo_changed"])

    def test_misaligned_entity_rejected(self) -> None:
        df = random_frame(seed=11)
        with pytest.raises(ValueError, match="aligned"):
            compute_change_flags(df, build_entity_keys(df)["uid"].iloc[1:], ("addr1",), "f")


class TestReportHelpers:
    def test_shuffle_within_preserves_group_multisets(self) -> None:
        values = pd.Series([1, 2, 3, 10, 20, 30, 40])
        groups = pd.Series(["a", "a", "a", "b", "b", "b", "b"])
        out = proxy_report.shuffle_within(values, groups, seed=1)
        for g in ("a", "b"):
            assert sorted(out[groups == g]) == sorted(values[groups == g])
        pd.testing.assert_series_equal(proxy_report.shuffle_within(values, groups, seed=1), out)

    def test_group_size_stats(self) -> None:
        key = pd.Series(["x"] * 150 + ["y"] * 50 + ["z"])
        s = proxy_report.group_size_stats(key)
        assert s["entities"] == 3 and s["size_max"] == 150 and s["singletons"] == 1
        assert s["pct_rows_in_groups_gt_100"] == round(100 * 150 / 201, 2)
        assert s["pct_rows_in_groups_gt_500"] == 0.0

    def test_modal_purity_ignores_singletons_and_missing(self) -> None:
        key = pd.Series(["a", "a", "a", "b", "c", "c"])
        vals = pd.Series(["x", "x", "y", "z", None, "w"])
        assert proxy_report.modal_purity(key, vals) == round(2 / 3, 4)


def test_proxy_report_main_writes_aggregates_only(tmp_path: Path) -> None:
    raw = write_split(tmp_path / "raw", make_transactions(10), make_identity([1001, 1004]))
    out = tmp_path / "p.json"
    metrics = proxy_report.main(raw, out)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(metrics))
    assert set(saved["group_sizes"]) >= {"card_id", "uid", "card_d1", "uid_d1", "uid_d1_null"}
    assert "1111|" not in out.read_text(encoding="utf-8")  # no entity keys or raw values
