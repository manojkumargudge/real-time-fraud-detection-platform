# Real-Time Financial Fraud Detection Platform

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
- XGBoost baseline: validation PR-AUC 0.2504 against a 3.44% fraud rate
  ([reports/model_comparison.md](reports/model_comparison.md))
- Kafka producer and consumer with a retry policy and a dead-letter queue (`transactions.dlq`)
- Idempotent PostgreSQL writes (`TransactionID` primary key)
- FastAPI `/health` and `/score`
- Docker Compose stack (Kafka, PostgreSQL, API, consumer)
- MLflow tracking and model registry (local SQLite `mlflow.db`, `python -m src.models.train`;
  registered as `fraud-xgboost`; alias `champion` = best candidate (version 2, no class
  weight), `production` = currently deployed (version 1, `baseline-6026acdc8e2e`))
- 355 unit tests ([docs/test-results-unit.txt](docs/test-results-unit.txt)) and 14 integration
  tests ([docs/test-results-integration.txt](docs/test-results-integration.txt))

### Planned

- Feast feature store
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
