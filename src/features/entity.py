"""Entity keys used as a customer proxy for IEEE-CIS.

IEEE-CIS has no customer or account ID. Two proxy keys are built from anonymised card and
address fields:

* ``card_id`` -- ``card1..card6`` (issuer/bin/network/type fingerprint of a payment card).
* ``uid``     -- ``card_id`` + ``addr1`` (billing region), the "user" proxy.

Neither key is guaranteed to identify one person: different customers can share all fields
(collision), and one customer can appear under several keys (fragmentation). See
docs/features.md.

Missing-value policy (deterministic):

* ``card1`` missing -> the key is NaN; the row has no entity and gets NaN history features.
* any other component missing -> the literal token ``<NA>`` is used for that component, so
  rows missing e.g. ``addr1`` share a coarser key with the same card fields.

Opt-in finer proxy (experimental, not used by the default features): ``card_d1`` / ``uid_d1``
append an estimated account-start day, ``TransactionDT // 86400 - D1``. D1 is an anonymised
day count whose meaning is not documented by the provider; it is treated as "days since the
card/account was first seen" only because that reading measurably groups rows more coherently
(see docs/features.md). The day index is relative, not a calendar date. Missing ``D1`` gives
the ``<NA>`` token, so such rows fall back to the coarser ``card_id`` / ``uid`` grouping.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.load import SchemaError

CARD_COLUMNS = ("card1", "card2", "card3", "card4", "card5", "card6")
USER_COLUMNS = (*CARD_COLUMNS, "addr1")
REQUIRED_COMPONENTS = ("card1",)

CARD_ID = "card_id"
USER_ID = "uid"
CARD_START_ID = "card_d1"
USER_START_ID = "uid_d1"
START_DAY = "account_start_day"
SECONDS_PER_DAY = 86_400
MISSING_TOKEN = "<NA>"
SEPARATOR = "|"
SMALL_FRAME_ROWS = 64


def _component_to_str(s: pd.Series) -> pd.Series:
    """Render one key component; integral floats print without ``.0`` (321.0 -> "321")."""
    out = pd.Series(MISSING_TOKEN, index=s.index, dtype=object)
    present = s.notna().to_numpy()
    if pd.api.types.is_numeric_dtype(s):
        v = s.to_numpy(dtype="float64")[present]
        integral = np.isfinite(v) & (v == np.floor(v))
        as_int = np.where(integral, v, 0).astype("int64").astype(str)
        rendered = np.where(integral, as_int, v.astype(str))
        out[present] = rendered
    else:
        out[present] = s[present].astype(str).to_numpy()
    return out


def build_entity_key(
    df: pd.DataFrame,
    columns: tuple[str, ...],
    *,
    required: tuple[str, ...] = REQUIRED_COMPONENTS,
    name: str | None = None,
) -> pd.Series:
    """Concatenate ``columns`` into one string key per row (NaN if a ``required`` part is NaN)."""
    missing = sorted(set(columns) - set(df.columns))
    if missing:
        raise SchemaError(f"entity key columns missing from frame: {missing}")
    if not set(required) <= set(columns):
        raise ValueError(f"required components {required} must be a subset of {columns}")
    if df.empty:
        return pd.Series(np.nan, index=df.index, dtype=object, name=name)
    sub = df[list(columns)]
    if len(sub) <= SMALL_FRAME_ROWS:  # online path: grouping costs more than it saves
        return _render_keys(sub, columns, required).rename(name)
    # Render strings once per distinct combination (~14x fewer rows on IEEE-CIS), then broadcast.
    group = sub.groupby(list(columns), dropna=False, sort=False, observed=True).ngroup()
    _, first = np.unique(group.to_numpy(), return_index=True)
    uniq_key = _render_keys(sub.iloc[first], columns, required)
    return pd.Series(uniq_key.to_numpy()[group.to_numpy()], index=df.index, name=name)


def _render_keys(
    sub: pd.DataFrame, columns: tuple[str, ...], required: tuple[str, ...]
) -> pd.Series:
    parts = [_component_to_str(sub[c]) for c in columns]
    key = parts[0].str.cat(parts[1:], sep=SEPARATOR) if len(parts) > 1 else parts[0]
    return key.where(sub[list(required)].notna().all(axis=1).to_numpy())


def build_entity_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``card_id`` and ``uid`` keys aligned to ``df.index``."""
    return pd.DataFrame(
        {
            CARD_ID: build_entity_key(df, CARD_COLUMNS),
            USER_ID: build_entity_key(df, USER_COLUMNS),
        },
        index=df.index,
    )


def account_start_day(df: pd.DataFrame) -> pd.Series:
    """Relative day index ``TransactionDT // 86400 - D1`` (float, NaN where D1 is missing).

    Uses only the row's own fields, so it cannot see labels or other transactions.
    """
    missing = sorted({"TransactionDT", "D1"} - set(df.columns))
    if missing:
        raise SchemaError(f"account start day needs missing columns: {missing}")
    d1 = df["D1"].astype("float64")
    present = d1.dropna()
    if (present < 0).any() or (present != np.floor(present)).any():
        raise SchemaError("D1 must be a non-negative whole number of days")
    day = df["TransactionDT"].astype("float64") // SECONDS_PER_DAY
    return (day - d1).rename(START_DAY)


def build_account_start_keys(df: pd.DataFrame) -> pd.DataFrame:
    """Return ``card_d1`` and ``uid_d1`` keys aligned to ``df.index`` (opt-in proxy)."""
    parts = df[list(USER_COLUMNS)].assign(**{START_DAY: account_start_day(df)})
    return pd.DataFrame(
        {
            CARD_START_ID: build_entity_key(parts, (*CARD_COLUMNS, START_DAY)),
            USER_START_ID: build_entity_key(parts, (*USER_COLUMNS, START_DAY)),
        },
        index=df.index,
    )
