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
| `history_features` (on-demand view) | `src/features/feast_store.py` | the 12 model features from both states + request fields; used for point-in-time (offline) retrieval |
| Online features | `FeastFeatures.read` | reads the two states and computes the features in-process with the **same function** the on-demand view runs (`use_odfv=True` goes through the view instead; same values, slower) |
| Online store | Redis (`docker-compose.yml`, service `redis`) | updated after every saved event |
| Offline store | parquet files in `FEAST_REPO_DIR` | state snapshots for point-in-time retrieval and the cold-start state |

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

## Cold start

The starting state comes from the **offline batch data**: every IEEE-CIS transaction with
`TransactionDT` earlier than the first streamed event, i.e. the history the model was trained
on. Run it once, before the stream starts, with the API and consumer stopped:

```
docker compose stop api consumer
FEAST_REDIS=localhost:16379 python -m src.features.feast_store materialize --before <first streamed TransactionDT>
docker compose up -d --no-deps api consumer
```

Without `--before`, the time of the earliest event in `scored_transactions`
(`DATABASE_URL`) is used; only that timestamp is read from PostgreSQL. The batch events are
replayed in `(TransactionDT, TransactionID)` order, each entity's final state is written to the
offline parquet files and loaded into Redis. Real-data check: the 472,432 transactions before
the validation cut-off become 39,087 uid and 13,792 card states in 25 s, and the first 5,000
streamed events then match the batch features exactly (0 rows differ).

On the live local stack the stream replays IEEE-CIS from its first transaction
(`TransactionDT` 86,400, the dataset minimum), so there is no pre-stream history: 0 of the 710
scored cards have any, `materialize` loads nothing, and the state is exactly what the stream
pushed.

Redis uses an append-only file (volume `redisdata`), so the state survives restarts.

## Train/serve skew of the history (store) path

The store path only knows events this service has ingested. When the stream starts after the
training data, as it would in production, cards that already have history get different
features than in training. Measured with the stream starting at the validation cut-off
(`TransactionDT` >= 12,192,900; 118,108 events, 7,474 cards, 6,373 of them with pre-stream
history), store-path features vs batch (training) features, production model v2:

| Measure | Value |
|---|---|
| rows on cards with pre-stream history | 116,430 (98.6%) |
| rows with any history feature different | 113,401 (96.0%); 97.4% of rows with pre-stream history, 0.0% without |
| most affected features | `uid_prior_txn_count` 95.1%, `uid_prior_amt_mean` / `uid_amt_to_prior_mean` 95.0%, `uid_txn_count_30d` 58.2%, `uid_seconds_since_prev_txn` 11.0% of rows |
| score change \|batch - served\| | mean 0.0104, median 0.0024, p95 0.0431, max 0.8420; 20.2% of rows change by more than 0.01 |
| alerts at threshold 0.1281 | 5,093 with batch features vs 3,856 served; 2,345 decisions (1.99%) flip |
| PR-AUC on the stream | 0.2992 with batch features vs 0.2533 with store-path features |

The Feast path with the batch cold start equals the batch features for these cards (unit test
`test_stream_replay_with_batch_cold_start`, and the real-data check above). On the live stack
there is no such skew today only because the stream started at the first transaction.

## Where the two paths can differ

For events that arrive in time order and cards without pre-stream history, they give the same
features and scores: replay tests (same-timestamp events on one uid, missing entity keys,
changing device/address; SQLite and Redis) and live data (700 real events read both ways,
10 through `/score`, 0 mismatches). They can differ when:

1. **Cards with pre-stream history**: the store path is skewed (section above); Feast is not.
2. **Late events** (`TransactionDT` older than the entity's latest timestamp). The history path
   still sees exactly the strictly earlier events. The state cannot: the count and amount mean
   include events that came after it, `seconds_since_prev` can be missing, card flags compare
   with values seen after it, and its own update is approximate. `test_late_event_is_approximate`
   pins this behaviour.
3. **Concurrent writes.** Known limitation: the API and the consumer each read-modify-write the
   whole state, so two events for one entity at the same moment can lose one update; fix would
   be an atomic per-entity update (a Redis Lua script or `WATCH`/`MULTI` transaction keyed by
   entity, or routing every write through the consumer, which handles a partition serially).
4. **Failed pushes.** Known limitation: a push that fails after the event is saved is only
   logged (`feast state update failed ...`) and the state stays stale; fix would be an
   automatic catch-up (record the last applied `TransactionID` per entity and replay the
   missed events from `scored_transactions` or the Kafka topic).
5. **Float rounding**: amount sums are accumulated incrementally instead of with the batch
   prefix sums; differences are at float64 rounding level and disappear in the float32 model
   input (tests use `rtol=1e-6`).

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

Features + model score per event, read-only, 300 real IEEE-CIS events after the stored ones
(Windows host process -> PostgreSQL and Redis in Docker; first 10 excluded as warm-up). All
three variants gave identical features and scores on every event.

| Path | p50 | p95 | p99 |
|---|---|---|---|
| store (event history in PostgreSQL) | 61.1 ms | 88.8 ms | 138.8 ms |
| Feast, features computed locally from the state (default) | 107.5 ms | 179.0 ms | 283.2 ms |
| Feast, features through the on-demand view | 227.1 ms | 346.7 ms | 566.4 ms |

In the local variant the Feast state read is 50.8 ms (p50) and the model row + predict (shared
with the store path) about 57 ms; a raw Redis round trip is 2 ms, so most of the read is Feast
Python SDK overhead. Earlier end-to-end `/score` samples in the Linux API container (5 events
per mode, on-demand view, including the PostgreSQL write and the state push): median 157.9 ms
(store) vs 147.2 ms (Feast). With pushes enabled the consumer took 264-405 ms per event (about
100-130 ms before); the push itself is not yet optimized.

At this data size (median 2-4 history rows per card) the store path is faster; its real cost
is the skew above, not latency. Next steps: read Redis directly (or batch reads) to cut the SDK
overhead, write both states in one push, and measure under load.
