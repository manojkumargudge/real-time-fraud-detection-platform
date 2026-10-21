from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.data.eda import column_family, compute_eda_metrics, main, merge_integrity
from src.data.load import IEEECISPaths, load_identity, load_ieee_cis, load_transactions
from src.data.load import merge_transaction_identity as merge


@pytest.mark.parametrize(
    ("name", "family"),
    [("V123", "V"), ("id_05", "id"), ("card1", "card"), ("DeviceInfo", "DeviceInfo")],
)
def test_column_family(name: str, family: str) -> None:
    assert column_family(name) == family


def test_metrics_on_synthetic_fixture(raw_dir: Path) -> None:
    m = compute_eda_metrics(load_ieee_cis(raw_dir))
    assert m["overview"]["rows"] == 6
    assert m["class_balance"] == {
        "fraud_count": 1,
        "legit_count": 5,
        "fraud_rate": round(1 / 6, 5),
        "neg_pos_ratio": 5.0,
    }
    assert m["identity_coverage"]["has_identity_rate"] == 0.5
    assert m["missingness"]["top_10_missing"]["dist1"] == 1.0
    assert m["feature_availability"]["location_change"]["all_present_rate"] == 0.0
    assert m["transaction_dt"]["distinct_days"] == 1


def test_merge_integrity_counts_orphans(tmp_path: Path) -> None:
    from tests.conftest import make_identity, make_transactions, write_split

    paths = IEEECISPaths.from_dir(
        write_split(tmp_path, make_transactions(), make_identity([1001, 9999]))
    )
    txn, ident = load_transactions(paths.transaction), load_identity(paths.identity)
    result = merge_integrity(txn, ident, merge(txn, ident))
    assert result["row_count_preserved"] is True
    assert result["identity_rows_matched"] == 1
    assert result["identity_rows_orphaned"] == 1


def test_main_writes_json_without_raw_rows(raw_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "eda_metrics.json"
    main(raw_dir, out)
    data = json.loads(out.read_text())
    assert data["merge_integrity"]["merged_rows"] == 6
    assert "1000" not in out.read_text()  # no TransactionIDs leak into the report
