"""Training frame from the Feast offline store equals the pandas (batch) frame exactly."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("feast")

from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis  # noqa: E402
from src.features.feast_training import (  # noqa: E402
    compare_frames,
    feast_model_frame,
    frame_sha256,
)
from src.features.history import TIME, HistoryConfig  # noqa: E402
from src.features.split import chronological_split  # noqa: E402
from src.models.compare import build_model_frame  # noqa: E402
from src.models.train import model_frame  # noqa: E402

CONFIG = HistoryConfig()


def assert_identical(df: pd.DataFrame, repo: Path, chunk_rows: int) -> None:
    """Small chunks, so retrieval crosses many chunk boundaries."""
    pandas_frame = build_model_frame(df, "baseline", CONFIG)
    feast_frame = feast_model_frame(df, CONFIG, repo, chunk_rows=chunk_rows)
    p_train, _, _ = chronological_split(pandas_frame, valid_fraction=0.2)
    f_train, _, _ = chronological_split(feast_frame, valid_fraction=0.2)
    assert len(p_train) > 0 and len(f_train) > 0
    print(f"rows pandas={len(pandas_frame)} feast={len(feast_frame)} train={len(p_train)}")
    result = compare_frames(pandas_frame, feast_frame)
    assert result["identical"], result
    assert compare_frames(p_train, f_train)["identical"]
    assert frame_sha256(pandas_frame) == frame_sha256(feast_frame)


def test_feast_training_frame_equals_pandas_on_synthetic(
    synthetic_frame: pd.DataFrame, tmp_path: Path
) -> None:
    assert_identical(synthetic_frame, tmp_path, chunk_rows=400)


@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_feast_training_frame_equals_pandas_on_real_slice(tmp_path: Path) -> None:
    """The earliest 3,000 real transactions (a slice is self-contained: history is earlier)."""
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    slice_ = df.sort_values([TIME, "TransactionID"]).head(3_000)
    assert slice_[TIME].duplicated().any()  # same-timestamp rows, some split across chunks
    assert_identical(slice_, tmp_path, chunk_rows=250)


def test_compare_frames_reports_differences() -> None:
    a = pd.DataFrame({"x": [1.0, float("nan"), 3.0], "y": [1, 2, 3]})
    b = a.copy()
    b.loc[2, "x"] = 3.5
    result = compare_frames(a, b)
    assert not result["identical"]
    assert result["differing_columns"] == ["x"] and result["max_abs_diff"] == {"x": 0.5}
    assert compare_frames(a, a.copy())["identical"]
    with pytest.raises(ValueError, match="empty"):
        compare_frames(a.iloc[:0], a)


def test_model_frame_rejects_unknown_source(synthetic_frame: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="feature source"):
        model_frame(synthetic_frame, "parquet")
