# Data: Kafka, PostgreSQL, Feast

What flows where, generated from the code: Kafka settings from `src/serving/config.py` and
`src/streaming/`, the table columns from the DDL constants in `src/serving/store.py`,
`src/monitoring/report_store.py` and `src/graph/rings.py` (parsed, not typed by hand). The HTTP
API is in [api.md](api.md).

## Kafka

| Topic | Partitions | Written by | Read by |
|---|---|---|---|
| `transactions` (`KAFKA_TOPIC`) | 3 (`KAFKA_NUM_PARTITIONS`, broker auto-create) | producer (`src/streaming/producer.py`): validation-period IEEE-CIS events in time order | consumers, group `fraud-scorer` (`KAFKA_GROUP_ID`), one per partition |
| `transactions.dlq` (`KAFKA_DLQ_TOPIC`) | 1, `retention.ms` 30 days, created by the consumer at startup | consumers | nobody automatically (inspect by hand) |

**Message (`src/streaming/events.py`):**

- **value:** the event as JSON, exactly the `POST /score` request body (`TransactionEvent`,
  fields in [api.md](api.md)). Labels and `D1` are never sent.
- **key:** the `card_id` proxy (card1-card6 joined with `|`), or `id:<TransactionID>` when
  `card1` is missing, so each card's events stay in one partition and in order (one writer per
  card's Feast state).
- **header:** `schema_version` = `"1"`.

**Retry and DLQ** (`src/streaming/reliability.py`; details in [serving.md](serving.md) §4):

- *invalid* (bad JSON, schema violation): dead-lettered on the first attempt;
- *transient* (database or network down): retried indefinitely with capped backoff (0.5 s,
  doubling, max 30 s, jitter); consumption stalls, nothing is skipped;
- *permanent* (anything else): first attempt + 3 retries, then dead-lettered.
- A DLQ record keeps the original key, value and headers and adds `dlq.*` headers (reason,
  source topic/partition/offset, attempts, sanitised error type and detail).
- Offsets are committed only after the database write or a confirmed DLQ write (at-least-once;
  the `transaction_id` primary key deduplicates). Retriable commit errors are retried
  0.2 / 0.5 / 1 s, then left for redelivery.

## PostgreSQL

The API and every consumer create and migrate `scored_transactions`, `alerts` and
`case_reviews` at startup, idempotently, under `pg_advisory_xact_lock(7291406001)`. The
dashboard never runs DDL. Columns marked "added later" come from additive `ALTER TABLE ... ADD
COLUMN IF NOT EXISTS` migrations.

### `scored_transactions` (src/serving/store.py)

| Column | Type and constraints |
|---|---|
| `transaction_id` | BIGINT PRIMARY KEY |
| `transaction_dt` | BIGINT NOT NULL CHECK (transaction_dt >= 0) |
| `card_id` | TEXT |
| `uid` | TEXT |
| `event` | JSONB NOT NULL |
| `score` | DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1) |
| `threshold` | DOUBLE PRECISION NOT NULL |
| `is_alert` | BOOLEAN NOT NULL |
| `model_version` | TEXT NOT NULL |
| `scored_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |

Indexes: index `ix_scored_card_dt` (card_id, transaction_dt).

**Key:** `transaction_id` (idempotency: `ON CONFLICT DO NOTHING`). **Writes:** the scorer (`PostgresStore.save`, API and consumers), one row per event, in the same transaction as its alert. **Reads:** the dashboard (`src/dashboard/data.py`), the drift job (scores and features of the current window), `feast_store materialize` (only `MIN(transaction_dt)`), and the `FEATURE_SOURCE=store` path (`card_history`).

### `alerts` (src/serving/store.py)

| Column | Type and constraints |
|---|---|
| `alert_id` | BIGSERIAL PRIMARY KEY |
| `transaction_id` | BIGINT NOT NULL UNIQUE REFERENCES scored_transactions (transaction_id) |
| `score` | DOUBLE PRECISION NOT NULL |
| `status` | TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'investigating', 'confirmed_fraud', 'false_positive', 'closed')) |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() (added later) |
| `version` | INTEGER NOT NULL DEFAULT 1 (added later) |

**Keys:** `alert_id`; unique `transaction_id`. **Writes:** the scorer inserts `(transaction_id, score)` for scores at or above the threshold (never touches analyst fields); the dashboard updates `status`, `updated_at` and `version` (optimistic lock on `version`). **Reads:** the dashboard.

### `case_reviews` (src/serving/store.py)

| Column | Type and constraints |
|---|---|
| `id` | BIGSERIAL PRIMARY KEY |
| `alert_id` | BIGINT NOT NULL REFERENCES alerts (alert_id) |
| `transaction_id` | BIGINT NOT NULL REFERENCES scored_transactions (transaction_id) |
| `from_status` | TEXT NOT NULL |
| `to_status` | TEXT NOT NULL |
| `label` | TEXT CHECK (label IN ('fraud', 'not_fraud')) |
| `analyst` | TEXT NOT NULL CHECK (btrim(analyst) <> '') |
| `notes` | TEXT |
| `alert_version` | INTEGER NOT NULL |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |
| `submit_token` | TEXT (added later) |

Indexes: index `ix_case_reviews_alert` (alert_id, id); index `ix_case_reviews_tx` (transaction_id, id); unique index `ux_case_reviews_submit_token` (submit_token).

**Keys:** `id`; unique `submit_token`. Append-only: a trigger rejects UPDATE and DELETE. **Writes:** the dashboard, one row per status change, in the same transaction as the alert update. **Reads:** the dashboard (review history) and `src.dashboard.data.latest_labels` (latest labelled review per transaction, the input for feedback-label retraining, not built yet).

### `drift_reports` (src/monitoring/report_store.py)

| Column | Type and constraints |
|---|---|
| `id` | BIGSERIAL PRIMARY KEY |
| `run_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |
| `synthetic` | BOOLEAN NOT NULL |
| `synthetic_note` | TEXT |
| `now_dt` | BIGINT NOT NULL |
| `label_cutoff_dt` | BIGINT NOT NULL |
| `window_start_dt` | BIGINT NOT NULL |
| `window_end_dt` | BIGINT NOT NULL |
| `reference_start_dt` | BIGINT NOT NULL |
| `reference_end_dt` | BIGINT NOT NULL |
| `current_rows` | INTEGER NOT NULL |
| `reference_rows` | INTEGER NOT NULL |
| `feature_count` | INTEGER NOT NULL |
| `drifted_feature_count` | INTEGER NOT NULL |
| `drifted_feature_share` | DOUBLE PRECISION NOT NULL |
| `top_drifted_features` | JSONB NOT NULL |
| `prediction_drift_method` | TEXT NOT NULL |
| `prediction_drift_score` | DOUBLE PRECISION NOT NULL |
| `prediction_drift_detected` | BOOLEAN NOT NULL |
| `performance_start_dt` | BIGINT NOT NULL |
| `performance_end_dt` | BIGINT NOT NULL |
| `delayed_rows` | INTEGER NOT NULL |
| `delayed_positives` | INTEGER NOT NULL |
| `delayed_pr_auc` | DOUBLE PRECISION |
| `champion_valid_pr_auc` | DOUBLE PRECISION NOT NULL |
| `triggered` | BOOLEAN NOT NULL |
| `trigger_reasons` | JSONB NOT NULL |
| `retrain_run_id` | TEXT |
| `challenger_version` | TEXT |
| `promotion_decision` | TEXT |
| `promotion` | JSONB |
| `windows` | JSONB NOT NULL |
| `report_path` | TEXT NOT NULL |
| `config` | JSONB NOT NULL |
| `forced` | BOOLEAN NOT NULL DEFAULT false (added later) |
| `top_gain_features` | JSONB (added later) |
| `drifted_top_gain_features` | JSONB (added later) |

**Key:** `id`. Created by the drift job, not at API startup (the dashboard tolerates its absence). **Writes:** `python -m src.monitoring.run_drift` (`report_store`), one row per run. **Reads:** the dashboard Drift page.

### `fraud_rings` (src/graph/rings.py)

| Column | Type and constraints |
|---|---|
| `rank` | integer PRIMARY KEY |
| `ring_id` | integer NOT NULL |
| `uid_count` | integer NOT NULL |
| `card_count` | integer NOT NULL |
| `tx_count` | integer NOT NULL |
| `total_amount` | double precision NOT NULL |
| `shared_devices` | jsonb NOT NULL |
| `mean_risk` | double precision NOT NULL |
| `scored_tx` | integer NOT NULL |
| `fraud_rate` | double precision NOT NULL |
| `first_dt` | bigint NOT NULL |
| `last_dt` | bigint NOT NULL |
| `uids` | jsonb NOT NULL |
| `generated_at` | timestamptz NOT NULL DEFAULT now() |

**Key:** `rank` (1-20). Created by the rings job. **Writes:** `python -m src.graph.rings` (host batch job; replaces all rows with the top 20 ranked rings). **Reads:** the dashboard (Fraud rings page; ring panel on Case detail).

## Feast

Details: [feature_store.md](feature_store.md). Definitions in `src/features/feast_store.py`.

| Object | What |
|---|---|
| Entities | `uid` (card key + addr1), `card_id` (card1-card6) |
| Feature view `uid_state` | per-uid state: counts and amount sums before / at the latest timestamp, latest and previous timestamps, timestamps within 30 days, write timestamp |
| Feature view `card_state` | per-card state: last known `DeviceInfo`, `DeviceType`, `addr1`, `addr2` before / at the latest timestamp |
| Request source `scoring_event` | the event's time, amount, change-column values and key presence |
| On-demand view `history_features` | the 12 model features from both states + the request |
| Online store | Redis (`FEAST_REDIS`): the current state of every entity, updated after each saved event |
| Offline store | parquet files in `FEAST_REPO_DIR`: state snapshots for point-in-time retrieval and the cold-start state |

What reads what:

- **Serving** (`FEATURE_SOURCE=feast`, the default): `FeastFeatures.read` reads `uid_state` and
  `card_state` from Redis and computes the features with `entity_state.history_features`; after
  the save, `push` writes the updated states back.
- **Training** (`python -m src.models.train`, default `--feature-source feast`):
  `get_historical_features` on the two state views from the offline store, point-in-time per
  transaction, then the same `history_features` function. `--feature-source pandas`, direct
  calls and automatic retraining use the batch code (`compute_history_features`), proven
  identical.
- **Cold start:** `python -m src.features.feast_store materialize --before validation` folds
  the batch transactions before the cut-off into each entity's state and loads it into Redis.
