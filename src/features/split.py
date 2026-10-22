"""Chronological train/validation split on ``TransactionDT``.

Train is ``TransactionDT < train_end``; validation is ``TransactionDT >= valid_start`` where
``valid_start = train_end + gap_seconds``. Rows sharing a timestamp always land on the same
side. Rows inside the gap are dropped (they would carry labels not yet observable at cutoff).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.features.history import TIME

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SplitBoundary:
    train_end: int  # exclusive
    valid_start: int  # inclusive

    @property
    def gap_seconds(self) -> int:
        return self.valid_start - self.train_end


def time_cutoff(dt: pd.Series[int], valid_fraction: float) -> int:
    """Smallest timestamp such that roughly ``valid_fraction`` of rows are at or after it."""
    if not 0 < valid_fraction < 1:
        raise ValueError(f"valid_fraction must be in (0, 1), got {valid_fraction}")
    if dt.empty or dt.isna().any():
        raise ValueError(f"{TIME} must be non-empty and non-null")
    values = np.sort(dt.to_numpy())
    return int(values[int(np.floor(len(values) * (1 - valid_fraction)))])


def chronological_split(
    df: pd.DataFrame,
    *,
    valid_fraction: float = 0.2,
    gap_seconds: int = 0,
    train_end: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, SplitBoundary]:
    """Split ``df`` by time. ``train_end`` overrides ``valid_fraction`` when given."""
    if gap_seconds < 0:
        raise ValueError("gap_seconds must be >= 0")
    if TIME not in df.columns:
        raise ValueError(f"frame has no {TIME} column")
    end = time_cutoff(df[TIME], valid_fraction) if train_end is None else int(train_end)
    boundary = SplitBoundary(train_end=end, valid_start=end + gap_seconds)

    t = df[TIME]
    train_mask = t < boundary.train_end
    valid_mask = t >= boundary.valid_start
    if not train_mask.any() or not valid_mask.any():
        raise ValueError(
            f"split at {boundary} leaves an empty side "
            f"(train={int(train_mask.sum())}, valid={int(valid_mask.sum())})"
        )
    dropped = len(df) - int(train_mask.sum()) - int(valid_mask.sum())
    logger.info(
        "chronological split train_end=%d valid_start=%d train=%d valid=%d gap_dropped=%d",
        boundary.train_end,
        boundary.valid_start,
        int(train_mask.sum()),
        int(valid_mask.sum()),
        dropped,
    )
    return df.loc[train_mask], df.loc[valid_mask], boundary
