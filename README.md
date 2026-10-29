# Real-Time Financial Fraud Detection Platform

[![CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml)

Scores card transactions for fraud as they stream through Kafka, using an XGBoost model with
leakage-safe history features served from Feast. Alerts go to PostgreSQL, where analysts review
them in a Streamlit dashboard. A batch GNN looks for fraud rings, and an Evidently drift job can
trigger retraining. It uses production-style engineering (CI, tests, security scanning, retries
and a dead-letter queue, Kubernetes) but runs as a local demo on Docker Compose and Minikube; it
is not deployed to real traffic.

## Results

| What | Result | Source |
|---|---|---|
| Served model | XGBoost v2 (`baseline-9fdad27ea3b0`), validation PR-AUC **0.2992**, against **0.0344** for a random model (PR-AUC of random scores = the validation fraud rate, 3.44%) | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Alerts at threshold 0.12806208 | precision **0.3136**, recall **0.3930**, 4.31% of transactions alerted | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Fraud rate (IEEE-CIS) | ~3.5% (3.499%) | [reports/eda_summary.md](reports/eda_summary.md) |
| GNN alone | validation PR-AUC 0.2021 (loses to XGBoost) | [reports/gnn_stacking.md](reports/gnn_stacking.md) |
| GNN + XGBoost blend | worse than XGBoost for both variants (95% CI [-0.0346, -0.0137] and [-0.0263, -0.0118]); not served | [reports/gnn_blend.md](reports/gnn_blend.md) |
| GNN with an account (uid) node | no gain: GNN alone 0.2008; blends still worse than XGBoost (transaction -0.0245 [-0.0354, -0.0140], entity -0.0144 [-0.0205, -0.0081]); not served | [docs/gnn.md](docs/gnn.md) "Account node", [reports/gnn_blend_account.md](reports/gnn_blend_account.md) |
| Throughput | **46.83 events/s** on 3 consumers (unthrottled, 1,000-event backlog; scorer p50 48 ms in that run); scorer p50 **41 ms** at a steady 5 events/s | [docs/serving.md](docs/serving.md) "Throughput benchmark" |
| Feast training matrix | identical to the pandas matrix v2 was trained on: 472,432 / 472,432 training rows, sha256 `66db4b971d3e…` both sides (validation 118,108 rows, encoded matrix also equal) | [docs/feature_store.md](docs/feature_store.md) "Training from the Feast offline store" |
| Drift trigger | fired without `--force-retrain` (days 170-181, top-feature rule); challenger kept out by the bootstrap rule: delta +0.0053, 95% CI [-0.0160, +0.0254], lower bound below the 0.01 margin | [docs/evidence/2026-10-27-drift-trigger/README.md](docs/evidence/2026-10-27-drift-trigger/README.md) |
| Fraud rings (exploratory) | GNN-ranked top-20 rings: 19.83% labelled fraud per transaction vs 3.50% overall as reported, but that reuses labels within a ring; with a strict temporal check (rank on training blocks, rate on validation rows only) 7.96% vs 2.89% in the same rings, on 9 frauds (account graph: 8.14%, 7 frauds). Most members are missing-address uids, see Known limitations | [docs/gnn.md](docs/gnn.md) "Ring ranking check" |
| Tests | 598 unit passed with the GNN extras (local, 2026-10-29); 573 passed + 10 skipped without them and 41 integration passed (CI run #70, main 305a75a) | CI run #70; older full logs: [docs/test-results-unit.txt](docs/test-results-unit.txt), [docs/test-results-integration.txt](docs/test-results-integration.txt) (2026-10-28) |

On one laptop with 3 consumer containers, the scoring pipeline processed **46.8 events/s** unthrottled. At a steady 5 events/s, scoring one event (Feast state read, features, XGBoost, PostgreSQL write, Feast state push) took **41 ms p50 / 109 ms p99**, and an event was in the database **31 ms p50 / 74 ms p99** after it was produced (1,000 replayed validation events; docs/serving.md "Throughput benchmark").

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
[docs/architecture.md](docs/architecture.md). API reference (`/health`, `/score`, OpenAPI spec):
[docs/api.md](docs/api.md). Kafka messages, PostgreSQL tables and Feast objects:
[docs/data.md](docs/data.md).

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

## Setup from a fresh clone

> **Verified 2026-10-28 from a fresh clone** (data copied; Kaggle download not tested).
> On Windows 11 (Git Bash), steps 1-3 and the Quickstart ran without an error; integration
> tests, lint/types and the GNN extras were not re-run in that clone.

Needs Python 3.11 and Docker Desktop; the loaded data alone takes about 1.5 GB of RAM. Windows
`cmd` first, macOS/Linux second.

```
git clone https://github.com/manojkumargudge/real-time-fraud-detection-platform.git
cd real-time-fraud-detection-platform
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt     # Windows
.venv/bin/python -m pip install -r requirements-dev.txt         # macOS/Linux
```

The install takes a while (about 22 min on the verification laptop: mlflow, feast, evidently,
streamlit, xgboost); it has not hung.

1. **Dataset.** You need a Kaggle account. Accept the rules at
   https://www.kaggle.com/c/ieee-fraud-detection/rules, download from
   https://www.kaggle.com/c/ieee-fraud-detection/data (or `kaggle competitions download -c
   ieee-fraud-detection` with the Kaggle CLI), **unzip** it, and put `train_transaction.csv` and
   `train_identity.csv` in a new folder `data/raw/ieee-cis/` ([data/README.md](data/README.md)).
   Check: `.venv\Scripts\python -m src.data.load` (macOS/Linux: `.venv/bin/python -m
   src.data.load`): 590,540 rows, fraud rate 3.4990%.
2. **Model bundle (the served v2 recipe):**
   ```
   .venv\Scripts\python -m src.models.train --feature-source pandas --imbalance none
   ```
   (macOS/Linux: `.venv/bin/python ...`). About 1-2 minutes. It writes `artifacts/baseline/` and
   logs the run to a local `mlflow.db`. On the verification run this reproduced the served v2
   **byte for byte**: model `baseline-9fdad27ea3b0` (same model.json sha256), validation PR-AUC
   0.2992.
   - `--imbalance none` matters. The brief asks for class weights or SMOTE. v1 was trained with
     class weights (`scale_pos_weight`, still the CLI default); over 3 seeds and two later time
     windows no weight scored higher (mean validation PR-AUC 0.2935 vs 0.2517), so v2 is served
     without weights ([reports/imbalance_comparison.md](reports/imbalance_comparison.md)). SMOTE
     was not used: it creates synthetic transactions with no real history, which breaks the
     history features. Without `--imbalance none` you get v1 (`baseline-6026acdc8e2e`, PR-AUC
     0.2504).
   - `--feature-source pandas` builds the same matrix as the default `feast` in about 2 s
     instead of about 38 min ([docs/feature_store.md](docs/feature_store.md)). The `feast`
     default needs nothing more than the dataset and the requirements: the command builds the
     Feast offline store itself in a temporary folder (no Redis, no `materialize`).
   - MLflow UI: `.venv\Scripts\mlflow ui --backend-store-uri sqlite:///mlflow.db` (repo root),
     then http://127.0.0.1:5000. Run artifacts are in `mlruns/`.
3. **Unit tests** (no services needed): `.venv\Scripts\python -m pytest
   --ignore=tests/test_integration.py`. Expect **573 passed, 10 skipped** without the GNN extras
   (598 passed with `requirements-gnn.txt` installed), about 8-15 minutes.
4. **Integration tests** use a separate Compose project that never touches the demo stack:
   ```
   docker compose -f docker-compose.test.yml up -d --wait
   set TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud    (macOS/Linux: export ...)
   set TEST_KAFKA_BOOTSTRAP=localhost:29092
   set TEST_REDIS=localhost:26379
   .venv\Scripts\python -m pytest tests/test_integration.py
   ```
5. Lint and types: `.venv\Scripts\ruff check .` and `.venv\Scripts\python -m mypy`.
6. Optional: the GNN needs `pip install -r requirements-gnn.txt` (torch 2.8.0+cpu,
   [docs/gnn.md](docs/gnn.md)).

## Quickstart (Docker Compose)

Do "Setup from a fresh clone" first: the stack needs the dataset (for the Feast state) and the
model bundle in `artifacts/baseline/`.

| Windows `cmd` | macOS/Linux |
|---|---|
| `copy .env.example .env` | `cp .env.example .env` |
| `set FEAST_REDIS=localhost:16379` | `export FEAST_REDIS=localhost:16379` |
| `.venv\Scripts\python ...` | `.venv/bin/python ...` |

```
copy .env.example .env                       # then set POSTGRES_PASSWORD (any value, local only)
docker compose up -d --build --wait          # kafka, postgres, redis, api, 3 consumers, dashboard
docker compose stop api consumer
set FEAST_REDIS=localhost:16379
.venv\Scripts\python -m src.features.feast_store materialize --before validation   # ~1-2 min
docker compose up -d --no-deps api consumer
set KAFKA_BOOTSTRAP_SERVERS=localhost:9092
.venv\Scripts\python -m src.streaming.producer --skip 0 --limit 2000              # ~1-2 min
curl http://127.0.0.1:18000/health           # API; dashboard: http://localhost:8501
```

The materialize step loads the Feast state from the transactions before the validation
cut-off, so streamed events get the same features as training
([docs/feature_store.md](docs/feature_store.md)). The dashboard has no login; enter an
**analyst name** in the sidebar before changing a case. Scoring details:
[docs/serving.md](docs/serving.md); dashboard: [docs/dashboard.md](docs/dashboard.md).

- **Stop vs reset:** `docker compose stop` keeps everything; `docker compose down -v` **deletes
  the database** and the Kafka and Redis data.
- **A second checkout** of this repo needs its own project name, because docker-compose.yml fixes
  `name: fraud-platform` and the host ports: stop the other stack and set
  `COMPOSE_PROJECT_NAME=<another name>` (or `docker compose -p <name> ...`), otherwise it reuses
  the first checkout's containers and volumes.

## Kubernetes (Minikube)

The same services run in Minikube (namespace `fraud-k8s`) with a Feast materialize Job, a
producer Job, consumer liveness checks and a drift CronJob (checks only). The cluster uses a
small replay, not the full demo database. Build, deploy and verify steps, and results:
[docs/kubernetes.md](docs/kubernetes.md).

```
minikube start --driver=docker --cpus=4 --memory=4g
k8s/build-image.sh                 # a bash script: use Git Bash on Windows
kubectl apply -k k8s
```

(The full order, including the namespace, data stores and the materialize Job, is in
docs/kubernetes.md "Deploy".)

## Brief to evidence

Every requirement of the project brief, with its status, evidence and gap:
[docs/brief-evidence.md](docs/brief-evidence.md).

## Experiment tracking

All training and evaluation runs are in MLflow (local `mlflow.db` + `mlruns/`, no server). Drift
checks are the exception: the Minikube CronJob (and the host drift job) write them only to
PostgreSQL; their history (`drift_reports` 1-9) was back-filled into `fraud-drift`.

| Experiment | Runs | Logged by |
|---|---|---|
| `fraud-xgboost` | XGBoost training (incl. the imbalance study and drift challengers); the registry (`champion` and `production` v2, `challenger` v7) | the training code |
| `fraud-evaluation` | entity-key comparison, seed robustness, evaluation study, robust evaluation (pilot, full), leakage audit, v1 vs v2, imbalance decision | `compare`, `robustness`, `evaluation_study`, `robust_eval` and `leakage_audit` at the end of `main()`; the existing reports were back-filled |
| `fraud-gnn` | GNN training, stacking and blend runs (tag `graph`: `default` or `account`); back-filled stacking and fraud-ring report runs | `src.graph.*` |
| `fraud-drift` | drift checks `drift_reports` 1-9 | back-filled |

Drift checks are stored in PostgreSQL (`drift_reports`). Ids 1-9 were back-filled into
`fraud-drift` on 2026-10-29 by `python -m scripts.mlflow_backfill` (SELECT only, tracking calls
only: the registry is never called; idempotent on `report_sha256` + run name). Back-filled runs
are tagged `backfilled=true`, `source_report`, `report_sha256` and `report_git_commit`.
**Known limitation:** new drift checks, including the Minikube CronJob's, write only to
PostgreSQL; they are not in MLflow (the back-fill covers ids 1-9).

## Known limitations

- Analyst feedback labels are saved (`case_reviews`, `src.dashboard.data.latest_labels`) but are
  not used for retraining yet.
- New drift checks (host job or Minikube CronJob) write only to PostgreSQL, not to MLflow; only
  `drift_reports` 1-9 were back-filled (see "Experiment tracking").
- Local demo: single-node Kafka, no authentication or TLS, no secrets manager, local MLflow
  (SQLite, no server).
- Retraining and promotion run on the host only; the Minikube CronJob runs drift checks only,
  and `production` is never moved automatically ([docs/monitoring.md](docs/monitoring.md)).
- The drift trigger is not calibrated on drift-free periods and has no minimum sample size.
- The served v2's MLflow run is tagged `git_dirty=true`, but it rebuilds identically: a fresh
  clone of main reproduced it byte for byte on 2026-10-28 (same model.json sha256, see Setup).
- The threshold is chosen on a training holdout and applied to the refit model, so the alert
  rate can drift.
- IEEE-CIS has no account ID: "user" is a proxy (card + address). The default GNN graph has no
  account node; the optional account graph adds one but links accounts to devices only through
  transactions (a direct edge needs edge-level time), and it did not improve the GNN.
- Fraud rings: a missing addr1 becomes part of the uid (as `<NA>`), so all of a card's
  transactions without an address count as one customer. This affects most of the stored rings:
  of the top 20, 16 contain at least one such uid and 9 consist only of them (45 of 73 members,
  62%), so ring membership partly reflects missing addresses rather than distinct customers.
- Feast: late events are approximate, concurrent writes to one entity can lose an update, and
  a failed push is only logged ([docs/feature_store.md](docs/feature_store.md)).
- Dashboard: no authentication, no connection pool, one replica.
- End-to-end latency is measured to the row's `scored_at`, the start of the database write, so it excludes the Feast state push (~14–16 ms p50) and the offset commit. HTTP `/score` latency has not been re-measured since the 2026-10-25 speed-ups.
- Security scan results and accepted risks: [docs/security.md](docs/security.md).

## Next steps

- Retrain from analyst feedback labels (`case_reviews`, latest label per transaction).
- Model registry link: a link file plus `GET /model` so serving reports its MLflow version
  ([docs/next-steps/model-registry-link.md](docs/next-steps/model-registry-link.md)).
- Exclude uids with missing addr1 from ring linking.
- An MLflow server with a shared artifact store, so retraining can run in the cluster.
- Rank drift features by their impact on the score instead of by gain.
- Next optimization: `entity_keys` (~17 ms) and `feast_push` (~16 ms) are about two-thirds of scoring time; `predict` is ~3 ms (docs/serving.md "Throughput benchmark", backlog run).

## How this project was built

I (Manojkumar M Hegde) own this project. I planned the milestones, set the
requirements and engineering rules, made the design decisions (for example the
model choice, the Feast feature source, the drift-trigger rules and the
reliability approach), reviewed every change and verified the results.

I used Claude Code (Anthropic) as an AI coding assistant to implement most of
the code, tests and documentation under my direction. Commits it helped write
carry a "Co-Authored-By: Claude" line.
