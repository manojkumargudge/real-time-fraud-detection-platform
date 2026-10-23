"""Feast feature store for the history features: entity state online, features on demand.

Objects (``definitions``):

* entities ``uid`` and ``card_id``;
* feature views ``uid_state`` and ``card_state`` (``src.features.entity_state``), each with a
  file (parquet) batch source for point-in-time retrieval / materialization and a push source
  for online updates;
* request source ``scoring_event`` (time, amount, change-column values, key presence);
* on-demand feature view ``history_features``: the 12 model features from state + request.

Timestamps: Feast needs datetimes; ``TransactionDT`` seconds are mapped to
``EPOCH + TransactionDT``. Offline rows use the entity's latest event time (so point-in-time
joins see exactly the events up to the request time; the state itself keeps the latest
timestamp group separate). Online writes use ``uid_write_ts`` / ``card_write_ts``, strictly
increasing per entity, because Feast's Redis store ignores a write whose timestamp (in whole
seconds) is not
newer than the stored one -- otherwise a second event at the same ``TransactionDT`` would be
dropped.

CLI (cold start): ``python -m src.features.feast_store materialize`` builds the latest state
of every entity from the scored events in PostgreSQL (``DATABASE_URL``) and loads it into the
online store (``FEAST_REDIS``). See docs/feature_store.md.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.features.entity import CARD_ID, USER_ID
from src.features.entity_state import (
    NONE_TS,
    CardState,
    UidState,
    apply_card,
    apply_uid,
    event_values,
    history_features,
)
from src.features.history import AMOUNT, TIME, HistoryConfig

logger = logging.getLogger(__name__)

PROJECT = "fraud"
EPOCH = datetime(2017, 12, 1, tzinfo=UTC)  # TransactionDT 0 (any fixed origin works)
TTL = timedelta(days=365 * 50)
MISSING_KEY = "__missing__"  # entity value sent when the key is missing (never stored)
MISSING_VALUE = "\x00"  # request value for a missing change column (online requests reject None)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "artifacts" / "feast"
EVENT_TIMESTAMP, CREATED = "event_timestamp", "created"
UID_VIEW, CARD_VIEW, ODFV = "uid_state", "card_state", "history_features"
UID_PUSH, CARD_PUSH = "uid_state_push", "card_state_push"
UID_WRITE_TS, CARD_WRITE_TS = "uid_write_ts", "card_write_ts"


def to_datetime(seconds: int) -> datetime:
    return EPOCH + timedelta(seconds=int(seconds))


# --------------------------------------------------------------------------- (de)serialisation


def uid_row(uid: str, s: UidState, write_ts: int) -> dict[str, Any]:
    return {
        USER_ID: uid,
        "uid_last_ts": s.last_ts,
        "uid_prev_ts": s.prev_ts,
        "uid_cnt_before": s.cnt_before,
        "uid_amt_before": s.amt_before,
        "uid_cnt_last": s.cnt_last,
        "uid_amt_last": s.amt_last,
        "uid_recent_ts": list(s.recent_ts),
        UID_WRITE_TS: write_ts,
    }


def uid_from_row(row: dict[str, Any]) -> UidState:
    if row.get("uid_last_ts") is None:
        return UidState()
    return UidState(
        last_ts=int(row["uid_last_ts"]),
        prev_ts=int(row["uid_prev_ts"]),
        cnt_before=int(row["uid_cnt_before"]),
        amt_before=float(row["uid_amt_before"]),
        cnt_last=int(row["uid_cnt_last"]),
        amt_last=float(row["uid_amt_last"]),
        recent_ts=tuple(int(x) for x in row["uid_recent_ts"]),
    )


def _col(c: str) -> str:
    return c.lower()


def card_row(card: str, s: CardState, columns: Sequence[str], write_ts: int) -> dict[str, Any]:
    row: dict[str, Any] = {CARD_ID: card, "card_last_ts": s.last_ts, CARD_WRITE_TS: write_ts}
    for c in columns:
        row[f"card_before_{_col(c)}"] = s.before.get(c)
        row[f"card_last_{_col(c)}"] = s.at_last.get(c)
        row[f"card_last_tid_{_col(c)}"] = s.at_last_tid.get(c, NONE_TS)
    return row


def card_from_row(row: dict[str, Any], columns: Sequence[str]) -> CardState:
    if row.get("card_last_ts") is None:
        return CardState()
    tids = {c: int(row[f"card_last_tid_{_col(c)}"]) for c in columns}
    return CardState(
        last_ts=int(row["card_last_ts"]),
        before={c: row.get(f"card_before_{_col(c)}") for c in columns},
        at_last={c: row.get(f"card_last_{_col(c)}") for c in columns},
        at_last_tid={c: t for c, t in tids.items() if t != NONE_TS},
    )


def request_row(
    event: dict[str, Any], card: str | None, uid: str | None, columns: Sequence[str]
) -> dict[str, Any]:
    vals = event_values(event, tuple(columns))
    return {
        USER_ID: uid if uid is not None else MISSING_KEY,
        CARD_ID: card if card is not None else MISSING_KEY,
        "event_ts": int(event[TIME]),
        "event_amt": float(event[AMOUNT]),
        "has_key": int(uid is not None),
        **{f"event_{_col(c)}": MISSING_VALUE if vals[c] is None else vals[c] for c in columns},
    }


def odfv_features(inputs: dict[str, list[Any]], config: HistoryConfig) -> dict[str, list[float]]:
    """Body of the on-demand view: rebuild state per row and compute the features."""
    cols = config.change_columns
    n = len(inputs["event_ts"])
    out: dict[str, list[float]] = {name: [] for name in config.feature_names()}
    for i in range(n):
        row = {k: v[i] for k, v in inputs.items()}
        event = {TIME: row["event_ts"], AMOUNT: row["event_amt"]}
        for col in cols:
            v = row[f"event_{_col(col)}"]
            event[col] = None if v is None or v == MISSING_VALUE else v
        has_key = bool(row["has_key"])
        uid_state = uid_from_row(row) if has_key else None
        card_state = card_from_row(row, cols) if has_key else None
        for name, value in history_features(uid_state, card_state, event, config).items():
            out[name].append(value)
    return out


# --------------------------------------------------------------------------- definitions


def uid_fields() -> list[tuple[str, str]]:
    return [
        ("uid_last_ts", "int"),
        ("uid_prev_ts", "int"),
        ("uid_cnt_before", "int"),
        ("uid_amt_before", "float"),
        ("uid_cnt_last", "int"),
        ("uid_amt_last", "float"),
        ("uid_recent_ts", "int_list"),
        (UID_WRITE_TS, "int"),
    ]


def card_fields(columns: Sequence[str]) -> list[tuple[str, str]]:
    out = [("card_last_ts", "int"), (CARD_WRITE_TS, "int")]
    for c in columns:
        out += [
            (f"card_before_{_col(c)}", "str"),
            (f"card_last_{_col(c)}", "str"),
            (f"card_last_tid_{_col(c)}", "int"),
        ]
    return out


def definitions(config: HistoryConfig | None = None) -> list[Any]:
    """All Feast objects; batch sources read ``{uid,card}_state.parquet`` in the repo dir."""
    from feast import Entity, FeatureView, Field, FileSource, PushSource, RequestSource
    from feast.on_demand_feature_view import on_demand_feature_view
    from feast.types import Array, Float64, Int64, String

    config = config or HistoryConfig()
    kinds: dict[str, Any] = {
        "int": Int64,
        "float": Float64,
        "str": String,
        "int_list": Array(Int64),
    }

    def schema(fields: list[tuple[str, str]]) -> list[Any]:
        return [Field(name=n, dtype=kinds[k]) for n, k in fields]

    uid = Entity(name=USER_ID, join_keys=[USER_ID])
    card = Entity(name=CARD_ID, join_keys=[CARD_ID])
    views = []
    for name, push_name, entity, fields in (
        (UID_VIEW, UID_PUSH, uid, uid_fields()),
        (CARD_VIEW, CARD_PUSH, card, card_fields(config.change_columns)),
    ):
        batch = FileSource(
            name=f"{name}_file",
            path=f"{name}.parquet",  # relative to repo_path (Feast misreads "C:\..." as a URL)
            timestamp_field=EVENT_TIMESTAMP,
            created_timestamp_column=CREATED,
        )
        push = PushSource(name=push_name, batch_source=batch)
        views.append(
            FeatureView(name=name, entities=[entity], ttl=TTL, source=push, schema=schema(fields))
        )
    request = RequestSource(
        name="scoring_event",
        schema=schema(
            [
                ("event_ts", "int"),
                ("event_amt", "float"),
                ("has_key", "int"),
                *((f"event_{_col(c)}", "str") for c in config.change_columns),
            ]
        ),
    )
    names = config.feature_names()

    @on_demand_feature_view(  # type: ignore[misc]
        name=ODFV,
        sources=[*views, request],
        schema=[Field(name=n, dtype=Float64) for n in names],
        mode="python",
    )
    def history_features_view(inputs: dict[str, list[Any]]) -> dict[str, list[float]]:
        from src.features.feast_store import odfv_features
        from src.features.history import HistoryConfig

        return odfv_features(inputs, HistoryConfig())

    return [uid, card, *views, request, history_features_view]


def feature_refs(config: HistoryConfig | None = None) -> list[str]:
    config = config or HistoryConfig()
    refs = [f"{UID_VIEW}:{n}" for n, _ in uid_fields()]
    refs += [f"{CARD_VIEW}:{n}" for n, _ in card_fields(config.change_columns)]
    refs += [f"{ODFV}:{n}" for n in config.feature_names()]
    return refs


def open_store(
    repo_dir: Path,
    *,
    redis: str | None = None,
    apply: bool = True,
    config: HistoryConfig | None = None,
    project: str = PROJECT,
) -> Any:
    """FeatureStore with a local registry in ``repo_dir``; Redis online store if ``redis``
    (``host:port``) is given, else SQLite in ``repo_dir``. ``apply`` registers the objects."""
    from feast import FeatureStore, RepoConfig

    repo_dir.mkdir(parents=True, exist_ok=True)
    online: dict[str, Any] = (
        {"type": "redis", "connection_string": redis}
        if redis
        else {"type": "sqlite", "path": "online.db"}
    )
    cfg = RepoConfig(
        project=project,  # online keys are namespaced by project (tests use their own)
        registry="registry.db",
        provider="local",
        online_store=online,
        offline_store={"type": "file"},
        entity_key_serialization_version=2,
        repo_path=str(repo_dir),
    )
    # repo_path also anchors the registry; without it Feast writes registry.db to the cwd.
    store = FeatureStore(repo_path=str(repo_dir), config=cfg)
    if apply:
        objs = definitions(config)
        for path in (repo_dir / f"{UID_VIEW}.parquet", repo_dir / f"{CARD_VIEW}.parquet"):
            if not path.exists():  # sources must exist for apply/materialize
                write_offline(repo_dir, [], [], config=config)
                break
        store.apply(objs)
    return store


# --------------------------------------------------------------------------- offline data


@dataclass(frozen=True)
class EntityEvent:
    event: dict[str, Any]
    card: str | None
    uid: str | None


def fold_states(
    events: Iterable[EntityEvent], config: HistoryConfig, *, snapshots: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay events in (TransactionDT, TransactionID) order. ``snapshots``: one row per event
    (state after it, for point-in-time joins); else only each entity's final state."""
    cols = config.change_columns
    uids: dict[str, UidState] = {}
    cards: dict[str, CardState] = {}
    uid_rows: list[dict[str, Any]] = []
    card_rows: list[dict[str, Any]] = []
    ordered = sorted(events, key=lambda e: (int(e.event[TIME]), int(e.event["TransactionID"])))
    for seq, e in enumerate(ordered):
        if e.uid is None or e.card is None:
            continue
        t, tid = int(e.event[TIME]), int(e.event["TransactionID"])
        uids[e.uid] = apply_uid(uids.get(e.uid, UidState()), t, float(e.event[AMOUNT]), config)
        cards[e.card] = apply_card(
            cards.get(e.card, CardState()), t, tid, event_values(e.event, cols), cols
        )
        if snapshots:
            stamp = {EVENT_TIMESTAMP: to_datetime(t), CREATED: to_datetime(seq)}
            uid_rows.append(uid_row(e.uid, uids[e.uid], t) | stamp)
            card_rows.append(card_row(e.card, cards[e.card], cols, t) | stamp)
    if not snapshots:
        created = {CREATED: to_datetime(len(ordered))}
        uid_rows = [
            uid_row(k, s, s.last_ts) | created | {EVENT_TIMESTAMP: to_datetime(s.last_ts)}
            for k, s in uids.items()
        ]
        card_rows = [
            card_row(k, s, cols, s.last_ts) | created | {EVENT_TIMESTAMP: to_datetime(s.last_ts)}
            for k, s in cards.items()
        ]
    return uid_rows, card_rows


def write_offline(
    out_dir: Path,
    uid_rows: list[dict[str, Any]],
    card_rows: list[dict[str, Any]],
    *,
    config: HistoryConfig | None = None,
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    config = config or HistoryConfig()
    types = {
        "int": pa.int64(),
        "float": pa.float64(),
        "str": pa.string(),
        "int_list": pa.list_(pa.int64()),
    }
    stamp = pa.timestamp("us", tz="UTC")
    for name, key, fields, rows in (
        (UID_VIEW, USER_ID, uid_fields(), uid_rows),
        (CARD_VIEW, CARD_ID, card_fields(config.change_columns), card_rows),
    ):
        schema = pa.schema(
            [
                (key, pa.string()),
                *((n, types[k]) for n, k in fields),
                (EVENT_TIMESTAMP, stamp),
                (CREATED, stamp),
            ]
        )
        table = pa.Table.from_pylist(rows, schema=schema)
        out_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, out_dir / f"{name}.parquet")


def historical_features(store: Any, entity_df: pd.DataFrame, config: HistoryConfig) -> pd.DataFrame:
    """Point-in-time features for every row of ``entity_df`` (needs a ``TransactionID`` column).

    Feast returns one row per (join keys, timestamp), so rows that share keys and timestamp --
    same-timestamp events of one uid -- would collapse into one. They are split into separate
    calls by occurrence number and the result is indexed by ``TransactionID``.
    """
    keys = [USER_ID, CARD_ID, EVENT_TIMESTAMP]
    if not entity_df["TransactionID"].is_unique:
        raise ValueError("TransactionID must be unique in entity_df")
    rank = entity_df.groupby(keys, sort=False).cumcount()
    parts = []
    for r in sorted(rank.unique()):
        part = entity_df[rank == r]
        got = store.get_historical_features(entity_df=part, features=feature_refs(config)).to_df()
        if len(got) != len(part):
            raise AssertionError(f"Feast returned {len(got)} rows for {len(part)} entity rows")
        parts.append(got)
    result: pd.DataFrame = pd.concat(parts).set_index("TransactionID")
    return result.loc[entity_df["TransactionID"]]


def materialize_all(store: Any) -> None:
    store.materialize(EPOCH.replace(year=2000), datetime.now(UTC) + timedelta(days=365 * 60))


# --------------------------------------------------------------------------- online reads/writes


@dataclass
class FeastFeatures:
    """Online history features from Feast, and state updates after each saved event."""

    store: Any
    config: HistoryConfig

    def read(
        self, event: dict[str, Any], card: str | None, uid: str | None
    ) -> tuple[dict[str, float], dict[str, Any]]:
        """``(features, raw_online_row)`` for one event (one online round trip)."""
        row = request_row(event, card, uid, self.config.change_columns)
        resp = self.store.get_online_features(
            features=feature_refs(self.config), entity_rows=[row]
        ).to_dict()
        flat = {k: v[0] for k, v in resp.items()}
        feats = {
            n: (float("nan") if flat[n] is None else float(flat[n]))
            for n in self.config.feature_names()
        }
        return feats, flat

    def push(
        self, event: dict[str, Any], card: str | None, uid: str | None, online: dict[str, Any]
    ) -> None:
        """Apply ``event`` to the state read with it and write both entities back."""
        if uid is None or card is None:
            return
        from feast.data_source import PushMode

        cols = self.config.change_columns
        t, tid = int(event[TIME]), int(event["TransactionID"])
        u = apply_uid(uid_from_row(online), t, float(event[AMOUNT]), self.config)
        c = apply_card(card_from_row(online, cols), t, tid, event_values(event, cols), cols)
        for view_push, field, row in (
            (UID_PUSH, UID_WRITE_TS, uid_row(uid, u, _next_write(online.get(UID_WRITE_TS), t))),
            (
                CARD_PUSH,
                CARD_WRITE_TS,
                card_row(card, c, cols, _next_write(online.get(CARD_WRITE_TS), t)),
            ),
        ):
            stamp = {EVENT_TIMESTAMP: to_datetime(row[field]), CREATED: datetime.now(UTC)}
            self.store.push(view_push, pd.DataFrame([row | stamp]), to=PushMode.ONLINE)


def _next_write(previous: Any, t: int) -> int:
    """Online write time in seconds: the event time, but always after the previous write."""
    return t if previous is None else max(t, int(previous) + 1)


# --------------------------------------------------------------------------- CLI


def _events_from_postgres(dsn: str) -> list[EntityEvent]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute("SELECT event, card_id, uid FROM scored_transactions").fetchall()
    return [EntityEvent(r["event"], r["card_id"], r["uid"]) for r in rows]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["materialize"])
    parser.add_argument("--repo-dir", type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dsn, redis = os.environ.get("DATABASE_URL"), os.environ.get("FEAST_REDIS")
    if not dsn or not redis:
        raise SystemExit(
            "set DATABASE_URL (source events) and FEAST_REDIS (online store host:port)"
        )
    config = HistoryConfig()
    events = _events_from_postgres(dsn)
    uid_rows, card_rows = fold_states(events, config, snapshots=False)
    write_offline(args.repo_dir, uid_rows, card_rows, config=config)
    store = open_store(args.repo_dir, redis=redis, config=config)
    materialize_all(store)
    logger.info(
        "materialized events=%d uids=%d cards=%d", len(events), len(uid_rows), len(card_rows)
    )


if __name__ == "__main__":
    main()
