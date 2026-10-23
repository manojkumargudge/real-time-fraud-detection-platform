"""Incremental per-entity state for the history features (the Feast online representation).

Feast stores *state*, not finished features; the features for an event are computed from the
state plus the event itself (``history_features``), with the same definitions as
``src.features.history.compute_history_features``:

* ``UidState`` (entity ``uid``): count and amount sum, kept in two groups -- events strictly
  before the latest timestamp, and events *at* the latest timestamp -- plus that timestamp,
  the previous distinct timestamp, and every timestamp within the longest window.
* ``CardState`` (entity ``card_id``): for each change column, the last known (non-missing)
  value strictly before the latest timestamp, and separately the last known value among the
  events at the latest timestamp (highest ``TransactionID`` wins, as in the batch sort).

Keeping the latest timestamp group separate enforces the batch rule that events sharing a
timestamp never see each other: an event at ``t == last_ts`` uses only the "before" group.

Exactness: for events that arrive in time order (``t >= last_ts``) the features equal the
batch features (amount means up to float summation order). A *late* event (``t < last_ts``)
cannot be answered exactly from this state: its count and amount mean include later events,
its card flags compare with values seen after it, and its own update is approximate. The
history-based serving path (``online_features``) is exact for late events; see
docs/feature_store.md.
"""

from __future__ import annotations

import math
from bisect import bisect_left, insort
from dataclasses import dataclass, field, replace
from typing import Any

from src.features.history import AMOUNT, TIME, HistoryConfig

NONE_TS = -1  # "no timestamp" sentinel (TransactionDT is non-negative); Feast ints stay ints
NAN = float("nan")


def canonical(value: Any) -> str | None:
    """Equality-preserving string for a change-column value (None if missing)."""
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return repr(value + 0.0)  # -0.0 == 0.0 in the batch float comparison
    if isinstance(value, int):
        return repr(float(value))
    return str(value)


@dataclass(frozen=True)
class UidState:
    last_ts: int = NONE_TS
    prev_ts: int = NONE_TS  # latest timestamp strictly before last_ts
    cnt_before: int = 0  # events with ts < last_ts
    amt_before: float = 0.0
    cnt_last: int = 0  # events with ts == last_ts
    amt_last: float = 0.0
    recent_ts: tuple[int, ...] = ()  # sorted, with repeats; ts >= last_ts - longest window

    @property
    def empty(self) -> bool:
        return self.last_ts == NONE_TS


@dataclass(frozen=True)
class CardState:
    last_ts: int = NONE_TS
    before: dict[str, str | None] = field(default_factory=dict)  # strictly before last_ts
    at_last: dict[str, str | None] = field(default_factory=dict)  # at last_ts, highest tid
    at_last_tid: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.last_ts == NONE_TS


def longest_window(config: HistoryConfig) -> int:
    return max(seconds for _, seconds in config.windows)


# --------------------------------------------------------------------------- updates


def apply_uid(state: UidState, t: int, amount: float, config: HistoryConfig) -> UidState:
    """State after adding one event (exact for ``t >= last_ts``; approximate if late)."""
    horizon_span = longest_window(config)
    if state.empty or t > state.last_ts:
        prev = state.prev_ts if state.empty else state.last_ts
        merged = replace(
            state,
            last_ts=t,
            prev_ts=prev,
            cnt_before=state.cnt_before + state.cnt_last,
            amt_before=state.amt_before + state.amt_last,
            cnt_last=1,
            amt_last=amount,
        )
    elif t == state.last_ts:
        merged = replace(state, cnt_last=state.cnt_last + 1, amt_last=state.amt_last + amount)
    else:  # late: counted as "before"; prev_ts may need to move up
        merged = replace(
            state,
            prev_ts=max(state.prev_ts, t),
            cnt_before=state.cnt_before + 1,
            amt_before=state.amt_before + amount,
        )
    recent = list(state.recent_ts)
    insort(recent, t)
    lo = bisect_left(recent, merged.last_ts - horizon_span)
    return replace(merged, recent_ts=tuple(recent[lo:]))


def apply_card(
    state: CardState, t: int, tid: int, values: dict[str, str | None], columns: tuple[str, ...]
) -> CardState:
    """State after adding one event's change-column values (canonical strings)."""
    if state.empty or t > state.last_ts:
        before = {
            c: state.at_last.get(c) if state.at_last.get(c) is not None else state.before.get(c)
            for c in columns
        }
        at_last = {c: values.get(c) for c in columns}
        tids = {c: tid for c in columns if values.get(c) is not None}
        return CardState(last_ts=t, before=before, at_last=at_last, at_last_tid=tids)
    if t == state.last_ts:
        at_last, tids = dict(state.at_last), dict(state.at_last_tid)
        for c in columns:
            if values.get(c) is not None and tid > tids.get(c, NONE_TS):
                at_last[c], tids[c] = values.get(c), tid
        return replace(state, at_last=at_last, at_last_tid=tids)
    return state  # late: the batch would insert it in the past; the state cannot


# --------------------------------------------------------------------------- features


def uid_features(
    state: UidState | None, t: int, amount: float, config: HistoryConfig
) -> dict[str, float]:
    """``uid_*`` features for an event at ``t`` (``state`` None = missing entity key)."""
    names = [n for n in config.feature_names() if n.startswith("uid_")]
    if state is None:
        return dict.fromkeys(names, NAN)
    if state.empty or t > state.last_ts:
        count = state.cnt_before + state.cnt_last
        total = state.amt_before + state.amt_last
        prev = state.last_ts
    elif t == state.last_ts:
        count, total, prev = state.cnt_before, state.amt_before, state.prev_ts
    else:  # late (approximate): totals include later events
        count = state.cnt_before + state.cnt_last
        total = state.amt_before + state.amt_last
        earlier = [x for x in state.recent_ts if x < t]
        prev = earlier[-1] if earlier else NONE_TS
    out: dict[str, float] = {"uid_prior_txn_count": float(count)}
    hi = bisect_left(state.recent_ts, t)  # recent timestamps strictly before t
    for label, seconds in config.windows:
        out[f"uid_txn_count_{label}"] = float(hi - bisect_left(state.recent_ts, t - seconds))
    out["uid_seconds_since_prev_txn"] = float(t - prev) if count and prev != NONE_TS else NAN
    mean = total / count if count else NAN
    out["uid_prior_amt_mean"] = mean
    out["uid_amt_to_prior_mean"] = amount / mean if count and mean > 0 else NAN
    return out


def card_flags(
    state: CardState | None, t: int, values: dict[str, str | None], columns: tuple[str, ...]
) -> dict[str, float]:
    """``card_<col>_changed`` for an event at ``t``: 1 changed, 0 same, NaN if unknown."""
    out: dict[str, float] = {}
    for c in columns:
        name = f"card_{c.lower()}_changed"
        cur = values.get(c)
        if state is None or state.empty or cur is None:
            out[name] = NAN
            continue
        if t > state.last_ts:
            prev = state.at_last.get(c)
            prev = prev if prev is not None else state.before.get(c)
        else:  # same timestamp (exact) or late (approximate)
            prev = state.before.get(c)
        out[name] = NAN if prev is None else float(cur != prev)
    return out


def event_values(event: dict[str, Any], columns: tuple[str, ...]) -> dict[str, str | None]:
    return {c: canonical(event.get(c)) for c in columns}


def history_features(
    uid_state: UidState | None,
    card_state: CardState | None,
    event: dict[str, Any],
    config: HistoryConfig,
) -> dict[str, float]:
    """All history features for ``event``; pass None states when the entity key is missing."""
    t, amount = int(event[TIME]), float(event[AMOUNT])
    feats = uid_features(uid_state, t, amount, config)
    feats.update(
        card_flags(card_state, t, event_values(event, config.change_columns), config.change_columns)
    )
    return {n: feats[n] for n in config.feature_names()}
