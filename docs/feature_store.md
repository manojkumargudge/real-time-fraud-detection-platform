# Feature store (Feast)

Feast stores **per-entity history state** (`uid_state`, `card_state`), not finished features.
Training and serving both read that state from Feast and compute the 12 history features with
the same function, `src.features.entity_state.history_features`:

- serving: `Scorer.score_event` -> `FeastFeatures.read` reads the state from the online store
  (Redis) and calls `history_features` on it with the event being scored;
- training (`--feature-source feast`): `get_historical_features` reads the state from the
  offline store, point-in-time per transaction, and `odfv_features` (the body of the
  `history_features` on-demand view) calls `history_features` on each row.

The batch definitions (`src/features/history.py`) give identical values (section "Training from
the Feast offline store"). Feast is the default serving feature source; the original serving
path (features recomputed from the card's stored event history) is kept behind
`FEATURE_SOURCE=store`.

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
| `FEATURE_SOURCE` | `feast` (default): history features from the Feast state; `store`: from the card's event history in PostgreSQL (skewed for cards seen before the stream) |
| `FEAST_REDIS=host:port` | required for `feast` (checked when the scorer is built); when set, every newly saved event (API or consumer) updates both entity states |
| `FEAST_REPO_DIR` | local registry and offline parquet files (container: `/tmp/feast`; host: `artifacts/feast`) |

Compose sets `FEAST_REDIS=redis:6379` for the API and the consumer, with
`FEATURE_SOURCE=${FEATURE_SOURCE:-feast}` (`FEATURE_SOURCE=store docker compose up -d --no-deps api`
switches back).

## Cold start

The starting state comes from the **offline batch data**: every IEEE-CIS transaction with
`TransactionDT` earlier than the first streamed event, i.e. the history the model was trained
on. Run it once, before the stream starts, with the API and consumer stopped:

```
docker compose stop api consumer
FEAST_REDIS=localhost:16379 python -m src.features.feast_store materialize --before validation
docker compose up -d --no-deps api consumer
```

`--before validation` is the validation cut-off (`TransactionDT` 12,192,900), where the demo
producer starts; `--before <TransactionDT>` sets it explicitly, and without `--before` the time
of the earliest event in `scored_transactions` (`DATABASE_URL`) is used (only that timestamp is
read from PostgreSQL). The batch events are
replayed in `(TransactionDT, TransactionID)` order, each entity's final state is written to the
offline parquet files and loaded into Redis. Real-data check: the 472,432 transactions before
the validation cut-off become 39,087 uid and 13,792 card states in 25 s, and the first 5,000
streamed events then match the batch features exactly (0 rows differ).

The demo stack (reset 2026-10-23) uses exactly this: Feast materialized from the 472,432
transactions before the cut-off in 80 s (52,879 Redis keys), and the producer streams only
validation-period transactions. Before the reset the demo replayed IEEE-CIS from its first
transaction, so it had no pre-stream history at all (0 of 710 cards).

Redis uses an append-only file (volume `redisdata`), so the state survives restarts.

## Training from the Feast offline store

Champion v2 was trained on the pandas matrix (`build_model_frame(df, "baseline")`, split at
`TransactionDT` 12,192,900). The Feast offline matrix (`src/features/feast_training.py`) is
**proven identical** to it on the full file (2026-10-28, PR #12,
`python -m scripts.compare_feature_sources`): same columns, order, dtypes and values, no
difference in any column.

| Matrix | Rows (pandas / Feast) | sha256 (both sides) |
|---|---|---|
| training frame | 472,432 / 472,432 | `66db4b971d3ec84093ab80c1508f8b9027ada08d42499f7f7a453fbc908e628c` |
| validation frame | 118,108 / 118,108 | `6ace174d268c0d12f9263bb05941ecdd4fd985884430f68eb85daadc39f85274` |
| encoded float32 training matrix | 472,432 / 472,432 | `ecd95303481717c66595861e7d9e5a76a8723e806ebfc762bbd8d9b038afe345` |

The rows, split and encoder vocabulary match v2's metadata, and v2's booster scores both
validation matrices bit-identically (PR-AUC 0.299238, as in its metadata).

**Defaults.** Both sources give the same matrix, so the choice is only about cost: the Feast
matrix takes about 38 min, pandas about 2 s.

| Entry point | Feature source |
|---|---|
| `python -m src.models.train` (CLI) | **Feast** by default; `--feature-source pandas` switches |
| direct calls (`fit_baseline`, `model_frame`, studies, tests) | pandas by default; `feature_source="feast"` switches |
| automatic retraining by the drift job (`run_job` in `src/monitoring/run_drift.py` -> `build_model_frame` -> `retrain_and_compare` -> `fit_on_split`) | **pandas** (no option) |

Retrieval runs in time-ordered chunks of 4,000 rows. Each chunk's offline source holds only the
snapshots a point-in-time join can pick for that chunk, because Feast's dask file store
re-pickles the frames and slows down superlinearly on the full source.
`tests/test_feast_training.py` checks small slices across chunk boundaries.

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
`test_stream_replay_with_batch_cold_start`, and the real-data check above). The demo stack
(reset 2026-10-23) streams only validation-period events after this cold start, so it serves
the batch features; the store path would show the skew above.

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
100-130 ms before).

These are the 2026-10-23 numbers. Since 2026-10-25 (`consumer-throughput`) Feast reads and
writes go through Feast's provider (`online_read` / `ingest_df`) instead of the SDK calls,
with the same values: scorer p50 48 ms, 46.8 events/s on 3 consumers (docs/serving.md
"Throughput benchmark"). At this data size the store path's real cost is the skew above, not
latency.
