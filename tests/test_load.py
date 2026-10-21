from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from src.data.load import (
    DEFAULT_RAW_DIR,
    HAS_IDENTITY,
    DataFileNotFoundError,
    DuplicateKeyError,
    SchemaError,
    load_identity,
    load_ieee_cis,
    load_transactions,
    merge_transaction_identity,
)
from tests.conftest import make_identity, make_transactions, write_split


class TestValidInput:
    def test_loads_and_merges_train_split(self, raw_dir: Path) -> None:
        df = load_ieee_cis(raw_dir)
        assert len(df) == 6
        assert df["TransactionID"].is_unique
        assert df["isFraud"].dtype == "int8"
        assert df["isFraud"].sum() == 1

    def test_dtypes_are_optimized(self, raw_dir: Path) -> None:
        df = load_ieee_cis(raw_dir)
        assert df["TransactionAmt"].dtype == "float32"
        assert df["TransactionID"].dtype == "int64"
        assert isinstance(df["ProductCD"].dtype, pd.CategoricalDtype)
        assert isinstance(df["DeviceType"].dtype, pd.CategoricalDtype)

    def test_nullable_feature_columns_keep_nans(self, raw_dir: Path) -> None:
        df = load_ieee_cis(raw_dir)
        assert df["card2"].isna().sum() == 1
        assert df["dist1"].isna().all()

    def test_usecols_limits_extra_columns_but_keeps_required(self, raw_dir: Path) -> None:
        df = load_transactions(raw_dir / "train_transaction.csv", usecols=["TransactionDT"])
        assert "V1" not in df.columns
        assert {"TransactionID", "isFraud", "card1", "P_emaildomain"} <= set(df.columns)

    def test_test_split_without_label_and_hyphenated_identity(self, tmp_path: Path) -> None:
        raw = write_split(
            tmp_path,
            make_transactions(4, with_label=False),
            make_identity([1000], hyphenated=True),
            split="test",
        )
        df = load_ieee_cis(raw, split="test")
        assert "isFraud" not in df.columns
        assert "id_01" in df.columns and "id-01" not in df.columns


class TestChunkedRead:
    def test_chunk_boundaries_do_not_change_result(
        self, raw_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expected = load_ieee_cis(raw_dir)
        # card2 is NaN only in the first chunk; addr1 only in the last; ints mix with NaN floats.
        monkeypatch.setattr("src.data.load.CSV_CHUNK_ROWS", 1)
        chunked = load_ieee_cis(raw_dir)
        pd.testing.assert_frame_equal(chunked, expected)
        assert chunked["card2"].dtype == "float32"
        assert isinstance(chunked["P_emaildomain"].dtype, pd.CategoricalDtype)

    def test_non_numeric_value_in_later_chunk_is_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        df = make_transactions()
        df["TransactionAmt"] = df["TransactionAmt"].astype(object)
        df.loc[5, "TransactionAmt"] = "n/a-text"
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        monkeypatch.setattr("src.data.load.CSV_CHUNK_ROWS", 2)
        with pytest.raises(SchemaError, match="non-numeric.*TransactionAmt"):
            load_transactions(path)


class TestMissingFiles:
    def test_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(DataFileNotFoundError, match="train_transaction.csv"):
            load_ieee_cis(tmp_path / "does-not-exist")

    def test_missing_identity_file(self, raw_dir: Path) -> None:
        (raw_dir / "train_identity.csv").unlink()
        with pytest.raises(DataFileNotFoundError, match="train_identity.csv"):
            load_ieee_cis(raw_dir)

    def test_error_is_a_file_not_found_error_with_download_hint(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="kaggle.com"):
            load_transactions(tmp_path / "train_transaction.csv")

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "train_transaction.csv"
        path.write_text("")
        with pytest.raises(SchemaError, match="empty"):
            load_transactions(path)


class TestSchemaValidation:
    @pytest.mark.parametrize("column", ["TransactionID", "TransactionDT", "isFraud", "card1"])
    def test_missing_required_transaction_column(self, tmp_path: Path, column: str) -> None:
        path = tmp_path / "t.csv"
        make_transactions().drop(columns=column).to_csv(path, index=False)
        with pytest.raises(SchemaError, match=f"missing required columns: .*'{column}'"):
            load_transactions(path)

    def test_missing_required_identity_column(self, tmp_path: Path) -> None:
        path = tmp_path / "i.csv"
        make_identity([1]).drop(columns="DeviceInfo").to_csv(path, index=False)
        with pytest.raises(SchemaError, match="DeviceInfo"):
            load_identity(path)

    def test_non_numeric_amount(self, tmp_path: Path) -> None:
        df = make_transactions()
        df["TransactionAmt"] = df["TransactionAmt"].astype(object)
        df.loc[0, "TransactionAmt"] = "ten"
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        with pytest.raises(SchemaError, match="non-numeric.*TransactionAmt"):
            load_transactions(path)

    def test_invalid_label_values(self, tmp_path: Path) -> None:
        df = make_transactions()
        df.loc[0, "isFraud"] = 2
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        with pytest.raises(SchemaError, match="invalid isFraud"):
            load_transactions(path)

    def test_null_label(self, tmp_path: Path) -> None:
        df = make_transactions()
        df["isFraud"] = df["isFraud"].astype(float)
        df.loc[0, "isFraud"] = None
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        with pytest.raises(SchemaError, match="nulls.*isFraud"):
            load_transactions(path)

    def test_negative_amount(self, tmp_path: Path) -> None:
        df = make_transactions()
        df.loc[0, "TransactionAmt"] = -1.0
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        with pytest.raises(SchemaError, match="negative"):
            load_transactions(path)


class TestDuplicateKeys:
    def test_duplicate_transaction_ids(self, tmp_path: Path) -> None:
        df = make_transactions()
        df.loc[1, "TransactionID"] = df.loc[0, "TransactionID"]
        path = tmp_path / "t.csv"
        df.to_csv(path, index=False)
        with pytest.raises(DuplicateKeyError, match="2 rows with duplicate TransactionID.*1000"):
            load_transactions(path)

    def test_duplicate_identity_ids_fail_before_merge(self, tmp_path: Path) -> None:
        raw = write_split(tmp_path, make_transactions(), make_identity([1001, 1001]))
        with pytest.raises(DuplicateKeyError, match="train_identity.csv"):
            load_ieee_cis(raw)


class TestMerge:
    def test_left_join_preserves_all_transactions_and_order(self, raw_dir: Path) -> None:
        txn = load_transactions(raw_dir / "train_transaction.csv")
        merged = merge_transaction_identity(txn, load_identity(raw_dir / "train_identity.csv"))
        assert merged["TransactionID"].tolist() == txn["TransactionID"].tolist()
        assert merged["isFraud"].tolist() == txn["isFraud"].tolist()

    def test_has_identity_flag_and_missing_device_fields(self, raw_dir: Path) -> None:
        df = load_ieee_cis(raw_dir).set_index("TransactionID")
        assert df[HAS_IDENTITY].dtype == bool
        assert df.loc[[1001, 1003, 1005], HAS_IDENTITY].all()
        assert not df.loc[[1000, 1002, 1004], HAS_IDENTITY].any()
        assert df.loc[[1000, 1002, 1004], "DeviceType"].isna().all()
        assert (df.loc[[1001, 1003, 1005], "DeviceType"] == "mobile").all()

    def test_orphan_identity_rows_are_dropped_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = write_split(tmp_path, make_transactions(), make_identity([1001, 9999]))
        with caplog.at_level(logging.WARNING, logger="src.data.load"):
            df = load_ieee_cis(raw)
        assert len(df) == 6
        assert 9999 not in df["TransactionID"].to_numpy()
        assert "count=1" in caplog.text

    def test_overlapping_non_key_columns_rejected(self) -> None:
        txn = make_transactions()
        ident = make_identity([1000]).assign(TransactionAmt=1.0)
        with pytest.raises(SchemaError, match="TransactionAmt"):
            merge_transaction_identity(txn, ident)

    def test_merge_rejects_duplicates_passed_directly(self) -> None:
        txn = make_transactions()
        ident = make_identity([1000, 1000])
        with pytest.raises(pd.errors.MergeError):
            merge_transaction_identity(txn, ident)


@pytest.mark.real_data
@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_real_train_split_loads() -> None:
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    assert len(df) > 0
    assert df["TransactionID"].is_unique
