# Real-Time Scoring Slice (Milestone 3)

This is a local, end-to-end path:

**Kafka event → consumer → online features → XGBoost → PostgreSQL (scores + alerts)**

There is also a synchronous `POST /score` API. This is a working local demo, **not** a
production deployment. See §6 for its limits.

```
producer (replays IEEE-CIS) --> Kafka topic "transactions" --> consumer --+
                                                                         +--> Scorer --> PostgreSQL
client --> FastAPI POST /score ------------------------------------------+     |
                                     (history read from PostgreSQL) <----------+
```

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

Measured on this machine: training took 35.8 s. The validation results match the Milestone 2
seed-42 baseline: PR-AUC 0.2504, threshold 0.672, precision 0.264, recall 0.419, and 5.47% of
validation transactions flagged.

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

## 3. Online feature path (`src/serving/online_features.py`)

For an event at time `t` on card `c`, the store returns `c`'s stored events with
`TransactionDT < t`. The event and that history then go through
**`compare.build_model_frame` — the same function used in training**.

One query is enough: every `uid` is `card_id` plus `addr1`, so it is a sub-group of one card.
Earlier events and the event's own ID are also filtered out a second time in Python, as a
safeguard.

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
- The dead-letter topic is at-least-once: a crash between its acknowledgement and the commit
  writes a second copy, identifiable by the same `dlq.source.*` headers.

**Consumer counts:** the stop log line, `consumer stopped counts=...`, reports:
- `scored`, `duplicate`, `invalid` and `failed`: one per message;
- `dead_lettered`: confirmed dead-letter writes;
- `retries`;
- `interrupted`: left uncommitted;
- `commit_rejected`.

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

**Consumer crashes (pre-existing bug, not fixed here).** All 3 consumers exited twice, at 06:38
and 07:41 UTC. A synchronous offset commit failed with `NOT_COORDINATOR` /
`COORDINATOR_LOAD_IN_PROGRESS` during heavy load on the laptop. `consumer.py` tolerates only the
rebalance errors (`ILLEGAL_GENERATION`, `UNKNOWN_MEMBER_ID`, `REBALANCE_IN_PROGRESS`) and
re-raises anything else. Docker restarted the consumers. No data was lost or duplicated: offsets
are committed after the DB write, and redelivered events are deduplicated. Fix: treat retriable
coordinator errors like the rebalance errors (redeliver).

## 7. Limitations

- **Cold start:** history contains only events this system has ingested. Until earlier history
  is replayed, online features differ from training, where the history spans the whole file.
- **Late events:** an event that arrives after later events is missing from their history. The
  store never returns future events (tested), but it cannot add an event to history that has
  already been scored.
- **Ordering:** ordering is guaranteed only within a card's partition. Events of one `uid` all
  share a card key, so they are ordered too.
- **Throughput:** about 10 events/s per consumer, and the cost grows with card history. Raising
  it needs incremental per-entity aggregates or a feature store; Feast is planned. Moving off
  the shared batch code requires the parity test to keep passing.
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
