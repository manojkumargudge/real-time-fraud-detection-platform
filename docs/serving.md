# Real-Time Scoring Slice (Milestone 3)

This is a local, end-to-end path:

**Kafka event → consumer → online features → XGBoost → PostgreSQL (scores + alerts)**

There is also a synchronous `POST /score` API. This is a working local demo, **not** a
production deployment. See §6 for its limits.

```
producer (replays IEEE-CIS) --> Kafka "transactions" --> consumer --+
                                                                    +--> Scorer.score_event
client --> FastAPI POST /score -------------------------------------+         |
                                                                              |
  1. dedupe:   store.get(TransactionID)            PostgreSQL (duplicate -> stored result)
  2. features: FeastFeatures.read(event)           Redis via Feast: uid_state + card_state
               -> entity_state.history_features    (same function as training)
  3. score:    XGBoost bundle (artifacts/baseline/, = MLflow v2)
  4. save:     store.save(score + alert)           PostgreSQL
  5. update:   FeastFeatures.push(event)           Redis via Feast (new entity state)
```

`FEATURE_SOURCE=feast` is the default (`src/serving/config.py`). With `FEATURE_SOURCE=store`,
step 2 instead reads the card's earlier events from PostgreSQL (`store.card_history`); that path
is kept for comparison only (docs/feature_store.md, "Train/serve skew of the history (store)
path").

## 1. Model (`src/models/train.py`)

`python -m src.models.train` reuses the Milestone 2 protocol: one chronological split,
`train_variant` and the max-F1 threshold from a training-only holdout. It trains on **baseline
keys only** (`card_id`/`uid`); D1 keys stay experimental. It writes these files to
`artifacts/baseline/`, which is gitignored and never committed:

- `model.json`: the XGBoost booster.
- `metadata.json`: feature list, encoder vocabulary, threshold, split boundaries, validation
  metrics, package versions and the model SHA-256.

`ModelBundle.load` refuses to load a bundle if:

- the SHA-256 doesn't match;
- the schema version is unknown;
- the variant isn't `baseline`;
- the feature list differs from what the current code produces.

The served bundle is MLflow v2 (`baseline-9fdad27ea3b0`, `--imbalance none`, seed 42):
validation PR-AUC 0.2992, threshold 0.1281, precision 0.3136, recall 0.3930, 4.31% of
validation transactions flagged. (The first bundle, v1 with `scale_pos_weight`, had PR-AUC
0.2504 and threshold 0.672; it is no longer served.) The CLI builds the history features from
the Feast offline store by default (`--feature-source feast|pandas`, proven identical:
docs/feature_store.md).

## 2. Event schema v1 (`src/serving/schema.py`)

Field names follow IEEE-CIS, so the batch code and online code read the same columns.

| Field | Type | Rule |
|---|---|---|
| `TransactionID` | int ≥ 0 | required, idempotency key |
| `TransactionDT` | int ≥ 0 | required, seconds (relative IEEE-CIS offset) |
| `TransactionAmt` | float ≥ 0 | required, finite |
| `ProductCD`, `card4`, `card6`, `P_emaildomain`, `R_emaildomain`, `DeviceType`, `DeviceInfo` | str or null | ≤ 128 chars |
| `card1` | int or null | null → no entity, no history features |
| `card2`, `card3`, `card5`, `addr1`, `addr2`, `dist1` | float or null | finite |
| `has_identity` | bool | must be true if `DeviceType`/`DeviceInfo` is set |

Unknown fields are **rejected**, including `isFraud` and `D1`. A client therefore cannot pass
the label or experimental inputs into scoring.

**Range limits** (added in the hardening audit; out-of-range events get HTTP 422 or are skipped
as invalid by the consumer):

- `TransactionID` and `card1` must be within ±(2^53 − 1). IDs pass through float64 in the
  feature path, so larger IDs failed with an error. Larger `card1` values collided on one card
  key, sharing history between unrelated cards.
- `TransactionDT` must be at most 2^40 seconds. This keeps the history index's int64 keys
  in range, and it also rejects millisecond timestamps.
- Text fields must not contain NUL (`\u0000`), which PostgreSQL `JSONB` cannot store.

Before these limits, such events passed validation and then failed on every attempt. For the
consumer that meant a crash loop on one uncommitted message.

**Kafka message format:**

- value: JSON of the event;
- key: the `card_id` proxy, or `id:<TransactionID>` when `card1` is missing;
- header: `schema_version=1`.

Keying by card keeps each card's events in order within one partition. The key is derived only
from card fields; no customer ID is invented.

## 3. Online feature path (`src/serving/scorer.py`, `src/features/feast_store.py`)

**Default (`FEATURE_SOURCE=feast`):** `Scorer.score_event` calls `FeastFeatures.read`, which
reads the event's `uid_state` and `card_state` from Redis through Feast and computes the 12
history features with `entity_state.history_features`, the same function training uses on the
Feast offline state (docs/feature_store.md). The model row is the event's raw columns
(`raw_model_columns`) plus those 12 values. After the save, `FeastFeatures.push` writes the
updated states back.

**Comparison path (`FEATURE_SOURCE=store`, `src/serving/online_features.py`):** for an event
at time `t` on card `c`, the store returns `c`'s stored events in PostgreSQL with
`TransactionDT < t`, and the event plus that history go through `compare.build_model_frame`.
One query is enough because every `uid` is `card_id` plus `addr1`. This path only knows
ingested events, so it is skewed for cards with earlier history.

What can be computed from what:

| Computed from the event alone | Needs stored history |
|---|---|
| amount, card1–3, card5, addr1/2, dist1, `has_identity`, the 7 categorical codes (vocabulary fitted on training data) | all 12 history features: counts, velocity windows, seconds since previous, prior mean amount and ratio, 4 change flags |

**Parity is tested:** 800 synthetic events replayed in time order through the scorer give the
same history features and the same scores (rtol 1e-6) as the batch training path. Another test
checks that events with a later `TransactionDT` already in the store are never used.

## 4. Persistence and delivery (`store.py`, `streaming/`)

PostgreSQL tables:

- `scored_transactions`: primary key `transaction_id`. It holds the event JSON, score,
  threshold, alert flag and model version, with an index on `(card_id, transaction_dt)`.
- `alerts`: unique on `transaction_id`, with status `open` / `confirmed_fraud` /
  `false_positive` / `closed`.

The score row and its alert row are written in one database transaction using
`ON CONFLICT DO NOTHING`.

**Delivery semantics:**

- **At-least-once:** the consumer commits a Kafka offset only after the database write. If it
  crashes in between, the message is delivered again, and the `TransactionID` primary key turns
  the redelivery into a no-op.
- **Not exactly-once.** This is idempotent at-least-once processing, not Kafka exactly-once.
- **Conflicting payloads:** a redelivered ID returns the stored score. If the payload differs,
  a warning is logged.
- **Invalid messages and failed events** go to the dead-letter topic, and database outages are
  retried. See *Reliability* below.
- **Shutdown:** on SIGTERM/SIGINT (`docker stop`, Ctrl+C), the consumer finishes and commits
  the current message, leaves the consumer group and closes the database connection. During
  a retry wait the message is left uncommitted instead and is redelivered after restart. The API
  closes the store it opened when it shuts down.
  - Python runs as PID 1 in the container, and the kernel ignores SIGTERM for PID 1 unless a
    handler is installed. Before the fix, `docker stop` always waited the full grace period
    and then SIGKILLed the consumer.
  - Measured on 2026-10-22 against a throwaway topic:
    - old code: 21.2 s, exit code 137, still listed as a group member;
    - new code: 1.0 s, exit code 0, group left with offsets committed (lag 0).
  - The images were rebuilt on 2026-10-22 and include this fix.
- **Deterministic scoring errors** are retried 3 times and then dead-lettered, so they can
  no longer block a partition.

### Reliability: retries, dead-letter topic, offsets (`streaming/reliability.py`)

**Error classes:**

| Class | Examples | Handling |
|---|---|---|
| invalid | bad JSON, schema violation | dead-lettered on the first attempt |
| transient | `psycopg.OperationalError` / `InterfaceError`, `ConnectionError`, `TimeoutError` | retried **indefinitely**, never dead-lettered |
| permanent | anything else (model error, `DataError`, ...) | first attempt + **3 retries**, then dead-lettered |

**Backoff:** 0.5 s, doubling, capped at 30 s, with "equal jitter" (each wait is between half
and all of the capped delay).

**While waiting:**
- The consumer pauses its partitions but keeps calling `poll()`, so it stays in its group
  (`max.poll.interval.ms`) and keeps serving rebalance callbacks.
- A message that arrives while paused is rewound with `seek`.
- A rebalance that revokes the in-flight partition abandons that message uncommitted; its new
  owner processes it, and the primary key deduplicates.
- SIGTERM interrupts the wait without committing.
- A database outage therefore stalls consumption. It never skips events or floods the
  dead-letter topic.

**Dead-letter topic:** `transactions.dlq` (override with `KAFKA_DLQ_TOPIC`):
- 1 partition, `retention.ms=2592000000` (30 days), `cleanup.policy=delete`.
- The consumer creates it at startup, before the first write. Broker auto-creation would
  otherwise apply the 7-day default.
- An existing topic is never altered. A different retention is logged as a warning.

**Each dead-letter record:**
- the original key, value bytes and headers, unchanged;
- `dlq.reason` (`invalid` or `processing_error`);
- `dlq.source.topic`, `dlq.source.partition`, `dlq.source.offset`, `dlq.source.timestamp_ms`;
- `dlq.attempts` and `dlq.failed_at` (UTC);
- `dlq.error.type` and `dlq.error.detail`.

Error details are sanitised: validation field locations and types, SQLSTATE, or the JSON
parser position only, never exception text, which can echo card fields. The record still
contains the **full original event**, including card and address fields, so access to the
topic must be restricted in any real deployment. Its 30-day retention bounds how long they
are kept.

**Offsets:**
- An offset is committed only after the database write or a **confirmed** dead-letter write.
  The producer is idempotent with `acks=all`, and delivery must be confirmed.
- If the dead-letter write fails, it is retried with backoff and nothing is committed.
- A commit rejected by a rebalance (`ILLEGAL_GENERATION`, `UNKNOWN_MEMBER_ID`,
  `REBALANCE_IN_PROGRESS`) is logged; the message is redelivered and deduplicated.
- A commit failing with a **retriable** error (`NOT_COORDINATOR`,
  `COORDINATOR_LOAD_IN_PROGRESS`, `COORDINATOR_NOT_AVAILABLE`, `REQUEST_TIMED_OUT`,
  `_TIMED_OUT`, or any error with `KafkaError.retriable()` true) is retried 3 times after
  0.2 s, 0.5 s and 1 s. If it still fails, a warning is logged, it counts as `commit_rejected`
  and the consumer carries on: the offset stays uncommitted, a later commit on the partition
  or a redelivery covers it, and the primary key deduplicates (at-least-once).
- If commits keep failing for more than 30 s in a row, an ERROR is logged (at most once per
  30 s) with `consecutive_commit_failures`; the consumer does not exit. The first successful
  commit logs "offset commits recovered" and resets the streak.
- Any other commit error (for example `_TRANSPORT` without the retriable flag, authorization
  errors) still raises and stops the consumer.
- The dead-letter topic is at-least-once: a crash between its acknowledgement and the commit
  writes a second copy, identifiable by the same `dlq.source.*` headers.

**Consumer counts:** the stop log line, `consumer stopped counts=...`, reports:
- `scored`, `duplicate`, `invalid` and `failed`: one per message;
- `dead_lettered`: confirmed dead-letter writes;
- `retries`;
- `interrupted`: left uncommitted;
- `commit_rejected`: commits not accepted (rebalance, or retriable errors after all retries);
- `commit_retries`: commit attempts after the first;
- `consecutive_commit_failures`: a gauge, not a total. It is present only while commits are
  failing in a row.

**Operations and recovery.** In Git Bash, prefix `docker exec` with `MSYS_NO_PATHCONV=1`.

- **Inspect dead letters:**
  `docker exec fraud-platform-kafka-1 /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 --topic transactions.dlq --from-beginning --property print.key=true --property print.headers=true --timeout-ms 10000`
- **Verify the retention:**
  `docker exec fraud-platform-kafka-1 /opt/kafka/bin/kafka-configs.sh --bootstrap-server localhost:9092 --entity-type topics --entity-name transactions.dlq --describe --all | grep retention.ms`
  It should show `2592000000`.
- **Detect a stall:** look for rising lag in
  `kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --group fraud-scorer`
  together with `processing failed kind=transient attempt=N` in the consumer log.
  - This means a database or network outage. Restore the dependency and consumption resumes
    on its own.
  - Do not reset offsets or restart the consumer in a loop.
- **Dead-letter topic unavailable:** the log shows `DLQ write failed attempt=N`. The consumer
  holds that one message uncommitted until Kafka accepts the write, then continues.
- **Replay after a fix:** review the record, then produce its value back to `transactions` with
  the same key. IDs that are already stored become no-ops, and dead-letter records stay for
  audit until retention removes them.
- **Never do this to "unstick" the consumer:**
  - delete the dead-letter topic;
  - reset `fraud-scorer` offsets;
  - truncate tables.

  First find the cause in the logs.

**Deploying:** the running consumer image predates this milestone. Apply it with
`docker compose build consumer && docker compose up -d --no-deps consumer`. The effects:
- a graceful restart of a few seconds;
- `transactions.dlq` is created on first start;
- invalid messages are dead-lettered instead of only logged.

## 5. Running it locally

```
python -m src.models.train                      # once; creates artifacts/baseline
copy .env.example .env                          # then set POSTGRES_PASSWORD
docker compose up -d --build --wait             # kafka, postgres, api, consumer
curl http://127.0.0.1:18000/health
set KAFKA_BOOTSTRAP_SERVERS=localhost:9092
python -m src.streaming.producer --skip 0 --limit 2000
docker compose down                             # add -v to also delete the database volume
```

Host ports: API 18000, PostgreSQL 15432, Kafka 9092. All are bound to 127.0.0.1 and can be
changed in `.env`.

Integration tests use a throwaway schema and topic per test:

```
set TEST_DATABASE_URL=postgresql://fraud:<password>@localhost:15432/fraud
set TEST_KAFKA_BOOTSTRAP=localhost:9092
python -m pytest -m integration
```

## 6. Measured demo results (2026-10-21)

Environment: Windows 11 laptop, 12 cores, Docker Desktop. One consumer, three partitions,
single-node Kafka.

| Measure | Result |
|---|---|
| Replay | First 2,000 IEEE-CIS train transactions; 2,000 rows stored, 79 alerts (3.95%) |
| Scorer latency inside the consumer (feature build + predict + DB write) | p50 80.5 ms, p95 138 ms, p99 199 ms, max 301 ms |
| End-to-end throughput, produce start → last DB row | about 9 events/s (2,000 in 216 s, including about 20 s dataset load in the producer) |
| Redelivery of 300 already-processed events | DB unchanged (2,000 rows, 79 alerts); consumer lag back to 0 |
| API, single new event (curl) | about 107 ms scorer latency; repeat call returned `duplicate=true` with the same score |

Latency hardly depends on history length (0 to 241 rows here). It is dominated by fixed pandas
overhead in the batch feature code, applied to tiny frames. These numbers come from one local
run and are **not** a production latency claim.

### Full validation stream (2026-10-24)

Rest of the validation period (skip 10,300, 107,808 events, days 145.2 to 183.0). Setup: Feast
features (`FEATURE_SOURCE=feast`), 3 consumers (one per partition), unthrottled producer, same
laptop.

| Measure | Result |
|---|---|
| Stored | 118,108 rows in total (the whole validation window), all `transaction_id`s unique, one model version; 5,093 alerts; DLQ empty |
| Throughput (first to last `scored_at`, 13,399 s) | **8.05 events/s** overall; **8.29 events/s** over 110 quiet minutes (minutes without other heavy jobs on the laptop) |
| Scorer latency per event (Feast features + XGBoost score; excludes Kafka queueing and the DB write) | **p50 207 ms, p95 549 ms, p99 843 ms** (107,808 samples) |
| Duplicates | 107,808 "scored" lines for 107,808 events; 7 redelivered events skipped as duplicates |

Contention: tests, image builds and scans also ran on the laptop between 05:55 and 07:46 UTC.
Throughput was about the same with and without them (8.05 vs 8.29 events/s). Latency is well
above the Feast figure from the 10,000-event run (p50 107.5 ms), with the same 3 consumers
competing for one CPU; this was not profiled further.

**Consumer crashes (fixed 2026-10-25, branch `consumer-crash-fix`).** All 3 consumers exited
twice, at 06:38 and 07:41 UTC. A synchronous offset commit failed with `NOT_COORDINATOR` /
`COORDINATOR_LOAD_IN_PROGRESS` during heavy load on the laptop. `consumer.py` then tolerated
only the rebalance errors (`ILLEGAL_GENERATION`, `UNKNOWN_MEMBER_ID`, `REBALANCE_IN_PROGRESS`)
and re-raised anything else. Docker restarted the consumers. No data was lost or duplicated:
offsets are committed after the DB write, and redelivered events are deduplicated. The fix
retries retriable commit errors and then leaves the message for redelivery instead of exiting
(see "Offsets" above and the chaos test below).

### Chaos test: Kafka restart mid-stream (2026-10-25)

`python -m scripts.chaos_kafka_restart [--mode restart|kill|pause]` runs only against the test
stack (`docker-compose.test.yml`). It uses a fresh `chaos` database, a new topic and a new
group per run, and 3 host consumers (`FEATURE_SOURCE=store`). It replays 500
validation-period events and disrupts test Kafka once a third of them are stored. It exits 0
only if every check passes. Logs and `summary.json` go to `reports/chaos/<run id>/`
(gitignored).

| Mode (run id) | Kafka back after | Consumers alive | Rows written / events sent | Duplicate IDs | Lag / DLQ | `commit_rejected` | Redelivered duplicates skipped |
|---|---|---|---|---|---|---|---|
| restart, `docker compose restart` (20261025T035507Z) | 12.4 s | 3/3 | 500 / 500 | 0 | 0 / 0 | 0 | 0 |
| kill + start, forced log recovery (20261025T035926Z) | 24.8 s | 3/3 | 500 / 500 | 0 | 0 / 0 | 0 | 0 |
| pause 70 s, frozen broker (20261025T040653Z) | 104.5 s | 3/3 | 500 / 500 | 0 | 0 / 0 | 3 (rebalance) | 3 |

What it shows and what it does not:
- In every mode the consumers stayed up, no event was lost and each was stored exactly once.
- With the broker gone (restart/kill), librdkafka holds the synchronous commit until a
  coordinator is back, so no commit error reached `consumer.py`. The group session timed out
  (45 s), and partitions were revoked and reassigned.
- In the pause run, 3 commits were rejected by that rebalance (`commit_rejected` = 3). Their
  messages were redelivered and skipped as duplicates.
- None of the runs produced `NOT_COORDINATOR` / `COORDINATOR_LOAD_IN_PROGRESS` on a commit.
  The new retry path is covered by unit tests (`tests/test_streaming_reliability.py`), not by
  this chaos test. The 2026-10-24 errors came from a live broker under heavy load, which a
  restart does not reproduce.
- A first pause run (20261025T040322Z) failed because of the harness, not the consumer. The
  consumers exited on their 90 s idle timeout while Kafka was frozen, and the producer's 30 s
  flush gave up with 89 events queued. The script now raises the idle timeout in pause mode
  and pauses only after the producer has finished.

### Throughput benchmark (2026-10-25, branch `consumer-throughput`)

**Problem:**
- The full-validation stream ran at about 8 events/s on 3 consumers, falling from about 10 to
  6.7 events/s as the stream went on.
- Scorer latency was p50/p95/p99 207/549/843 ms, about 2x earlier figures.
- Target: 30+ events/s on 3 consumers, unthrottled, with the same delivery guarantees.

**Setup** (`scripts/bench_throughput.py`, `docker-compose.bench.yml`; identical for BEFORE and
AFTER):
- Database `fraud_bench`: a `pg_dump` clone of the demo database on the demo PostgreSQL.
  - The last 4,000 events by (TransactionDT, TransactionID) were deleted from it; they are the
    replay events. The cut is at TransactionDT 15,700,530, and no timestamp straddles it.
  - Left: 114,108 transactions and 4,948 alerts.
  - The demo database is only read.
- Feast state in a separate Redis (`bench-redis`), built with `feast_store materialize --before
  15700530`. Those are the same transactions fraud_bench holds (transaction IDs checked
  identical); 57,791 keys.
- Consumers: 3 containers from the serving image, joined to the demo network, exactly as the demo
  runs them (`FEATURE_SOURCE=feast`).
  - Own topic `bench-transactions` (3 partitions) and group `bench-scorer` on the demo broker.
  - `STAGE_TIMINGS=1` (per-stage timing log line, otherwise no change).
- Producer: a container on the same network, with pre-encoded events.
  - Produce times and `scored_at` share the Docker VM clock. The VM clock drifted by up to ~1 s
    against the Windows host clock, which spoiled the first end-to-end attempt (see below).
- Laptop: 12 vCPUs in the Docker VM. The wall clock was logged every 5 s in each run: the
  largest gap was 5 s, so no suspend.

**Feature source:** the demo consumers run `FEATURE_SOURCE=feast` (container environment, and
`features=feast` in every scored log line). So no per-event history query reaches PostgreSQL.

**PostgreSQL** (EXPLAIN ANALYZE on fraud_bench, busiest card `9500|321|150|visa|226|debit`,
2,894 rows):

| Per-event query | Plan | Time |
|---|---|---|
| dedupe `get` by `transaction_id` | PK index scan | 0.8 ms |
| `INSERT scored_transactions ... ON CONFLICT DO NOTHING RETURNING` | PK arbiter | 2.0 ms |
| `INSERT alerts ... ON CONFLICT DO NOTHING` (alerts only) | unique-key arbiter + FK check | 1.5 ms |
| `card_history` (`FEATURE_SOURCE=store` only; not run by the Feast consumers) | bitmap scan on `ix_scored_card_dt` + sort | 39.0 ms |

Indexes: `scored_transactions_pkey (transaction_id)`, `ix_scored_card_dt (card_id,
transaction_dt)`, `alerts_pkey`, `alerts_transaction_id_key`. Nothing is missing for the Feast
path. Only `card_history` grows with a card's history, and it is not on the served path.

**BEFORE** (main + timing logs; replay events 0-999 at 5/s, then 1,000-1,999 unthrottled):

| Measure | Run 1 (5 events/s) | Run 2 (unthrottled) |
|---|---|---|
| Throughput, first to last `scored_at` | 5.2 events/s (= input rate) | **13.07 events/s** |
| End-to-end, produce -> `scored_at` (= start of the DB write, before the Feast push and offset commit), p50 / p95 / p99 | 114 / 669 / 2,668 ms | 38.1 / 71.9 / 76.1 s (backlog) |
| Scorer latency (features + score + save + push), p50 / p95 / p99 | 125 / 396 / 660 ms | 203 / 357 / 427 ms |

Per stage, Run 2 (ms, p50 / p95), with Run 1 p50 in brackets:

| Stage | p50 / p95 | Run 1 p50 |
|---|---|---|
| Kafka poll (returning call; backlog) | 0.02 / 0.04 | (266, waiting) |
| dedupe `get` (PostgreSQL) | 0.75 / 2.95 | (0.84) |
| entity keys (pandas, one row) | 31.0 / 61.6 | (22.3) |
| Feast read (`get_online_features`) | 46.6 / 84.9 | (28.3) |
| features from state (`history_features`) | 0.09 / 0.21 | (0.08) |
| model frame (`build_model_frame` on one row) | 40.2 / 76.6 | (24.2) |
| model predict (encoder + XGBoost) | 51.4 / 116.1 | (28.4) |
| PostgreSQL write (`save`) | 7.2 / 20.3 | (6.5) |
| Feast push (2 pushes) | 15.8 / 38.3 | (13.1) |
| commit (synchronous) | 2.0 / 13.0 | (1.6) |
| whole message (`process`) | 204.8 / 359.9 | (126.1) |

CPU during Run 2 (`docker stats`, mean / max, 100% = one core): consumers 186 / 280, 168 / 265
and 199 / 293%; Kafka 38 / 173%; PostgreSQL 3.6 / 8.8%; bench Redis 3.5 / 34.5%.

**Findings:**
- PostgreSQL, Redis and Kafka are not the bottleneck. The database work is under 10 ms per
  event, and Redis and PostgreSQL stay below 4% CPU.
- **The consumers are CPU-bound on overhead, not on useful work.**
  - Each single-threaded consumer burns about 2 cores (42 threads per process). XGBoost
    predicts with `nthread` = all 12 cores, so 3 consumers start 36 OpenMP workers for
    one-row predictions.
  - In the consumer image, one `inplace_predict` takes 9.75 ms with 12 threads vs 0.51 ms with
    `nthread=1`, before any contention. Under 3-way contention, predict is 51 ms and every
    other CPU stage slows down too (Run 2 stages are 1.4-1.8x Run 1).
- **Repeated pandas work per event:**
  - The event is turned into a one-row DataFrame twice (entity keys, model frame).
  - With Feast, `model_row_from_features` runs the whole batch `build_model_frame`, which
    recomputes the entity keys and every history feature, and then overwrites all 12 history
    features with the Feast values.
  - Entity keys + model frame + encoder are about 60% of the message time.
- Feast SDK overhead: `get_online_features` takes 28-47 ms and the two pushes 13-16 ms, for
  about 2 ms of Redis round trips.
- The earlier slowdown "as `scored_transactions` grew" is not caused by the table on the Feast
  path. The per-event queries are index lookups and inserts. The 2026-10-24 stream also shared
  the CPU with tests and image builds; that run was not instrumented, so this is not proven.

**Fixes** (all allowed kinds: repeated work removed, objects cached; no change to the model, the
features, the feature source, the schema or delivery):
1. XGBoost `nthread=1` for serving. Scores are bit-identical (tested against the default
   thread count).
2. `predict` passes a NumPy array (columns in `feature_columns` order, checked against the
   booster at load) and has a single-row encoder path.
3. The one-event frame is built once (keys + model row), and one-row entity keys are rendered
   value by value with the same NumPy formatting.
4. Feast path: model row = `raw_model_columns` (the helper `build_model_frame` now uses) + the
   Feast values. The batch history computation it used to run was fully overwritten.
5. Feast reads and writes go through Feast's provider (`online_read` / `ingest_df`, the calls
   behind `get_online_features` / `push`), with the two state feature views resolved once.
   - Same online store, keys and value conversion.
   - Uses `FeatureStore._get_provider()`, a private accessor; `feast==0.47.0` is pinned, and a
     test compares the result with the SDK path. `FeastFeatures(direct_online=False)` keeps the
     SDK path.

Every fast path has an equality test against the old code (model row, entity keys, encoder,
scores, single- vs multi-thread predictions, and Feast direct vs SDK reads, writes and final
state). The existing serving-vs-batch parity tests pass unchanged.

**AFTER** (same fraud_bench, same setup; replay events 2,000-2,999 at 5/s, then 3,000-3,999
unthrottled. These are different, adjacent events of the same clone, and the history includes
the BEFORE events):

| Measure | BEFORE | AFTER |
|---|---|---|
| Run 2 throughput (unthrottled, first to last `scored_at`) | 13.07 events/s | **46.83 events/s** (3.6x) |
| Run 1 end-to-end to `scored_at` (start of the DB write, before the Feast push and offset commit), p50 / p95 / p99 (5 events/s) | 114 / 669 / 2,668 ms | **30.6 / 60.8 / 74.0 ms** |
| Run 2 end-to-end to `scored_at` (same definition), p50 / p95 / p99 (backlog of 1,000) | 38.1 / 71.9 / 76.1 s | 11.0 / 19.1 / 20.7 s |
| Scorer latency Run 1, p50 / p95 / p99 | 125 / 396 / 660 ms | 41 / 84 / 109 ms |
| Scorer latency Run 2, p50 / p95 / p99 | 203 / 357 / 427 ms | 48 / 105 / 147 ms |
| Run 2 stage p50 / p95: Kafka poll | 0.02 / 0.04 ms | 0.03 / 0.05 ms |
| dedupe `get` | 0.75 / 2.95 ms | 0.95 / 2.13 ms |
| entity keys (now incl. building the event frame) | 31.0 / 61.6 ms | 17.2 / 43.2 ms |
| Feast read | 46.6 / 84.9 ms | **1.7 / 4.2 ms** |
| features from state | 0.09 / 0.21 ms | 0.12 / 0.29 ms |
| model frame | 40.2 / 76.6 ms | 4.0 / 9.6 ms |
| model predict | 51.4 / 116.1 ms | **3.1 / 7.1 ms** |
| PostgreSQL write | 7.2 / 20.3 ms | 4.2 / 9.4 ms |
| Feast push | 15.8 / 38.3 ms | 15.6 / 30.9 ms |
| commit | 2.0 / 13.0 ms | 1.8 / 5.1 ms |
| whole message (`process` stage; excludes poll and commit) | 204.8 / 359.9 ms | 50.0 / 107.6 ms |
| Run 2 CPU mean / max, consumers | 186/280, 168/265, 199/293% | 49/87, 55/89, 49/88% |
| Run 2 CPU mean / max, Kafka / PostgreSQL / bench Redis | 38/173, 3.6/8.8, 3.5/34.5% | 56/192, 8.2/15.9, 7.5/15.6% |
| Run 1 CPU mean, consumers | 69, 56, 78% | 5.8, 7.0, 6.1% |

All 4,000 replay events were stored exactly once (status `scored`, none duplicate or
dead-lettered; DLQ empty, lag 0). fraud_bench ended at 118,108 rows and 5,093 alerts, the
demo database's counts.

**End-to-end parity:** each of the 4,000 replayed scores is bit-identical to the score the demo
database stored on 2026-10-24 for the same transaction, with the old code (max difference 0.0,
0 alert decisions differ). This holds for events 0-1,999 (BEFORE code) and 2,000-3,999 (AFTER
code).

**What is left** (not needed for the target):
- Feast push, 15.6 ms: Feast's DataFrame -> Arrow -> protobuf conversion for one row, twice.
  Fix: build the two value protos directly and call the online store's `online_write_batch`.
- Event frame + keys, 17 ms: `events_to_frame` (pandas `from_records` + per-column
  `to_numeric`) for one event. Fix: a single-event constructor with the same dtypes, tested
  equal.
- Both are allowed kinds of change and were not needed to reach 30 events/s.

**Discarded runs:**
- The first BEFORE pair (`reports/bench/discarded-before-*.json`) was not discarded for
  suspend (largest gap 5 s), but for measurement reasons:
  - Its producer ran on the Windows host, so end-to-end latency compared the host clock with
    the Docker VM's `scored_at`. The two clocks moved about 480 ms apart between the two runs,
    and within Run 1 they drifted and stepped by up to about 1 s.
  - In Run 2 the host producer needed 33.8 s for 1,000 events, because it encoded inside the
    loop and competed for CPU.
- Its Run 2 throughput (13.28 events/s) agrees with the repeat (13.07).
- `bench_throughput reset` restored fraud_bench and the bench Redis to the post-setup state
  (same counts and key count), and both BEFORE runs were repeated with the containerised,
  pre-encoded producer.

## 7. Limitations

- **Cold start:** the Feast state must be materialized from the batch history before the stream
  starts (`feast_store materialize`, docs/feature_store.md "Cold start"); then online features
  equal training. The `FEATURE_SOURCE=store` path only knows ingested events and stays skewed.
- **Late events:** an event that arrives after later events is missing from their history.
  Feast state updates for late events are approximate (docs/feature_store.md); the store path
  never returns future events (tested) but cannot add an event to history already scored.
- **Ordering:** ordering is guaranteed only within a card's partition. Events of one `uid` all
  share a card key, so they are ordered too.
- **Throughput:**
  - With Feast (the default): 46.8 events/s on 3 consumers on one laptop, about 16 per
    consumer ("Throughput benchmark", 2026-10-25). Not a production capacity figure.
  - The `FEATURE_SOURCE=store` path still reads the card's whole history per event, so its cost
    grows with card history (39 ms for the busiest card's query alone).
- **Single connection:** each process uses one PostgreSQL connection behind a lock. There is no
  connection pool, retry policy or migration tool; tables are created at startup.
- **Security:** the API has no authentication. Kafka uses plaintext with no authentication or
  ACLs. Ports are bound to localhost only. The database password comes from `.env` (gitignored).
  Stored events contain card and address fields, which would need access controls and a
  retention policy in any real deployment.
- **Proxy entities:** `card_id`/`uid` remain the coarse proxies documented in `features.md`, so
  the history features inherit their collisions.
- **Float precision:** training loads float columns as float32, while the API receives float64.
  For decimal amounts the online history features can differ from training by float32
  rounding. Measured on the saved model over 2,000 random histories (2026-10-22), feature rows
  differed in 736 cases, but scores differed in 0 and no alert decision changed.
- **Stop timeout:** containers created by Docker Compose v2.40.3-desktop on this machine got
  `StopTimeout=1`, so a plain `docker stop` or a Docker Desktop shutdown SIGKILLs them after
  1 s. This caused the API's exit code 137 on 2026-10-21, after a graceful uvicorn shutdown.
  Every service now sets `stop_grace_period`:
  - Kafka 60 s: controlled shutdown flushes and closes the partition logs.
  - PostgreSQL 60 s: SIGINT fast shutdown with a final checkpoint.
  - Consumer 30 s.
  - API 10 s.

  A container gets the new value only when it is recreated. All four containers were
  recreated on 2026-10-22 and now carry these timeouts.

  Measured shutdowns, all well inside their grace periods:
  - Kafka: 1.8 s, exit 143, LogManager and broker report a clean shutdown.
  - PostgreSQL: recreate in 2.6 s, fast shutdown with checkpoint.
  - Consumer and API: about 1 s each, exit 0.
- **Kafka data location (migrated 2026-10-22):** the broker used to write to
  `/tmp/kafka-logs` inside the container layer. Recreating the container would have discarded
  all topics, committed consumer offsets and cluster metadata. It now uses
  `KAFKA_LOG_DIRS=/var/lib/kafka/data` on the named volume `kafkadata`.

  Migration method, rehearsed first on a throwaway broker:
  1. Clean stop with `docker stop -t 60`.
  2. `docker cp` the log directory out as a tar, which keeps owners and modes.
  3. Extract it into the volume as `appuser`.
  4. Compare per-file SHA-256 manifests: 305 of 305 files identical.
  5. Recreate the broker.

  After migration the broker had the same `directory.id`, TopicId and end/committed offsets.
  The image formats an empty log dir with the same default cluster ID, so the cluster ID
  alone cannot detect an accidental re-initialisation; `directory.id` and TopicId can.

  Backups kept: image `fraud-platform-kafka-premigration:2026-10-22` and a tar in the
  session scratchpad.
- **API logging:** the API configures root logging at startup, so `src.*` records reach the
  container log alongside uvicorn's lines: model version, store, per-score lines and shutdown.
- **Restart policy:** all four services use `unless-stopped`. A graceful SIGTERM makes the
  consumer exit 0, so `on-failure` would have left it down after a Docker restart while the
  other services came back. `unless-stopped` still respects an intentional
  `docker compose stop`.

  Before this change only the consumer had a restart policy. After a Docker restart it looped
  while Kafka and PostgreSQL stayed down: more than 230 restarts with
  `postgres: Name or service not known`, observed on 2026-10-22.
- **Served model scope:** the bundle is trained on the first 80% of time only, so its stored
  validation metrics stay a genuine held-out estimate. A deployment would retrain on all
  labelled data under a documented procedure.
- **No monitoring yet:** no drift monitoring (Evidently) and no alert-review UI. The model is
  the untuned Milestone 2 baseline.
