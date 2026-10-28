# Real-Time Financial Fraud Detection Platform

[![CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml)

Scores card transactions for fraud as they stream through Kafka, using an XGBoost model with
leakage-safe history features served from Feast. Alerts go to PostgreSQL, where analysts review
them in a Streamlit dashboard. A batch GNN looks for fraud rings, and an Evidently drift job can
trigger retraining. It runs as a local demo on Docker Compose and Minikube; it is **not** a
production system.

## Results

| What | Result | Source |
|---|---|---|
| Served model | XGBoost v2 (`baseline-9fdad27ea3b0`), validation PR-AUC **0.2992** | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Alerts at threshold 0.12806208 | precision **0.3136**, recall **0.3930**, 4.31% of transactions alerted | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Fraud rate (IEEE-CIS) | ~3.5% (3.499%) | [reports/eda_summary.md](reports/eda_summary.md) |
| GNN alone | validation PR-AUC 0.2021 (loses to XGBoost) | [reports/gnn_stacking.md](reports/gnn_stacking.md) |
| GNN + XGBoost blend | worse than XGBoost for both variants (95% CI [-0.0346, -0.0137] and [-0.0263, -0.0118]); not served | [reports/gnn_blend.md](reports/gnn_blend.md) |
| Throughput | **46.83 events/s** on 3 consumers, scorer p50 **48 ms** | [docs/serving.md](docs/serving.md) "Throughput benchmark" |
| Tests | 580 unit + 41 integration passed (2026-10-28) | [docs/test-results-unit.txt](docs/test-results-unit.txt), [docs/test-results-integration.txt](docs/test-results-integration.txt) |

More results and experiments (class imbalance, alert budgets, train/serve skew, demo stream,
chaos test): [docs/results.md](docs/results.md).

## Architecture

```mermaid
flowchart LR
  P[Kafka producer] --> K[[Kafka]] --> C[Consumers x3] --> S[Scorer: Feast read + XGBoost v2]
  S <--> R[(Feast online state, Redis)]
  S --> PG[(PostgreSQL: scores, alerts)] <--> D[Streamlit dashboard]
  O[(Feast offline store)] --> T[Training] --> M[(MLflow registry, host)]
  PG --> DR[Evidently drift job] --> T
```

Full diagram, with what runs in Docker Compose, in Minikube and on the host only:
[docs/architecture.md](docs/architecture.md).

## Dashboard

Streamlit analyst dashboard on the demo database (2026-10-28; details in
[docs/dashboard.md](docs/dashboard.md)):

| Overview | Alert queue |
|---|---|
| ![Overview](docs/screenshots/01-overview.png) | ![Alert queue](docs/screenshots/02-alert-queue.png) |
| **Case 1502** | **Drift reports** |
| ![Case 1502](docs/screenshots/03-case-1502-top.png) | ![Drift reports](docs/screenshots/08-drift.png) |

More: [case 1502 customer and review history](docs/screenshots/04-case-1502-customer-history.png),
[case 4705 review history](docs/screenshots/05-case-4705-history.png),
[fraud rings](docs/screenshots/06-fraud-rings-table.png),
[ring #5](docs/screenshots/07-fraud-ring-5.png).

## Quickstart (Docker Compose)

Needs Docker Desktop, Python 3.11, the dataset (see the next section) and a model bundle in
`artifacts/baseline/` (`python -m src.models.train`, below). Windows `cmd` syntax:

```
copy .env.example .env                       # then set POSTGRES_PASSWORD
docker compose up -d --build --wait          # kafka, postgres, redis, api, 3 consumers, dashboard
docker compose stop api consumer
set FEAST_REDIS=localhost:16379
.venv\Scripts\python -m src.features.feast_store materialize --before validation
docker compose up -d --no-deps api consumer
set KAFKA_BOOTSTRAP_SERVERS=localhost:9092
.venv\Scripts\python -m src.streaming.producer --skip 0 --limit 2000
curl http://127.0.0.1:18000/health           # API; dashboard: http://localhost:8501
```

The materialize step loads the Feast state from the transactions before the validation
cut-off, so streamed events get the same features as training
([docs/feature_store.md](docs/feature_store.md)). Scoring details: [docs/serving.md](docs/serving.md);
dashboard: [docs/dashboard.md](docs/dashboard.md).

## Setup from a fresh clone

> **Not yet verified on a clean machine.**

```
git clone https://github.com/manojkumargudge/real-time-fraud-detection-platform.git
cd real-time-fraud-detection-platform
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

1. Dataset: accept the rules at https://www.kaggle.com/c/ieee-fraud-detection/rules, download
   from https://www.kaggle.com/c/ieee-fraud-detection/data and put `train_transaction.csv` and
   `train_identity.csv` in `data/raw/ieee-cis/` ([data/README.md](data/README.md)). Check with
   `.venv\Scripts\python -m src.data.load`.
2. Model bundle: `.venv\Scripts\python -m src.models.train` writes `artifacts/baseline/` and logs
   the run to a local `mlflow.db`. The CLI builds features from the Feast offline store;
   `--feature-source pandas` builds the identical matrix much faster
   ([docs/feature_store.md](docs/feature_store.md)). A fresh run is not byte-identical to the
   served v2, which was trained from an uncommitted working tree.
3. Unit tests (no services needed): `.venv\Scripts\python -m pytest --ignore=tests/test_integration.py`.
4. Integration tests use a separate Compose project that never touches the demo stack:
   ```
   docker compose -f docker-compose.test.yml up -d --wait
   set TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
   set TEST_KAFKA_BOOTSTRAP=localhost:29092
   set TEST_REDIS=localhost:26379
   .venv\Scripts\python -m pytest tests/test_integration.py
   ```
5. Lint and types: `.venv\Scripts\ruff check .` and `.venv\Scripts\python -m mypy`.
6. Optional: the GNN needs `pip install -r requirements-gnn.txt` (torch 2.8.0+cpu,
   [docs/gnn.md](docs/gnn.md)).

## Kubernetes (Minikube)

The same services run in Minikube (namespace `fraud-k8s`) with a Feast materialize Job, a
producer Job, consumer liveness checks and a drift CronJob (checks only). The cluster uses a
small replay, not the full demo database. Build, deploy and verify steps, and results:
[docs/kubernetes.md](docs/kubernetes.md).

```
minikube start --driver=docker --cpus=4 --memory=4g
k8s/build-image.sh
kubectl apply -k k8s
```

(The full order, including the namespace, data stores and the materialize Job, is in
docs/kubernetes.md "Deploy".)

## Brief to evidence

Every requirement of the project brief, with its status, evidence and gap:
[docs/brief-evidence.md](docs/brief-evidence.md).

## Known limitations

- Local demo: single-node Kafka, no authentication or TLS, no secrets manager, local MLflow
  (SQLite, no server).
- Retraining and promotion run on the host only; the Minikube CronJob runs drift checks only,
  and `production` is never moved automatically ([docs/monitoring.md](docs/monitoring.md)).
- The drift trigger is not calibrated on drift-free periods and has no minimum sample size.
- The served v2 was trained from an uncommitted working tree, so it cannot be rebuilt exactly.
- The threshold is chosen on a training holdout and applied to the refit model, so the alert
  rate can drift.
- IEEE-CIS has no account ID: "user" is a proxy (card + address), and the graph has no account
  node.
- Fraud rings: a missing addr1 becomes part of the uid (as `<NA>`), so all of a card's
  transactions without an address count as one customer. This affects most of the stored rings:
  of the top 20, 16 contain at least one such uid and 9 consist only of them (45 of 73 members,
  62%), so ring membership partly reflects missing addresses rather than distinct customers.
- Feast: late events are approximate, concurrent writes to one entity can lose an update, and
  a failed push is only logged ([docs/feature_store.md](docs/feature_store.md)).
- Dashboard: no authentication, no connection pool, one replica.
- Security scan results and accepted risks: [docs/security.md](docs/security.md).

## Next steps

- Retrain from analyst feedback labels (`case_reviews`, latest label per transaction).
- Model registry link: a link file plus `GET /model` so serving reports its MLflow version
  ([docs/next-steps/model-registry-link.md](docs/next-steps/model-registry-link.md)).
- An explicit account (`uid`) node in the GNN graph.
- Exclude uids with missing addr1 from ring linking.
- An MLflow server with a shared artifact store, so retraining can run in the cluster.
- Rank drift features by their impact on the score instead of by gain.

## How this project was built

I (Manojkumar M Hegde) own this project. I planned the milestones, set the
requirements and engineering rules, made the design decisions (for example the
model choice, the Feast feature source, the drift-trigger rules and the
reliability approach), reviewed every change and verified the results.

I used Claude Code (Anthropic) as an AI coding assistant to implement most of
the code, tests and documentation under my direction. Commits it helped write
carry a "Co-Authored-By: Claude" line.
