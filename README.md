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
- Feast feature store ([docs/feature_store.md](docs/feature_store.md)): per-entity state (`uid`,
  `card_id`) in Redis, updated after every saved event; an on-demand view computes the history
  features from state + event. Point-in-time retrieval equals the batch features; replay and
  live checks show the same features and scores as the history path for in-order events (late
  events can differ). Off by default for reads (`FEATURE_SOURCE=feast` to enable)
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

### Verified on live data

On the local Docker Compose stack (2026-10-22):

- The `transactions` topic held 2,300 messages = 2,000 unique `TransactionID`s + 300
  duplicates; each of the 2,000 was stored exactly once.
- Publishing 5 new events added exactly 5 rows; publishing the same 5 again added 0.

## Setup (Windows, Python 3.11)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
.venv\Scripts\python -m mypy        # strict type check of src/ and tests/ (pyproject.toml)
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
