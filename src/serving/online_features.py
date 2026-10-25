"""Online feature path: the batch feature code applied to one event plus its stored history.

For an event at time ``t`` on card ``c`` the store returns earlier events of ``c``
(``TransactionDT < t``). Every ``uid`` is a sub-group of one ``card_id`` (uid = card_id +
addr1), so this single fetch covers both the uid-level history and the card-level change
flags. The frame is then passed through ``compare.build_model_frame`` -- the same function
used in training -- and the event's row is returned. Train/serve parity is tested.

Limitations: history contains only events this service has ingested (cold start), events
that arrive late are missing from the history of events scored before them, and the cost
grows with the card's history length (no incremental aggregates yet).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.data.load import HAS_IDENTITY, KEY
from src.features.entity import CARD_ID, USER_ID, build_entity_keys
from src.features.history import TIME, HistoryConfig
from src.models.compare import build_model_frame, raw_model_columns

NUMERIC_FIELDS = (
    KEY,
    TIME,
    "TransactionAmt",
    "card1",
    "card2",
    "card3",
    "card5",
    "addr1",
    "addr2",
    "dist1",
)
TEXT_FIELDS = (
    "ProductCD",
    "card4",
    "card6",
    "P_emaildomain",
    "R_emaildomain",
    "DeviceType",
    "DeviceInfo",
)
EVENT_FIELDS = (*NUMERIC_FIELDS, *TEXT_FIELDS, HAS_IDENTITY)


def events_to_frame(events: list[dict[str, Any]]) -> pd.DataFrame:
    """Build a loader-shaped frame from event dicts (missing values -> NaN/None)."""
    frame = pd.DataFrame.from_records(events, columns=list(EVENT_FIELDS))
    for col in NUMERIC_FIELDS:
        frame[col] = pd.to_numeric(frame[col], errors="raise").astype("float64")
    frame[KEY] = frame[KEY].astype("int64")
    frame[TIME] = frame[TIME].astype("int64")
    for col in TEXT_FIELDS:
        frame[col] = frame[col].astype(object).where(frame[col].notna(), None)
    frame[HAS_IDENTITY] = frame[HAS_IDENTITY].fillna(False).astype(bool)
    return frame


def entity_keys(event: dict[str, Any]) -> tuple[str | None, str | None]:
    return entity_keys_from_frame(events_to_frame([event]))


def entity_keys_from_frame(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    """Keys of a one-event frame from ``events_to_frame`` (lets the scorer build it once)."""
    keys = build_entity_keys(frame).iloc[0]
    card, uid = keys[CARD_ID], keys[USER_ID]
    return (None if pd.isna(card) else card), (None if pd.isna(uid) else uid)


def online_model_row(
    event: dict[str, Any], history: list[dict[str, Any]], config: HistoryConfig
) -> pd.DataFrame:
    """Model-frame row for ``event`` using only strictly earlier ``history`` events."""
    t, tid = event[TIME], event[KEY]
    prior = [h for h in history if h[TIME] < t and h[KEY] != tid]  # defensive re-check
    frame = events_to_frame([*prior, event])
    model_frame = build_model_frame(frame, "baseline", config)
    row = model_frame.iloc[[-1]]
    if int(row[KEY].iloc[0]) != tid:
        raise RuntimeError("online feature row does not belong to the scored event")
    return row.reset_index(drop=True)


def model_row_from_features(
    frame: pd.DataFrame, features: dict[str, float], config: HistoryConfig
) -> pd.DataFrame:
    """Model-frame row for a one-event ``frame`` (``events_to_frame``) with history features
    computed elsewhere (Feast).

    Same columns, order and dtypes as ``build_model_frame``: the raw columns come from the
    function it uses (``raw_model_columns``), the history columns are float32. The batch
    history computation is skipped because Feast replaces all of its values (tested equal).
    """
    raw = raw_model_columns(frame).reset_index(drop=True)
    hist = pd.DataFrame(
        {n: np.array([features[n]], dtype=np.float32) for n in config.feature_names()}
    )
    return pd.concat([raw, hist], axis=1)


def history_feature_values(row: pd.DataFrame, config: HistoryConfig) -> dict[str, float | None]:
    values = row[config.feature_names()].iloc[0].to_numpy(dtype=np.float64)
    return {
        n: (None if np.isnan(v) else float(v))
        for n, v in zip(config.feature_names(), values, strict=True)
    }
