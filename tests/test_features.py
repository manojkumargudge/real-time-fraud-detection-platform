from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.data.load import DEFAULT_RAW_DIR, SchemaError, load_ieee_cis
from src.features import build
from src.features.entity import (
    CARD_ID,
    MISSING_TOKEN,
    USER_ID,
    build_entity_key,
    build_entity_keys,
)
from src.features.history import HistoryConfig, compute_history_features
from src.features.preprocess import CategoricalEncoder, NotFittedError
from src.features.split import chronological_split, time_cutoff
from tests.conftest import make_identity, make_transactions, write_split

NAMES = HistoryConfig().feature_names()
CHANGE_COLS = HistoryConfig().change_columns


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Minimal feature-input frame; unspecified fields get a fixed default."""
    defaults = {
        "TransactionDT": 0,
        "TransactionAmt": 10.0,
        "card1": 1111,
        "card2": 321.0,
        "card3": 150.0,
        "card4": "visa",
        "card5": 226.0,
        "card6": "debit",
        "addr1": 315.0,
        "addr2": 87.0,
        "DeviceType": None,
        "DeviceInfo": None,
        "isFraud": 0,
    }
    df = pd.DataFrame([{**defaults, **r} for r in rows])
    if "TransactionID" not in df:
        df["TransactionID"] = np.arange(len(df)) + 1
    df["DeviceInfo"] = df["DeviceInfo"].astype("category")
    df["DeviceType"] = df["DeviceType"].astype("category")
    return df


def random_frame(n: int = 400, seed: int = 0) -> pd.DataFrame:
    """Few entities, coarse timestamps (many ties), NaNs in key and change columns."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {
            "TransactionID": rng.permutation(n) + 10_000,
            "TransactionDT": rng.integers(0, 40, n) * 1_800,
            "TransactionAmt": rng.choice([0.0, 5.0, 20.0, 99.5], n),
            "card1": rng.choice([1, 2, 3], n).astype(float),
            "card2": rng.choice([10.0, np.nan], n, p=[0.8, 0.2]),
            "card3": 150.0,
            "card4": "visa",
            "card5": 226.0,
            "card6": "debit",
            "addr1": rng.choice([100.0, 200.0, np.nan], n),
            "addr2": rng.choice([87.0, np.nan], n),
            "DeviceType": rng.choice(np.array(["mobile", "desktop", None], dtype=object), n),
            "DeviceInfo": rng.choice(
                np.array(["a", "b", None], dtype=object), n, p=[0.3, 0.3, 0.4]
            ),
            "isFraud": rng.integers(0, 2, n),
        }
    )
    df.loc[rng.choice(n, 10, replace=False).tolist(), "card1"] = np.nan
    return df


def brute_force(df: pd.DataFrame, config: HistoryConfig) -> pd.DataFrame:
    """O(n^2) reference implementation of the documented definitions."""
    keys = build_entity_keys(df)
    rows = []
    for i in df.index:
        t, amt, uid, card = (
            df.loc[i, "TransactionDT"],
            df.loc[i, "TransactionAmt"],
            keys.loc[i, USER_ID],
            keys.loc[i, CARD_ID],
        )
        r: dict[str, float] = dict.fromkeys(config.feature_names(), np.nan)
        if pd.notna(uid):
            hist = df[(keys[USER_ID] == uid) & (df["TransactionDT"] < t)]
            r["uid_prior_txn_count"] = len(hist)
            for label, w in config.windows:
                r[f"uid_txn_count_{label}"] = int((hist["TransactionDT"] >= t - w).sum())
            if len(hist):
                r["uid_seconds_since_prev_txn"] = t - hist["TransactionDT"].max()
                mean = hist["TransactionAmt"].mean()
                r["uid_prior_amt_mean"] = mean
                r["uid_amt_to_prior_mean"] = amt / mean if mean > 0 else np.nan
        if pd.notna(card):
            chist = df[(keys[CARD_ID] == card) & (df["TransactionDT"] < t)]
            chist = chist.sort_values(["TransactionDT", "TransactionID"])
            for col in config.change_columns:
                cur = df.loc[i, col]
                known = chist[col].dropna()
                if pd.notna(cur) and len(known):
                    r[f"card_{col.lower()}_changed"] = float(known.iloc[-1] != cur)
        rows.append(r)
    return pd.DataFrame(rows, index=df.index).astype("float32")


class TestEntityKey:
    def test_float_components_render_as_integers(self) -> None:
        keys = build_entity_keys(frame([{}]))
        assert keys.loc[0, USER_ID] == "1111|321|150|visa|226|debit|315"
        assert keys.loc[0, CARD_ID] == "1111|321|150|visa|226|debit"

    def test_missing_optional_component_uses_token(self) -> None:
        keys = build_entity_keys(frame([{"addr1": np.nan, "card4": None}]))
        assert keys.loc[0, USER_ID] == f"1111|321|150|{MISSING_TOKEN}|226|debit|{MISSING_TOKEN}"

    def test_missing_card1_gives_no_entity(self) -> None:
        keys = build_entity_keys(frame([{"card1": np.nan}]))
        assert keys.isna().all(axis=None)

    def test_matches_row_by_row_construction(self) -> None:
        df = random_frame(seed=6)
        df["card4"] = df["card4"].astype("category")
        cols = ("card1", "card2", "card4", "addr1")
        expected = [
            np.nan
            if pd.isna(r.card1)
            else "|".join(
                MISSING_TOKEN if pd.isna(v) else (str(int(v)) if isinstance(v, float) else str(v))
                for v in (r.card1, r.card2, r.card4, r.addr1)
            )
            for r in df.itertuples()
        ]
        got = build_entity_key(df, cols)
        pd.testing.assert_series_equal(got, pd.Series(expected, index=df.index, dtype=object))

    def test_small_and_large_frame_paths_agree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = random_frame(seed=12)
        cols = ("card1", "card2", "card4", "addr1")
        large = build_entity_key(df, cols)
        monkeypatch.setattr("src.features.entity.SMALL_FRAME_ROWS", len(df))
        pd.testing.assert_series_equal(build_entity_key(df, cols), large)

    def test_missing_column_is_rejected(self) -> None:
        with pytest.raises(SchemaError, match="addr1"):
            build_entity_key(frame([{}]).drop(columns="addr1"), ("card1", "addr1"))

    def test_huge_components_do_not_collide(self) -> None:
        """Regression: values beyond int64 all cast to one integer and shared one key."""
        keys = build_entity_keys(frame([{"addr1": 1e30}, {"addr1": 2e30}, {"addr1": 2.0**53}]))
        assert keys[USER_ID].nunique() == 3
        assert isinstance(k := keys.loc[2, USER_ID], str) and k.endswith(
            f"|{2**53}"
        )  # exact integers keep integer form


class TestHistoryCorrectness:
    def test_matches_brute_force_reference(self) -> None:
        df = random_frame()
        got = compute_history_features(df)[NAMES]
        pd.testing.assert_frame_equal(got, brute_force(df, HistoryConfig()), rtol=1e-5)

    def test_hand_computed_example(self) -> None:
        df = frame(
            [
                {"TransactionDT": 0, "TransactionAmt": 10.0, "DeviceInfo": "iOS"},
                {"TransactionDT": 1_800, "TransactionAmt": 30.0, "DeviceInfo": None},
                {"TransactionDT": 90_000, "TransactionAmt": 40.0, "DeviceInfo": "Android"},
            ]
        )
        f = compute_history_features(df)
        assert f["uid_prior_txn_count"].tolist() == [0, 1, 2]
        assert f["uid_txn_count_1h"].tolist() == [0, 1, 0]
        assert f["uid_txn_count_24h"].tolist() == [0, 1, 0]  # [3600, 90000) holds neither
        assert f["uid_txn_count_7d"].tolist() == [0, 1, 2]
        assert f["uid_seconds_since_prev_txn"].tolist()[1:] == [1_800, 88_200]
        assert f["uid_prior_amt_mean"].tolist()[1:] == [10.0, 20.0]
        assert f["uid_amt_to_prior_mean"].tolist()[1:] == [3.0, 2.0]
        # Row 2 compares against the last *known* device (iOS), skipping the missing one.
        changed = f["card_deviceinfo_changed"].tolist()
        assert np.isnan(changed[0]) and np.isnan(changed[1]) and changed[2] == 1.0

    def test_window_lower_bound_is_inclusive(self) -> None:
        df = frame([{"TransactionDT": 0}, {"TransactionDT": 3_600}, {"TransactionDT": 3_601}])
        assert compute_history_features(df)["uid_txn_count_1h"].tolist() == [0, 1, 1]

    def test_zero_prior_mean_gives_nan_ratio(self) -> None:
        df = frame(
            [
                {"TransactionDT": 0, "TransactionAmt": 0.0},
                {"TransactionDT": 1, "TransactionAmt": 5.0},
            ]
        )
        f = compute_history_features(df)
        assert f.loc[1, "uid_prior_amt_mean"] == 0.0 and pd.isna(f.loc[1, "uid_amt_to_prior_mean"])

    def test_location_change_is_tracked_per_card_not_uid(self) -> None:
        df = frame([{"TransactionDT": 0, "addr1": 100.0}, {"TransactionDT": 10, "addr1": 200.0}])
        f = compute_history_features(df)
        # Different addr1 -> different uid, so no uid history; the card still sees the move.
        assert f["uid_prior_txn_count"].tolist() == [0, 0]
        assert f.loc[1, "card_addr1_changed"] == 1.0


class TestLeakage:
    def test_appending_future_rows_does_not_change_past_features(self) -> None:
        df = random_frame(seed=1)
        cutoff = 20 * 1_800
        full = compute_history_features(df)
        past = df[df["TransactionDT"] < cutoff]
        pd.testing.assert_frame_equal(compute_history_features(past), full.loc[past.index])

    def test_features_ignore_labels(self) -> None:
        df = random_frame(seed=2)
        base = compute_history_features(df)
        flipped = df.assign(isFraud=1 - df["isFraud"])
        pd.testing.assert_frame_equal(compute_history_features(flipped), base)
        pd.testing.assert_frame_equal(compute_history_features(df.drop(columns="isFraud")), base)

    def test_no_feature_column_derived_from_label(self) -> None:
        out = compute_history_features(random_frame())
        assert "isFraud" not in out.columns
        assert all("fraud" not in c.lower() for c in out.columns)

    def test_current_amount_not_in_own_prior_mean(self) -> None:
        df = frame([{"TransactionDT": 0, "TransactionAmt": 1_000.0}])
        assert pd.isna(compute_history_features(df).loc[0, "uid_prior_amt_mean"])


class TestOrderingAndTies:
    def test_input_row_order_does_not_matter(self) -> None:
        df = random_frame(seed=3)
        base = compute_history_features(df)
        shuffled = df.sample(frac=1.0, random_state=0)
        pd.testing.assert_frame_equal(compute_history_features(shuffled).loc[df.index], base)

    def test_output_aligned_to_non_default_index(self) -> None:
        df = random_frame(seed=4)
        df.index = df.index * 7 + 3
        assert compute_history_features(df).index.equals(df.index)

    def test_duplicate_timestamps_do_not_see_each_other(self) -> None:
        df = frame(
            [
                {"TransactionDT": 100, "TransactionAmt": 10.0, "DeviceInfo": "a"},
                {"TransactionDT": 100, "TransactionAmt": 50.0, "DeviceInfo": "b"},
                {"TransactionDT": 200, "TransactionAmt": 30.0, "DeviceInfo": "b"},
            ]
        )
        f = compute_history_features(df)
        assert f["uid_prior_txn_count"].tolist() == [0, 0, 2]
        assert f.loc[2, "uid_prior_amt_mean"] == 30.0
        assert f["card_deviceinfo_changed"].iloc[:2].isna().all()  # rows 0 and 1
        # Among tied earlier rows, the higher TransactionID ("b") is the last known value.
        assert f.loc[2, "card_deviceinfo_changed"] == 0.0

    def test_tie_break_uses_transaction_id_not_row_position(self) -> None:
        rows = [
            {"TransactionID": 2, "TransactionDT": 100, "DeviceInfo": "a"},
            {"TransactionID": 1, "TransactionDT": 100, "DeviceInfo": "b"},
            {"TransactionID": 3, "TransactionDT": 200, "DeviceInfo": "a"},
        ]
        f = compute_history_features(frame(rows))
        assert f.loc[2, "card_deviceinfo_changed"] == 0.0  # last known = ID 2 = "a"


class TestMissingAndUnseen:
    def test_missing_card1_gives_nan_features(self) -> None:
        df = frame([{"TransactionDT": 0}, {"TransactionDT": 10, "card1": np.nan}])
        f = compute_history_features(df)
        assert f.loc[1][NAMES].isna().all()
        assert f.loc[0, "uid_prior_txn_count"] == 0

    def test_missing_card1_rows_do_not_form_a_shared_history(self) -> None:
        df = frame([{"TransactionDT": t, "card1": np.nan} for t in (0, 10, 20)])
        assert compute_history_features(df)[NAMES].isna().all(axis=None)

    def test_missing_addr1_rows_share_coarser_entity(self) -> None:
        df = frame([{"TransactionDT": 0, "addr1": np.nan}, {"TransactionDT": 10, "addr1": np.nan}])
        assert compute_history_features(df)["uid_prior_txn_count"].tolist() == [0, 1]

    def test_unseen_entity_has_zero_count_and_nan_history(self) -> None:
        df = frame([{"TransactionDT": 0}, {"TransactionDT": 10, "card1": 9999}])
        row = compute_history_features(df).loc[1]
        assert row["uid_prior_txn_count"] == 0 and row["uid_txn_count_30d"] == 0
        rest = [n for n in NAMES if "count" not in n]
        assert row[rest].isna().all()

    def test_missing_device_is_not_a_change(self) -> None:
        df = frame([{"TransactionDT": 0, "DeviceInfo": "a"}, {"TransactionDT": 10}])
        assert pd.isna(compute_history_features(df).loc[1, "card_deviceinfo_changed"])

    def test_empty_frame(self) -> None:
        out = compute_history_features(frame([{}]).iloc[:0])
        assert out.empty and set(NAMES) <= set(out.columns)


class TestValidation:
    @pytest.mark.parametrize("column", ["TransactionDT", "TransactionAmt", "addr1", "DeviceInfo"])
    def test_missing_input_column(self, column: str) -> None:
        with pytest.raises(SchemaError, match=column):
            compute_history_features(frame([{}]).drop(columns=column))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("TransactionDT", -1), ("TransactionDT", 1.5), ("TransactionAmt", -2.0)],
    )
    def test_invalid_values(self, field: str, value: float) -> None:
        with pytest.raises(SchemaError):
            compute_history_features(frame([{"TransactionDT": 0}, {field: value}]))

    def test_null_time(self) -> None:
        df = frame([{"TransactionDT": 0}, {"TransactionDT": np.nan}])
        with pytest.raises(SchemaError, match="null"):
            compute_history_features(df)

    def test_duplicate_transaction_id(self) -> None:
        df = frame([{"TransactionID": 5}, {"TransactionID": 5, "TransactionDT": 1}])
        with pytest.raises(SchemaError, match="unique"):
            compute_history_features(df)

    def test_bad_config(self) -> None:
        with pytest.raises(ValueError):
            HistoryConfig(windows=(("x", 0),))


class TestChronologicalSplit:
    def test_boundary_strictly_separates_time(self) -> None:
        df = random_frame(seed=5)
        train, valid, b = chronological_split(df, valid_fraction=0.25)
        assert train["TransactionDT"].max() < b.train_end <= valid["TransactionDT"].min()
        assert len(train) + len(valid) == len(df)
        assert not set(train.index) & set(valid.index)

    def test_duplicate_timestamps_never_straddle_boundary(self) -> None:
        df = frame([{"TransactionDT": t} for t in (0, 10, 10, 10, 10, 20)])
        train, valid, b = chronological_split(df, valid_fraction=0.5)
        assert b.train_end == 10
        assert train["TransactionDT"].tolist() == [0] and len(valid) == 5

    def test_gap_drops_rows_between_train_and_valid(self) -> None:
        df = frame([{"TransactionDT": t} for t in range(0, 100, 10)])
        train, valid, b = chronological_split(df, train_end=50, gap_seconds=20)
        assert train["TransactionDT"].max() == 40
        assert valid["TransactionDT"].min() == 70 and b.gap_seconds == 20

    def test_explicit_train_end(self) -> None:
        df = frame([{"TransactionDT": t} for t in range(5)])
        train, valid, _ = chronological_split(df, train_end=3)
        assert train["TransactionDT"].tolist() == [0, 1, 2]

    @pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
    def test_invalid_fraction(self, fraction: float) -> None:
        with pytest.raises(ValueError):
            time_cutoff(pd.Series([1, 2, 3]), fraction)

    def test_empty_side_is_rejected(self) -> None:
        df = frame([{"TransactionDT": 5} for _ in range(4)])
        with pytest.raises(ValueError, match="empty side"):
            chronological_split(df, valid_fraction=0.5)

    def test_negative_gap_rejected(self) -> None:
        with pytest.raises(ValueError):
            chronological_split(frame([{}]), gap_seconds=-1)


class TestCategoricalEncoder:
    def test_fit_on_train_only_and_unseen_maps_to_minus_one(self) -> None:
        train = pd.DataFrame({"c": ["b", "a", "b", None]})
        valid = pd.DataFrame({"c": ["a", "z", None]})
        enc = CategoricalEncoder(columns=("c",)).fit(train)
        assert enc.vocab_ == {"c": {"a": 0, "b": 1}}
        codes = enc.transform(valid)["c"].tolist()
        assert codes[:2] == [0.0, -1.0] and np.isnan(codes[2])

    def test_rare_categories_treated_as_unseen(self) -> None:
        enc = CategoricalEncoder(columns=("c",), min_count=2).fit(
            pd.DataFrame({"c": ["a", "a", "b"]})
        )
        assert enc.transform(pd.DataFrame({"c": ["a", "b"]}))["c"].tolist() == [0.0, -1.0]

    def test_transform_does_not_mutate_or_refit(self) -> None:
        train = pd.DataFrame({"c": ["a"]})
        valid = pd.DataFrame({"c": ["new"]})
        enc = CategoricalEncoder(columns=("c",)).fit(train)
        enc.transform(valid)
        assert valid["c"].tolist() == ["new"] and enc.vocab_ == {"c": {"a": 0}}

    def test_not_fitted(self) -> None:
        with pytest.raises(NotFittedError):
            CategoricalEncoder(columns=("c",)).transform(pd.DataFrame({"c": ["a"]}))

    def test_label_cannot_be_encoded(self) -> None:
        with pytest.raises(ValueError, match="isFraud"):
            CategoricalEncoder(columns=("isFraud",))

    def test_round_trip_serialisation(self) -> None:
        df = pd.DataFrame({"c": ["x", "y", None]})
        enc = CategoricalEncoder(columns=("c",)).fit(df)
        restored = CategoricalEncoder.from_dict(json.loads(json.dumps(enc.to_dict())))
        pd.testing.assert_frame_equal(restored.transform(df), enc.transform(df))


def test_build_main_on_synthetic_split(tmp_path: Path) -> None:
    txn = make_transactions(10)
    raw = write_split(tmp_path / "raw", txn, make_identity([1001, 1004]))
    out = tmp_path / "m.json"
    metrics = build.main(raw, out, valid_fraction=0.3)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(metrics))
    assert saved["split"]["rows"] == {"train": 7, "valid": 3}
    assert saved["split"]["max_train_dt_lt_min_valid_dt"] is True
    assert "TransactionID" not in out.read_text(encoding="utf-8")  # aggregates only


@pytest.mark.real_data
@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_real_data_features_are_point_in_time() -> None:
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    full = compute_history_features(df)
    assert set(NAMES) <= set(full.columns)
    assert (full["uid_prior_txn_count"] >= full["uid_txn_count_30d"]).all()
    # Recomputing on the first 30% of time must reproduce those rows exactly.
    cutoff = time_cutoff(df["TransactionDT"], 0.7)
    past = df[df["TransactionDT"] < cutoff]
    pd.testing.assert_frame_equal(compute_history_features(past), full.loc[past.index])
    train, valid, _ = chronological_split(full.join(df["TransactionDT"]), valid_fraction=0.2)
    assert train["TransactionDT"].max() < valid["TransactionDT"].min()
