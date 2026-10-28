# Scoring API

FastAPI app `src/serving/api.py` (`uvicorn src.serving.api:app`; Compose service `api` on
127.0.0.1:18000, Kubernetes Service `api:8000`). The OpenAPI spec is exported from the app object
to [openapi.json](openapi.json) (`app.openapi()`; regenerate after changing the schema).

The same scorer, `Scorer.score_event`, serves `POST /score` and the Kafka consumers
([serving.md](serving.md), [data.md](data.md)). No authentication (local demo).

## GET /health

**Purpose:** liveness and dependency check: is the model loaded and the store reachable.

**Request:** no parameters.

**Response** (no response model in the spec; built in `api.py`):

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `ok`, or `degraded` when the store does not answer |
| `model_version` | string | served bundle (`baseline-` + first 12 hex of the model's sha256) |
| `event_schema_version` | integer | event schema the API accepts (1) |
| `store` | string | `postgres`, or `memory` when `DATABASE_URL` is not set |
| `store_ok` | boolean | result of the store ping |

Example (HTTP 200):

```json
{"status": "ok", "model_version": "baseline-9fdad27ea3b0", "event_schema_version": 1, "store": "postgres", "store_ok": true}
```

**Errors:** HTTP **503** with the same body, `"status": "degraded"` and `"store_ok": false`, when
the store ping fails.

## POST /score

**Purpose:** score one transaction event, persist the score (and an alert at or above the
threshold), and update the Feast entity state. Idempotent per `TransactionID`: a repeat returns
the stored result with `duplicate: true` and changes nothing.

**Request:** JSON body `TransactionEvent` (`src/serving/schema.py`, schema version 1). Field
names follow IEEE-CIS. Unknown fields are rejected, including `isFraud` and `D1`.

| Field | Type | Rule |
|---|---|---|
| `TransactionID` | integer | **required**, 0 to 2^53 − 1; idempotency key |
| `TransactionDT` | integer | **required**, 0 to 2^40, seconds of IEEE-CIS relative time |
| `TransactionAmt` | number | **required**, ≥ 0, finite |
| `card1` | integer or null | ±(2^53 − 1); null → no entity, no history features |
| `card2`, `card3`, `card5`, `addr1`, `addr2`, `dist1` | number or null | finite |
| `ProductCD`, `card4`, `card6`, `P_emaildomain`, `R_emaildomain`, `DeviceType`, `DeviceInfo` | string or null | ≤ 128 characters, no NUL |
| `has_identity` | boolean | default false; must be true if `DeviceType` or `DeviceInfo` is set |

**Response:** `ScoreResponse`:

| Field | Type | Meaning |
|---|---|---|
| `transaction_id` | integer | the event's `TransactionID` |
| `score` | number | fraud probability from the served XGBoost model |
| `is_alert` | boolean | `score >= threshold` |
| `threshold` | number | the bundle's threshold (0.12806208431720734 for v2) |
| `model_version` | string | bundle that scored it |
| `duplicate` | boolean | true if the `TransactionID` was already stored (stored result returned) |
| `history_rows` | integer | earlier transactions of the event's uid (`uid_prior_txn_count`; 0 if no key) |
| `latency_ms` | number | time spent in the scorer for this request |
| `history_features` | object | the 12 point-in-time history features used (empty on duplicates) |

**Example:** the first validation event (`k8s/local/events.json`, as sent by the producer Job in
the 2026-10-25 Kubernetes run):

```json
{"TransactionID": 3459432, "TransactionDT": 12192900, "TransactionAmt": 33.26100158691406,
 "ProductCD": "C", "card1": 9300, "card2": 103.0, "card3": 185.0, "card4": "visa", "card5": 138.0,
 "card6": "debit", "addr1": null, "addr2": null, "dist1": null, "P_emaildomain": "gmail.com",
 "R_emaildomain": "gmail.com", "DeviceType": "desktop", "DeviceInfo": null, "has_identity": true}
```

Response (HTTP 200):

```json
{
  "transaction_id": 3459432,
  "score": 0.17915059626102448,
  "is_alert": true,
  "threshold": 0.12806208431720734,
  "model_version": "baseline-9fdad27ea3b0",
  "duplicate": false,
  "history_rows": 922,
  "history_features": {
    "uid_prior_txn_count": 922.0, "uid_txn_count_1h": 0.0, "uid_txn_count_24h": 2.0,
    "uid_txn_count_7d": 33.0, "uid_txn_count_30d": 159.0, "uid_seconds_since_prev_txn": 82428.0,
    "uid_prior_amt_mean": 41.6013, "uid_amt_to_prior_mean": 0.7995,
    "card_deviceinfo_changed": null, "card_devicetype_changed": 0.0,
    "card_addr1_changed": null, "card_addr2_changed": null
  }
}
```

Where the values come from: `score`, `is_alert` and `model_version` are the cluster's stored
result for this event (`k8s/local/cluster-scores.csv`; in-cluster alert 1). The history features
were recomputed from the batch features, which equal the Feast features (docs/feature_store.md),
and re-scoring them with v2 gives exactly the stored score (0.17915059626102448, difference
0.0). The two mean values are rounded here. `latency_ms` (a required number in every response)
was not recorded for this event, so it is left out of the example; see "Latency" below.

**Errors:**

- **422** for any invalid body: unknown field, out-of-range value, `NaN`/`Infinity`, NUL in a
  text field, device fields without `has_identity`. A custom handler keeps the body valid JSON
  even when the rejected input was `NaN`. Real examples:

  ```json
  {"detail": [{"type": "extra_forbidden", "loc": ["body", "isFraud"], "msg": "Extra inputs are not permitted", "input": 1}]}
  {"detail": [{"type": "greater_than_equal", "loc": ["body", "TransactionAmt"], "msg": "Input should be greater than or equal to 0", "input": -5, "ctx": {"ge": 0.0}}]}
  ```

- **500** if the store or the feature source fails while scoring (for example PostgreSQL
  unreachable). Nothing is half-written: the score row and its alert are inserted together, and
  the Feast state is updated only after the save.

## Latency (measured; see the source for the setup)

| What | p50 / p95 / p99 | Source |
|---|---|---|
| Scorer (`Scorer.score_event`: Feast read, features, predict, save, push), 3 consumers, 5 events/s | 41 / 84 / 109 ms | [serving.md](serving.md) "Throughput benchmark" (line 478), 2026-10-25 |
| Same, backlog of 1,000 events | 48 / 105 / 147 ms | [serving.md](serving.md) "Throughput benchmark" (line 479), 2026-10-25 |
| Features + score only, Feast path, host process (before the 2026-10-25 speed-ups) | 107.5 / 179.0 / 283.2 ms | [feature_store.md](feature_store.md) "Latency" |

`POST /score` runs the same `Scorer.score_event`. HTTP-level latency of `/score` has not been
measured since the 2026-10-25 speed-ups; the earlier 5-event samples in the API container are in
docs/feature_store.md "Latency".
