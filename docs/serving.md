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
- **Invalid messages** (bad JSON or schema violations) are logged and skipped, so they cannot
  block a partition. There is no dead-letter topic yet.
- **Infrastructure errors** (for example, the database is down) stop the consumer *without*
  committing the offset.

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
- **No monitoring yet:** no drift monitoring (Evidently) and no alert-review UI. The model is
  the untuned Milestone 2 baseline.
