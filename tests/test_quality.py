from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.data import quality
from tests.conftest import make_identity, make_transactions, write_split


def test_duplicate_rows_ignoring_key() -> None:
    df = make_transactions(4)
    df.loc[3, df.columns.drop("TransactionID")] = df.loc[2, df.columns.drop("TransactionID")]
    assert quality.duplicate_rows_ignoring_key(df) == 1
    assert quality.duplicate_rows_ignoring_key(make_transactions(4)) == 0


def test_peak_memory_is_measured() -> None:
    mb = quality.peak_memory_mb()
    assert mb is None or mb > 1  # a running Python process uses more than 1 MB


def test_report_on_synthetic_split(tmp_path: Path) -> None:
    raw = write_split(tmp_path / "raw", make_transactions(), make_identity([1001, 1003, 9999]))
    r = quality.build_report(raw)
    m = r["merge"]
    assert m["transaction_rows"] == m["merged_rows"] == 6 and m["row_count_preserved"]
    assert m["identity_rows_matched"] == 2 and m["identity_rows_orphaned"] == 1
    assert r["files"]["transaction"]["transaction_id_unique"]
    assert r["values"]["label_values"] == {"0": 5, "1": 1}
    assert r["values"]["amount_negative_rows"] == 0
    assert set(r["execution"]["seconds"]) >= {"load_transactions", "merge", "total"}


def test_main_writes_aggregates_only(raw_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    quality.main(raw_dir, out)
    text = (out / "data_quality.json").read_text(encoding="utf-8")
    json.loads(text)
    md = (out / "data_quality.md").read_text(encoding="utf-8")
    ids = [str(i) for i in pd.read_csv(raw_dir / "train_transaction.csv")["TransactionID"]]
    assert not any(i in text or i in md for i in ids)  # no row identifiers
    assert "gmail.com" not in text and "iOS Device" not in text  # no raw values


def test_family_rates_keep_precision_for_display() -> None:
    # 0.581513 must render as 58.2%, not 58.1% via an intermediate 4-decimal round (0.5815).
    df = pd.DataFrame({"D1": [None] * 581_513 + [1.0] * 418_487})
    rate = quality.missingness_by_family(df)["D"]["mean_missing"]
    assert rate == 0.581513 and f"{100 * rate:.1f}" == "58.2"
