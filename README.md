# Real-Time Financial Fraud Detection Platform

An AI/ML project for detecting suspicious financial transactions,
identifying potential fraud networks, and supporting analyst investigations.

## Project Status

Under development.

## Planned Technology Stack

- Python and FastAPI
- XGBoost and PyTorch Geometric
- Apache Kafka and Feast
- MLflow and Evidently AI
- PostgreSQL and React
- Docker and Kubernetes

## Planned Features

- Transaction fraud-risk scoring
- Graph-based fraud detection
- Real-time transaction streaming
- Analyst alerts and case management
- Model evaluation and drift monitoring

## Setup (Windows, Python 3.11)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
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
- Milestone 3: real-time scoring slice: baseline model training, FastAPI `/health` and
  `/score`, Kafka producer/consumer, PostgreSQL persistence with duplicate handling, Docker
  Compose ([docs/serving.md](docs/serving.md)). Local demo only; see its limitations.

## Documentation

Architecture, installation instructions, API documentation, test results,
and demonstration details will be added as development progresses.
