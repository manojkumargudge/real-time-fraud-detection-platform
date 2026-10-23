"""Incremental entity state (Feast representation) vs the batch history features."""

from __future__ import annotations

import random
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.features.entity import CARD_ID, USER_ID, build_entity_keys
from src.features.entity_state import (
    CardState,
    UidState,
    apply_card,
    apply_uid,
    canonical,
    event_values,
    history_features,
)
from src.features.history import HistoryConfig, compute_history_features
from src.serving.online_features import entity_keys, events_to_frame

CONFIG = HistoryConfig()
NAMES = CONFIG.feature_names()


def make_events(
    n: int,
    seed: int,
    *,
    cards: tuple[int | None, ...] = (1111, 2222, None),
    first_id: int = 1_000,
    start: int = 0,
) -> list[dict[str, Any]]:
    """Small key space so entities repeat; heavy timestamp ties; missing keys and values."""
    rng = random.Random(seed)
    events, t = [], start
    for i in range(n):
        t += rng.choice([0, 0, 0, 1, 30, 3_600, 86_400, 40 * 86_400])  # many ties
        card1 = rng.choice(cards)  # None -> missing card_id and uid
        has_identity = rng.random() < 0.7
        events.append(
            {
                "TransactionID": first_id + i,
                "TransactionDT": t,
                "TransactionAmt": rng.choice([0.0, 5.0, 12.5, 100.0, 7.25]),
                "card1": card1,
                "card2": 321.0,
                "card3": 150.0,
                "card4": "visa",
                "card5": 226.0,
                "card6": "debit",
                "addr1": rng.choice([315.0, 204.0, None]),  # uid splits; address changes
                "addr2": rng.choice([87.0, 60.0, None]),
                "ProductCD": "W",
                "DeviceType": rng.choice(["mobile", "desktop", None]) if has_identity else None,
                "DeviceInfo": rng.choice(["iOS", "Windows", None]) if has_identity else None,
                "has_identity": has_identity,
            }
        )
    return events


def arrival_order(events: list[dict[str, Any]], seed: int) -> list[dict[str, Any]]:
    """Time order, but random order *within* a timestamp (as a stream may deliver them)."""
    rng = random.Random(seed)
    return sorted(events, key=lambda e: (e["TransactionDT"], rng.random()))


def keys_by_id(events: list[dict[str, Any]]) -> dict[int, tuple[str | None, str | None]]:
    """Same keys as ``entity_keys`` per event, built in one vectorised call."""
    frame = events_to_frame(events)
    keys = build_entity_keys(frame).astype(object).where(lambda k: k.notna(), None)
    return dict(
        zip(frame["TransactionID"], zip(keys[CARD_ID], keys[USER_ID], strict=True), strict=True)
    )


def replay(events: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    uids: dict[str, UidState] = {}
    cards: dict[str, CardState] = {}
    out = {}
    keys = keys_by_id(events)
    for ev in events:
        card, uid = keys[ev["TransactionID"]]
        u = None if uid is None else uids.get(uid, UidState())
        c = None if card is None else cards.get(card, CardState())
        out[ev["TransactionID"]] = history_features(u, c, ev, CONFIG)
        t = int(ev["TransactionDT"])
        if uid is not None and u is not None:
            uids[uid] = apply_uid(u, t, float(ev["TransactionAmt"]), CONFIG)
        if card is not None and c is not None:
            vals = event_values(ev, CONFIG.change_columns)
            cards[card] = apply_card(c, t, ev["TransactionID"], vals, CONFIG.change_columns)
    return out


def assert_matches_batch(events: list[dict[str, Any]], got: dict[int, dict[str, float]]) -> None:
    frame = events_to_frame(events)
    batch = compute_history_features(frame, CONFIG).set_index(frame["TransactionID"])
    mine = pd.DataFrame.from_dict(got, orient="index")[NAMES].astype("float32")
    mine = mine.loc[batch.index]
    np.testing.assert_allclose(
        mine.to_numpy(np.float64), batch[NAMES].to_numpy(np.float64), rtol=1e-6, equal_nan=True
    )


@pytest.mark.parametrize("seed", range(8))
def test_in_order_replay_equals_batch(seed: int) -> None:
    events = make_events(300, seed)
    assert_matches_batch(events, replay(arrival_order(events, seed)))


def test_generated_data_covers_the_hard_cases() -> None:
    events = make_events(300, 0)
    frame = events_to_frame(events)
    keys = compute_history_features(frame, CONFIG)[[CARD_ID, USER_ID]]
    assert keys[USER_ID].isna().any()  # missing entity keys
    tied = frame.assign(uid=keys[USER_ID]).dropna(subset=["uid"])
    assert tied.duplicated(["uid", "TransactionDT"]).any()  # same-timestamp events on one uid
    changes = compute_history_features(frame, CONFIG)["card_devicetype_changed"]
    assert (changes == 1).any() and (changes == 0).any()  # device changes and repeats


def test_same_timestamp_events_do_not_see_each_other() -> None:
    base = make_events(1, 0)[0] | {"card1": 1111, "addr1": 315.0, "TransactionDT": 100}
    first = base | {"TransactionID": 1, "TransactionAmt": 10.0}
    second = base | {"TransactionID": 2, "TransactionAmt": 30.0, "TransactionDT": 200}
    tie = base | {"TransactionID": 3, "TransactionAmt": 50.0, "TransactionDT": 200}
    got = replay([first, second, tie])
    assert got[3]["uid_prior_txn_count"] == 1.0  # sees `first`, not `second`
    assert got[3]["uid_prior_amt_mean"] == 10.0
    assert got[3]["uid_seconds_since_prev_txn"] == 100.0
    assert_matches_batch([first, second, tie], got)


def test_card_change_uses_strictly_earlier_values() -> None:
    base = make_events(1, 0)[0] | {"card1": 1111, "has_identity": True}
    a = base | {"TransactionID": 1, "TransactionDT": 10, "DeviceType": "mobile"}
    b = base | {"TransactionID": 2, "TransactionDT": 20, "DeviceType": "desktop"}
    c = base | {"TransactionID": 3, "TransactionDT": 20, "DeviceType": "mobile"}  # same ts as b
    d = base | {"TransactionID": 4, "TransactionDT": 30, "DeviceType": "mobile"}
    got = replay([a, c, b, d])  # b and c arrive out of TransactionID order
    assert got[2]["card_devicetype_changed"] == 1.0  # vs a (mobile), not vs c
    assert got[3]["card_devicetype_changed"] == 0.0  # vs a, not vs b
    assert got[4]["card_devicetype_changed"] == 0.0  # vs c (highest tid at ts 20) = mobile
    assert_matches_batch([a, b, c, d], got)


def test_vectorised_keys_match_the_online_keys() -> None:
    events = make_events(80, 3)
    assert keys_by_id(events) == {e["TransactionID"]: entity_keys(e) for e in events}


def test_canonical_values() -> None:
    assert canonical(None) is None and canonical(float("nan")) is None
    assert canonical(315) == canonical(315.0) == "315.0"
    assert canonical(-0.0) == canonical(0.0)
    assert canonical("iOS") == "iOS"


def test_late_event_is_approximate() -> None:
    """Documented limitation: an out-of-order event counts history that came after it."""
    base = make_events(1, 0)[0] | {"card1": 1111, "addr1": 315.0}
    early = base | {"TransactionID": 1, "TransactionDT": 100}
    later = base | {"TransactionID": 2, "TransactionDT": 500}
    late = base | {"TransactionID": 3, "TransactionDT": 300}  # arrives after `later`
    got = replay([early, later, late])
    assert got[3]["uid_prior_txn_count"] == 2.0  # batch says 1 (only `early` is earlier)
    batch = compute_history_features(events_to_frame([early, later, late]), CONFIG)
    assert batch.loc[2, "uid_prior_txn_count"] == 1.0
