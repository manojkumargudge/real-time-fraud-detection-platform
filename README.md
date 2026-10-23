# Real-Time Financial Fraud Detection Platform

[![CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml)

CI runs ruff, strict mypy, the unit tests and the integration tests (throwaway PostgreSQL,
Kafka and Redis) on every push and pull request to `main`; CD then builds the serving image and
pushes it to `ghcr.io/manojkumargudge/real-time-fraud-detection-platform` (tags: commit SHA and
`latest`). Nothing is deployed.

An AI/ML project for detecting suspicious financial transactions,
identifying potential fraud networks, and supporting analyst investigations.

## Project Status

Work in progress: a tested local demo, not a production system.

### Done

- IEEE-CIS dataset loader and EDA ([reports/eda_summary.md](reports/eda_summary.md))
- Data-quality report ([reports/data_quality.md](reports/data_quality.md))
- Leakage-safe point-in-time features with an 18-check audit
  ([docs/audit_milestone2.md](docs/audit_milestone2.md); check 18, D1 semantics, is blocked
  pending provider documentation, so D1 stays out of serving)
- Chronological train/validation split
- XGBoost model served: `baseline-9fdad27ea3b0` (MLflow `fraud-xgboost` version 2, no class
  weight, seed 42): validation PR-AUC 0.2992 against a 3.44% fraud rate
  ([reports/imbalance_comparison.md](reports/imbalance_comparison.md)). Previous served model:
  version 1, `baseline-6026acdc8e2e` (`scale_pos_weight` 27.46), validation PR-AUC 0.2504
  ([reports/model_comparison.md](reports/model_comparison.md))
- Kafka producer and consumer with a retry policy and a dead-letter queue (`transactions.dlq`)
- Idempotent PostgreSQL writes (`TransactionID` primary key)
- FastAPI `/health` and `/score`
- Docker Compose stack (Kafka, PostgreSQL, Redis, API, consumer)
- Feast feature store, **the default feature source** ([docs/feature_store.md](docs/feature_store.md)):
  per-entity state (`uid`, `card_id`) in Redis, started from the training history (all
  transactions before the validation cut-off) and updated after every saved event; features are
  computed from state + event. Point-in-time retrieval, the cold-start replay and the live demo
  equal the batch (training) features. The older store path (each card's event history in
  PostgreSQL) stays behind `FEATURE_SOURCE=store`
- MLflow tracking and model registry (local SQLite `mlflow.db`, `python -m src.models.train`;
  registered as `fraud-xgboost`; alias `champion` = best candidate, `production` = currently
  deployed; both point to version 2 since 2026-10-23)
- 387 unit tests ([docs/test-results-unit.txt](docs/test-results-unit.txt)) and 15 integration
  tests ([docs/test-results-integration.txt](docs/test-results-integration.txt))

#### Experiment: class imbalance

- Removing class weighting (`scale_pos_weight` 27.46 -> none) raised mean validation PR-AUC
  from 0.2517 to 0.2935 over 3 seeds; the gain (+0.0418) is about 3 times the seed spread
  (0.0134).
- Confirmed on two other time windows with seed 42: W1 +0.0137 (small), W2 +0.0430.
- Trade-off: on W1 at the 1% alert budget, `none` was slightly worse; it was better at 5%.
- Deployed on 2026-10-23: version 2 is now `production` and served by the API and consumer
  (`/health` reports `baseline-9fdad27ea3b0`).
- Details: [reports/imbalance_comparison.md](reports/imbalance_comparison.md). Rerun with
  `python -m src.models.imbalance_comparison` and
  `python -m src.models.imbalance_comparison --confirm`.

#### Served model v1 vs v2 at fixed alert budgets

Same validation window (118,108 rows, 3.44% fraud); at each budget both models flag the same
number of transactions ([reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md),
`python -m src.models.version_comparison`):

| Budget | Recall v1 | Recall v2 | Precision v1 | Precision v2 | Threshold v1 | Threshold v2 |
|---|---|---|---|---|---|---|
| 1% | 0.1321 | 0.1550 | 0.4543 | 0.5330 | 0.8967 | 0.3640 |
| 2% | 0.2266 | 0.2507 | 0.3898 | 0.4312 | 0.8401 | 0.2433 |
| 5% | 0.3991 | 0.4181 | 0.2746 | 0.2877 | 0.6922 | 0.1121 |

Where v2 is worse: at its own deployed threshold (0.1281) it flags 4.31% of transactions and
catches 39.30% of frauds, against v1's 5.47% and 41.93% at 0.6721; and on window W1 at the 1%
budget (confirmation above). Thresholds are chosen per model, by max F1 on the training
holdout, so the alert rate changed with the model.

Known limitation: the threshold is chosen by max F1 on the stage-1 model's holdout scores but
applied to the refit model (trained on all training rows), so the deployed alert rate can differ
from the holdout's. Next step: an alert-budget threshold, checked on post-deployment data.

### Planned

- PyTorch Geometric GNN for fraud rings
- Evidently drift reports and a retraining trigger
- Analyst dashboard
- Kubernetes (Minikube)

Note: the IEEE-CIS fraud rate is about 3.5% (3.499% overall), not under 1%.

### Why Feast is the default: train/serve skew

The store path only knows events the service has ingested. With a stream that starts at the
validation cut-off (as in production, after training), 96% of streamed rows get history
features that differ from training, validation PR-AUC falls from **0.2992 to 0.2533**, and
**2% of alert decisions flip** (2,345 of 118,108). Feast starts from the training history, so
its features equal the training features. The cost is latency: features + score take p50
107.5 ms with Feast vs 61.1 ms with the store path (p95 179.0 vs 88.8 ms), because of Feast
Python SDK overhead (a Redis round trip is 2 ms); see docs/feature_store.md.

### Demo: unseen validation data (reset 2026-10-23)

The demo stack streams **only validation-period transactions** (`TransactionDT` >=
12,192,900), in time order; the producer refuses anything earlier or out of order, and labels
are never sent. Feast was materialized from the 472,432 earlier transactions (39,087 uid and
13,792 card states). Earlier demo data was backed up and cleared.

| | First 5,000 events, 1 consumer | Next 5,000 events, 3 consumers |
|---|---|---|
| Throughput while a backlog existed | 7.3 events/s | 12.0 events/s (1.6x, not 3x) |
| Per-event latency in the consumer, p50 / p95 | 112.2 / 239.8 ms | 214.3 / 422.2 ms |
| Peak sampled consumer lag (whole batch published at once) | 3,910 | 3,130 |
| Errors, dead-lettered events, consumer restarts | 0 / 0 / 0 | 0 / 0 / 0 |

First 5,000 events: 248 alerts (4.96%), 107 of them true fraud (precision 0.4315, recall
0.4693; 228 frauds, 4.56% fraud rate). Full validation at the same threshold: 4.31% alerts,
precision 0.3136, recall 0.3930. The gap is the time window, not serving: the same 5,000
events scored offline with training features give identical scores (max difference 9e-10)
and identical alerts, and these first ~1.9 days have a higher fraud rate (4.56% vs 3.44%);
with 228 frauds the recall's 95% margin is about +-0.065.

Three consumers did not scale linearly: per-event latency doubled, which points to a shared
bottleneck (the laptop's CPU or the PostgreSQL / Redis round trips); not yet profiled. Events
are keyed by `card_id` (events without card1 have no card key and use `id:<TransactionID>`),
so each card's events stay in one partition, in order, and each card's Feast state has one
writer among the consumers. After the rebalance, the Redis state of 300 sampled streamed uids
(216 cards) equalled the state rebuilt from batch data, and their next unseen events got the
same features as batch.

Earlier (2026-10-22) the at-least-once path was checked on the first IEEE-CIS transactions:
2,300 messages = 2,000 unique `TransactionID`s + 300 duplicates, each stored once.

## Setup (Windows, Python 3.11)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest --ignore=tests/test_integration.py   # unit tests, no services
.venv\Scripts\ruff check .
.venv\Scripts\python -m mypy        # strict type check of src/ and tests/ (pyproject.toml)
```

Integration tests run against a **separate Compose project** (`docker-compose.test.yml`:
project `fraud-platform-test`, ports 25432 / 29092 / 26379, its own volumes, test-only
credentials), so they never touch the demo stack's PostgreSQL, Kafka or Redis:

```
docker compose -f docker-compose.test.yml up -d --wait
set TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
set TEST_KAFKA_BOOTSTRAP=localhost:29092
set TEST_REDIS=localhost:26379
.venv\Scripts\python -m pytest tests/test_integration.py
docker compose -f docker-compose.test.yml down -v   # removes only the test project's volumes
```

Dataset download instructions: [data/README.md](data/README.md).

## Progress

- Milestone 1: validated IEEE-CIS loader, EDA and data-quality report
  ([reports/eda_summary.md](reports/eda_summary.md), [reports/data_quality.md](reports/data_quality.md),
  figures in `reports/figures/`; rerun steps in [data/README.md](data/README.md)).
- Milestone 2: leakage-safe entity features, chronological split, train-only encoding
  ([docs/features.md](docs/features.md)). Run: `.venv\Scripts\python -m src.features.build`.
- Milestone 2 experiment: XGBoost comparison of default vs D1-based entity keys
  ([reports/model_comparison.md](reports/model_comparison.md)). Run:
  `.venv\Scripts\python -m src.models.compare` and `python -m src.models.robustness`.
- Milestone 2 audit: point-in-time, split, encoder and online-parity checks with evidence
  ([docs/audit_milestone2.md](docs/audit_milestone2.md)).
- Milestone 3: real-time scoring slice: baseline model training, FastAPI `/health` and
  `/score`, Kafka producer/consumer, PostgreSQL persistence with duplicate handling, Docker
  Compose ([docs/serving.md](docs/serving.md)). Local demo only; see its limitations.

## Documentation

Architecture, installation instructions, API documentation, test results,
and demonstration details will be added as development progresses.
