# Feature store (Feast)

Feast holds **per-entity state**, not finished features. The 12 history features are computed
from that state plus the event being scored, with the same definitions as the batch code
(`src/features/history.py`). The original serving path (features recomputed from the card's
stored event history) is kept and is still the default.

## Design

| Piece | Where | What |
|---|---|---|
| `uid_state` (entity `uid`) | `src/features/entity_state.py` | count and amount sum in two groups (strictly before the latest timestamp / at it), latest and previous timestamps, every timestamp within the longest window (30 days) |
| `card_state` (entity `card_id`) | same | per change column (`DeviceInfo`, `DeviceType`, `addr1`, `addr2`): last known value strictly before the latest timestamp, and the last known value at it (highest `TransactionID` wins, as in the batch sort) |
| `history_features` (on-demand view) | `src/features/feast_store.py` | the 12 model features from both states + request fields (`event_ts`, `event_amt`, change-column values, key presence) |
| Online store | Redis (`docker-compose.yml`, service `redis`) | updated after every saved event |
| Offline store | parquet files in `FEAST_REPO_DIR` | state snapshots for point-in-time retrieval and materialization |

Batch rule kept: events with the same `TransactionDT` never see each other. An event at the
state's latest timestamp uses only the "before" group, and "last known" values come from
strictly earlier times.

## Configuration

| Variable | Effect |
|---|---|
| `FEAST_REDIS=host:port` | enables Feast: every newly saved event (API or consumer) updates both entity states |
| `FEATURE_SOURCE=feast` | `/score` and the consumer read the history features from Feast; default `store` (event history in PostgreSQL). Needs `FEAST_REDIS` |
| `FEAST_REPO_DIR` | local registry and offline parquet files (container: `/tmp/feast`; host: `artifacts/feast`) |

Compose sets `FEAST_REDIS=redis:6379` for the API and the consumer and passes
`FEATURE_SOURCE` through (`FEATURE_SOURCE=feast docker compose up -d --no-deps api`).

## Operations

Cold start (or recovery after a failed push) -- with the API and consumer **stopped**, so no
push races the load:

```
docker compose stop api consumer
DATABASE_URL=postgresql://fraud:<pw>@localhost:15432/fraud FEAST_REDIS=localhost:16379 \
  python -m src.features.feast_store materialize
docker compose up -d --no-deps api consumer
```

`materialize` replays every event in `scored_transactions` in `(TransactionDT, TransactionID)`
order, writes each entity's final state to the offline parquet files and loads it into Redis.
Redis uses an append-only file (volume `redisdata`), so the state survives restarts.

A failed push after a successful save is logged as `feast state update failed ...` and does not
fail the request; the entity's state is then stale until the next `materialize`.

## Where the two paths can differ

They agree on every event that arrives in time order: same features and scores in the replay
tests (same-timestamp events on one uid, missing entity keys, changing device/address) and on
live data (300 + 100 real events, 0 mismatches, score differences 0). They can differ when:

1. **Late events** (`TransactionDT` older than the entity's latest timestamp). The history path
   still sees exactly the strictly earlier events. The state cannot: the count and amount mean
   include events that came after it, `seconds_since_prev` can be missing, card flags compare
   with values seen after it, and its own update is approximate (counted in the "before"
   group; card values ignored). `test_late_event_is_approximate` pins this behaviour.
2. **A failed or skipped push** (Redis down, crash between save and push): the state misses
   that event until `materialize`.
3. **Concurrent scoring of one entity** (API and consumer at the same moment): each push is a
   read-modify-write of the whole state, so one update can overwrite the other.
4. **Float rounding**: amount sums are accumulated incrementally instead of with the batch
   prefix sums; differences are at float64 rounding level and disappear in the float32 model
   input (tests use `rtol=1e-6`).

The history path has none of these problems but its cost grows with the card's history.

## Feast details that matter here

- **Redis ignores a write whose timestamp (whole seconds) is not newer than the stored one.**
  A second event at the same `TransactionDT` would be silently dropped. Online writes therefore
  carry `uid_write_ts` / `card_write_ts` = `max(event time, previous write + 1 s)`. Without
  this, two same-timestamp events left a count of 1 instead of 2 (checked against Redis);
  `test_feast_redis_replay_matches_the_store_path` (integration) covers it. SQLite, used by the
  unit tests, overwrites unconditionally and would not catch it.
- **Historical retrieval returns one row per (entity keys, timestamp)**, so same-timestamp
  events of one uid collapse into one row. `feast_store.historical_features` splits such rows
  into separate calls and indexes the result by `TransactionID`.
- Online requests reject `None` request values: a missing change-column value is sent as
  `"\x00"` and decoded back to missing inside the on-demand view.
- Without an explicit `repo_path`, Feast writes `registry.db` to the current directory.
- Pins: `feast[redis]==0.47.0` (newest release compatible with numpy 1.26.4 and pydantic
  2.14.1); it installs pyarrow 18.0.0 and SQLAlchemy 2.0.55. protobuf 7.36.2 works; no pin was
  needed.

## Latency (measured 2026-10-23, local stack)

Features + model score per event, read-only, next real IEEE-CIS events after the stored ones
(Windows host process -> PostgreSQL and Redis in Docker; first 10 events excluded as warm-up):

| Path | p50 | p95 | p99 |
|---|---|---|---|
| store (event history in PostgreSQL) | 61.0 ms | 102.5 ms | 131.9 ms |
| Feast (Redis state + on-demand view) | 238.6 ms | 365.5 ms | 461.6 ms |

Breakdown of the Feast path (p50): Feast online call with the on-demand view 164 ms, the same
call for the state only 62 ms, Redis round trip 2 ms, feature arithmetic 0.1 ms, model row +
predict 67 ms (shared with the store path). The cost is the Feast Python SDK and its
on-demand-view machinery, not Redis.

End to end through `/score` in the API container (Linux), 5 new events per mode, including
the PostgreSQL write and the Feast state push: median 157.9 ms (store) vs 147.2 ms (Feast).
These samples are small. With pushes enabled the consumer took 264-405 ms per event (it was
about 100-130 ms before).

At this data size (median 2-4 history rows per card) the history path is cheaper. Feast pays
off only for long histories (the longest card here has 243 rows) or to share state across
services. Next steps: compute the features from the state locally instead of through the
on-demand view (the state read is 62 ms of the 164 ms), batch the two pushes, and measure
under load.
