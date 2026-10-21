"""Point-in-time historical features per entity.

Leakage rule: a row's features use only rows of the same entity with a **strictly earlier**
``TransactionDT``. Rows sharing a timestamp never see each other, so results do not depend on
input row order. Labels are never read. Features for a row are therefore unchanged when any
later rows are appended, which is what the serving path will also guarantee.

Implementation: rows are sorted by (entity, time, TransactionID) and encoded as one int64
composite key per row; every lookup is a vectorised ``np.searchsorted`` (O(n log n), CPU only).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.data.load import KEY, SchemaError
from src.features.entity import CARD_ID, USER_COLUMNS, USER_ID, build_entity_keys

logger = logging.getLogger(__name__)

TIME = "TransactionDT"
AMOUNT = "TransactionAmt"

DEFAULT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("1h", 3_600),
    ("24h", 86_400),
    ("7d", 7 * 86_400),
    ("30d", 30 * 86_400),
)
# Compared against the card's last known value; addr1 is part of uid, so changes are tracked
# on the coarser card_id.
DEFAULT_CHANGE_COLUMNS = ("DeviceInfo", "DeviceType", "addr1", "addr2")


@dataclass(frozen=True)
class HistoryConfig:
    windows: tuple[tuple[str, int], ...] = DEFAULT_WINDOWS
    change_columns: tuple[str, ...] = DEFAULT_CHANGE_COLUMNS

    def __post_init__(self) -> None:
        if any(seconds <= 0 for _, seconds in self.windows):
            raise ValueError("window lengths must be positive seconds")
        if len({label for label, _ in self.windows}) != len(self.windows):
            raise ValueError("window labels must be unique")

    def feature_names(self) -> list[str]:
        return [
            "uid_prior_txn_count",
            *(f"uid_txn_count_{label}" for label, _ in self.windows),
            "uid_seconds_since_prev_txn",
            "uid_prior_amt_mean",
            "uid_amt_to_prior_mean",
            *(f"card_{c.lower()}_changed" for c in self.change_columns),
        ]


@dataclass(frozen=True)
class _PointInTimeIndex:
    """Sorted view of one entity level. Arrays other than ``order`` are in sorted space."""

    order: np.ndarray  # input positions in sorted order
    entity: np.ndarray  # entity code + 1 (0 = missing entity)
    keys: np.ndarray  # composite (entity, time) keys
    start: np.ndarray  # index of the entity's first row
    left: np.ndarray  # index of the entity's first row at the same timestamp

    @classmethod
    def build(cls, entity: pd.Series, t: np.ndarray, tid: np.ndarray) -> _PointInTimeIndex:
        codes, _ = pd.factorize(entity, use_na_sentinel=True)
        order = np.lexsort((tid, t, codes))
        t0 = int(t.min())
        span = int(t.max()) - t0 + 1
        if (int(codes.max()) + 2) * span >= np.iinfo(np.int64).max:
            raise OverflowError("entity count x time span overflows int64 composite keys")
        ent = codes[order].astype(np.int64) + 1
        keys = ent * span + (t[order] - t0)
        start = np.searchsorted(keys, ent * span, side="left")
        left = np.searchsorted(keys, keys, side="left")
        return cls(order=order, entity=ent, keys=keys, start=start, left=left)

    @property
    def prior_count(self) -> np.ndarray:
        return self.left - self.start

    def window_count(self, seconds: int) -> np.ndarray:
        """Rows in ``[t - seconds, t)`` for the same entity."""
        lo = np.maximum(np.searchsorted(self.keys, self.keys - seconds, side="left"), self.start)
        return self.left - lo

    def prior_sum(self, values: np.ndarray) -> np.ndarray:
        prefix = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
        return prefix[self.left] - prefix[self.start]

    def last_known_before(self, values: np.ndarray) -> np.ndarray:
        """Most recent non-NaN value among strictly earlier rows (ties by TransactionID)."""
        filled = pd.Series(values).groupby(self.entity).ffill().to_numpy(dtype=np.float64)
        out = np.full(len(values), np.nan)
        has = self.prior_count > 0
        out[has] = filled[self.left[has] - 1]
        return out

    def to_input_order(self, sorted_values: np.ndarray) -> np.ndarray:
        out = np.empty(len(sorted_values), dtype=np.float64)
        out[self.order] = sorted_values
        # Rows without an entity have no history.
        out[self.order[self.entity == 0]] = np.nan
        return out


def _validate(df: pd.DataFrame, config: HistoryConfig) -> None:
    required = {KEY, TIME, AMOUNT, *USER_COLUMNS, *config.change_columns}
    missing = sorted(required - set(df.columns))
    if missing:
        raise SchemaError(f"history features need missing columns: {missing}")
    for col in (KEY, TIME, AMOUNT):
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise SchemaError(f"{col} must be numeric")
        if df[col].isna().any():
            raise SchemaError(f"{col} must not contain nulls")
    t = df[TIME].to_numpy()
    if (t < 0).any() or (t != np.floor(t)).any():
        raise SchemaError(f"{TIME} must be non-negative integer seconds")
    amt = df[AMOUNT].to_numpy(dtype=np.float64)
    if not np.isfinite(amt).all() or (amt < 0).any():
        raise SchemaError(f"{AMOUNT} must be finite and non-negative")
    if not df[KEY].is_unique:
        raise SchemaError(f"{KEY} must be unique")


def _value_codes(s: pd.Series) -> np.ndarray:
    """Equality-preserving float encoding with NaN for missing (no fitted state)."""
    if pd.api.types.is_numeric_dtype(s):
        return s.to_numpy(dtype=np.float64, na_value=np.nan)
    codes, _ = pd.factorize(s, use_na_sentinel=True)
    return np.where(codes < 0, np.nan, codes).astype(np.float64)


def _change_flags(
    df: pd.DataFrame,
    entity: pd.Series,
    t: np.ndarray,
    tid: np.ndarray,
    columns: tuple[str, ...],
    prefix: str,
) -> dict[str, np.ndarray]:
    idx = _PointInTimeIndex.build(entity, t, tid)
    out = {}
    for col in columns:
        cur = _value_codes(df[col])[idx.order]
        prev = idx.last_known_before(cur)
        known = ~np.isnan(cur) & ~np.isnan(prev)
        changed = np.where(known, (cur != prev).astype(np.float64), np.nan)
        out[f"{prefix}_{col.lower()}_changed"] = idx.to_input_order(changed)
    return out


def compute_change_flags(
    df: pd.DataFrame, entity: pd.Series, columns: tuple[str, ...], prefix: str
) -> pd.DataFrame:
    """``<prefix>_<col>_changed`` flags for an arbitrary entity key aligned to ``df.index``.

    Same point-in-time rule and definition as the ``card_*_changed`` features.
    """
    missing = sorted({KEY, TIME, *columns} - set(df.columns))
    if missing:
        raise SchemaError(f"change flags need missing columns: {missing}")
    if not entity.index.equals(df.index):
        raise ValueError("entity must be aligned to df.index")
    if df.empty:
        names = [f"{prefix}_{c.lower()}_changed" for c in columns]
        return pd.DataFrame(columns=names, index=df.index, dtype="float32")
    t = df[TIME].to_numpy(dtype=np.int64)
    tid = df[KEY].to_numpy(dtype=np.int64)
    flags = _change_flags(df, entity, t, tid, columns, prefix)
    return pd.DataFrame(flags, index=df.index).astype("float32")


def compute_history_features(
    df: pd.DataFrame,
    config: HistoryConfig | None = None,
    *,
    keys: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return entity keys plus point-in-time features, aligned to ``df.index``.

    Feature definitions (``uid`` level unless noted):

    * ``uid_prior_txn_count``: earlier transactions of the entity.
    * ``uid_txn_count_<w>``: earlier transactions in ``[t - w, t)``.
    * ``uid_seconds_since_prev_txn``: ``t`` minus the latest earlier timestamp.
    * ``uid_prior_amt_mean``: mean ``TransactionAmt`` of earlier transactions.
    * ``uid_amt_to_prior_mean``: current amount / ``uid_prior_amt_mean``.
    * ``card_<col>_changed``: 1 if ``col`` differs from the card's last known earlier value,
      0 if equal, NaN if either value is missing.

    No history (first sighting) gives count 0 and NaN for the other features. A missing entity
    key gives NaN everywhere.

    ``keys`` optionally replaces the default proxies: a frame aligned to ``df.index`` with
    ``uid`` and ``card_id`` columns (e.g. the opt-in ``uid_d1`` / ``card_d1`` renamed). Feature
    names stay the same so variants are directly comparable.
    """
    config = config or HistoryConfig()
    _validate(df, config)
    names = config.feature_names()
    if keys is None:
        keys = build_entity_keys(df)
    elif not keys.index.equals(df.index) or not {USER_ID, CARD_ID} <= set(keys.columns):
        raise ValueError(f"keys must be aligned to df.index with {USER_ID} and {CARD_ID} columns")
    keys = keys[[CARD_ID, USER_ID]]
    if df.empty:
        return pd.concat(
            [keys, pd.DataFrame(columns=names, index=df.index, dtype="float32")], axis=1
        )

    t = df[TIME].to_numpy(dtype=np.int64)
    tid = df[KEY].to_numpy(dtype=np.int64)
    feats: dict[str, np.ndarray] = {}

    uid = _PointInTimeIndex.build(keys[USER_ID], t, tid)
    prior = uid.prior_count.astype(np.float64)
    feats["uid_prior_txn_count"] = uid.to_input_order(prior)
    for label, seconds in config.windows:
        feats[f"uid_txn_count_{label}"] = uid.to_input_order(uid.window_count(seconds))

    t_sorted = t[uid.order]
    has = uid.prior_count > 0
    since = np.full(len(t), np.nan)
    since[has] = t_sorted[has] - t_sorted[uid.left[has] - 1]
    feats["uid_seconds_since_prev_txn"] = uid.to_input_order(since)

    amt_sorted = df[AMOUNT].to_numpy(dtype=np.float64)[uid.order]
    mean = np.divide(uid.prior_sum(amt_sorted), prior, out=np.full(len(t), np.nan), where=has)
    ratio = np.divide(amt_sorted, mean, out=np.full(len(t), np.nan), where=has & (mean > 0))
    feats["uid_prior_amt_mean"] = uid.to_input_order(mean)
    feats["uid_amt_to_prior_mean"] = uid.to_input_order(ratio)

    feats.update(_change_flags(df, keys[CARD_ID], t, tid, config.change_columns, "card"))

    out = pd.DataFrame(feats, index=df.index).astype("float32")[names]
    logger.info(
        "computed history features rows=%d uids=%d cards=%d features=%d",
        len(df),
        keys[USER_ID].nunique(),
        keys[CARD_ID].nunique(),
        len(names),
    )
    return pd.concat([keys, out], axis=1)
