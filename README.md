# Real-Time Financial Fraud Detection Platform

[![CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml)

Scores card transactions for fraud as they stream through Kafka, using an XGBoost model with
leakage-safe history features served from Feast. Alerts go to PostgreSQL, where analysts review
them in a Streamlit dashboard. A batch job links users into candidate fraud rings, which a GNN ranks
(no lift on the strict check, see Results), and an Evidently drift job can
trigger retraining. It uses production-style engineering (CI, tests, security scanning, retries
and a dead-letter queue, Kubernetes) but runs as a local demo on Docker Compose and Minikube; it
is not deployed to real traffic.

The [Compute Engine release checklist and runbook](docs/production-readiness.md) covers
the Linux Compose deployment, service exposure, model and registry setup, and release gates.
Production release is blocked. The latest verified cloud environment was a private
single-VM rehearsal in `us-central1-a`. The proposed IAP-only portfolio deployment
is pending separate approval and live verification. [Restricted deployment controls](docs/production-readiness.md#restricted-portfolio-deployment)

## Results

| What | Result | Source |
|---|---|---|
| Served model | XGBoost v2 (`baseline-9fdad27ea3b0`), validation PR-AUC **0.2992**, against **0.0344** for a random model (PR-AUC of random scores = the validation fraud rate, 3.44%) | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Alerts at threshold 0.12806208 | precision **0.3136**, recall **0.3930**, 4.31% of transactions alerted | [reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md) |
| Fraud rate (IEEE-CIS) | ~3.5% (3.499%) | [reports/eda_summary.md](reports/eda_summary.md) |
| GNN alone | validation PR-AUC 0.2021 (loses to XGBoost) | [reports/gnn_stacking.md](reports/gnn_stacking.md) |
| GNN + XGBoost blend | worse than XGBoost for both variants (95% CI [-0.0346, -0.0137] and [-0.0263, -0.0118]); not served | [reports/gnn_blend.md](reports/gnn_blend.md) |
| GNN with an account (uid) node | no gain: GNN alone 0.2008; blends still worse than XGBoost (transaction -0.0245 [-0.0354, -0.0140], entity -0.0144 [-0.0205, -0.0081]); not served | [docs/gnn.md](docs/gnn.md) "Account node", [reports/gnn_blend_account.md](reports/gnn_blend_account.md) |
| Load test, `POST /score` (bench copy, one laptop) | 2026-11-15, connection pool (3 runs): one client **15.0-27.9 req/s**, p50 **30-54 ms**; 5 clients 7.6-13.8 req/s, p50 334-605 ms; 0 errors. Unchanged at 5 clients against the single locked connection on 2026-11-11 (12.5-14.3 req/s, [docs/evidence/2026-11-11-load-test/](docs/evidence/2026-11-11-load-test/README.md)): removing the lock did not add throughput. Likely bottleneck (diagnosis, not proven): one uvicorn process doing CPU-bound scoring. Laptop noise is large (one client fell from 27.9 to 15.0 req/s over three runs with no code change). Direct API calls on one API process, not comparable to the 46.83 events/s Kafka streaming figure below | [docs/evidence/2026-11-15-api-pool/](docs/evidence/2026-11-15-api-pool/README.md) |
| Throughput | **46.83 events/s** on 3 consumers (unthrottled, 1,000-event backlog; scorer p50 48 ms in that run); scorer p50 **41 ms** at a steady 5 events/s. Measured 2026-10-25 on ca57b2c (`consumer-throughput`); not re-measured after the producer idempotence (M1) and connection-pool (S3) changes. A 200-event replay on main b62c29b (2026-11-16) scored bit-identically to the demo ([docs/evidence/2026-11-16-m2/](docs/evidence/2026-11-16-m2/README.md) B1) | [docs/serving.md](docs/serving.md) "Throughput benchmark" |
| Forensic replay | Reconciled **58,468 distinct transaction scores and 2,430 alerts** against the independent batch oracle, with zero unexplained differences, duplicates, dead letters or residual lag after injected service failures. | [Full retained Kafka cohort](docs/production-readiness.md#full-retained-kafka-cohort) |
| Final-image GCP capacity | **108,000 Kafka scores** reconciled across four repeats, with matching partition offsets and zero dead letters or oracle differences. Release medians were **24.26 rps** at one client and **32.28 rps** at eight spread clients. One-client p95 was **51.58 ms**. All sustained gates passed. | [November 28 rehearsal evidence](docs/evidence/2026-11-28-release-continuation.md) |
| Feast training matrix | identical to the pandas matrix v2 was trained on: 472,432 / 472,432 training rows, sha256 `66db4b971d3e…` both sides (validation 118,108 rows, encoded matrix also equal) | [docs/feature_store.md](docs/feature_store.md) "Training from the Feast offline store" |
| Drift trigger | fired without `--force-retrain` (days 170-181, top-feature rule); challenger kept out by the bootstrap rule: delta +0.0053, 95% CI [-0.0160, +0.0254], lower bound below the 0.01 margin | [docs/evidence/2026-10-27-drift-trigger/README.md](docs/evidence/2026-10-27-drift-trigger/README.md) |
| Fraud rings (exploratory) | GNN-ranked top-20 rings: 17.23% labelled fraud per transaction vs 3.50% overall as reported, but that reuses labels within a ring; with a strict temporal check (rank on training blocks, rate on validation rows only) 2.22% vs 2.35% in the same rings, on 8 frauds (account graph: 1.42%, 5 frauds), so the ranking shows no lift. Uids with a missing address are excluded, see Known limitations | [docs/gnn.md](docs/gnn.md) "Ring ranking check" |
| Tests | 726 unit passed + 1 skipped (an opt-in demo-DB test) with the GNN extras and 44 integration passed (local, main b62c29b, 2026-11-16); 709 passed + 3 skipped in a fresh clone without the GNN extras; CI run #116: 701 passed + 11 skipped, 44 integration passed | CI run #116; full logs: [docs/test-results-unit.txt](docs/test-results-unit.txt), [docs/test-results-integration.txt](docs/test-results-integration.txt) (2026-11-16). CI differs from local because its unit job has no GNN stack and no IEEE-CIS dataset, so those tests skip there. |

On one laptop with 3 consumer containers, the scoring pipeline processed **46.8 events/s** unthrottled. At a steady 5 events/s, scoring one event (Feast state read, features, XGBoost, PostgreSQL write, Feast state push) took **41 ms p50 / 109 ms p99**, and an event was in the database **31 ms p50 / 74 ms p99** after it was produced (1,000 replayed validation events; docs/serving.md "Throughput benchmark").

More results and experiments (class imbalance, alert budgets, train/serve skew, demo stream,
chaos test): [docs/results.md](docs/results.md).

## Architecture

```mermaid
flowchart LR
  P[Kafka producer] --> K[[Kafka]] --> C[Consumers x3] --> S[Scorer: Feast read + XGBoost v2]
  S <--> R[(Feast online state, Redis)]
  S --> PG[(PostgreSQL: scores, alerts)] <--> D[Streamlit dashboard]
  O[(Feast offline store)] --> T[Training] --> M[(MLflow model registry)]
  PG --> DR[Evidently drift job] --> T
```

Full diagram, with what runs in Docker Compose, in Minikube and on the host only:
[docs/architecture.md](docs/architecture.md). API reference (`/health`, `/score`, OpenAPI spec):
[docs/api.md](docs/api.md). Kafka messages, PostgreSQL tables and Feast objects:
[docs/data.md](docs/data.md).

### Serving design and trade-offs

- Kafka buffers ingestion, partitions the stream by card, and lets independent consumers
  resume from committed offsets. In production a consumer processes one record at a time,
  checks for offset gaps and acknowledges only after score persistence and the Feast update
  or after a confirmed dead-letter write. Retries can redeliver messages. PostgreSQL's
  transaction ID key deduplicates stored scores and alerts.
- PostgreSQL stores the score, alert and pending Feast update atomically. A per-card lock
  orders Redis updates and allows a retry to finish a pending write before later scoring
  uses the card. Redis serves compact feature history quickly. Feast defines how the training
  and serving paths interpret that history. Complete Redis state loss requires a verified
  restore or rebuild before scoring resumes.
- XGBoost serves the champion because it delivered higher validation PR-AUC than the graph
  candidates. MLflow tracks the versioned model and artifacts and keeps the champion
  selection traceable. The retraining evaluation must clear the promotion gate.
- The checkpoint-enabled hosted benchmark at `333ad04` measured 87.32 requests/s at eight
  clients versus 70.36 on main. p99 fell from 193.88 ms to 132.71 ms. All 4,608 requests
  shared the same score, decision and feature-history digest and completed without
  errors or timeouts. The four-vCPU full-stack rehearsal reached CPU saturation.
  The later eight-vCPU private rehearsal passed the sustained capacity gates.
  [Hosted measurements](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45755631735)

### Portfolio evidence

- Reconciled **58,468 distinct scores and 2,430 alerts** with an independent batch
  oracle through Kafka, PostgreSQL and Redis interruptions. Partition offsets drained
  with zero unexplained differences, duplicate scores or dead letters.
  [Reconciliation evidence](docs/production-readiness.md#full-retained-kafka-cohort)
- Built a durable PostgreSQL checkpoint for Redis feature state. A promoted seed
  verified 52,879 entity rows, and all 12 history features matched the batch oracle
  for a 256-event sample. The coordinated checkpoint restored Kafka offsets,
  PostgreSQL outcomes, Redis and the MLflow champion in 30 seconds on the synthetic
  fixture. [Feature-state runbook](docs/feature_store.md#rebuilding-online-state-after-redis-loss)
- Validated a private single-VM release candidate with sustained load, exact
  reconciliation, and live Pub/Sub opening and closure for one incident. The 13
  High findings still hold unrestricted production release.
  [Release and deployment checklist](docs/production-readiness.md)

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

**Why flagged** (case detail, read-only, 2026-10-30): the top 5 features that raised the case's
fraud score and the top 3 that lowered it, as horizontal bars with the transaction's value and
the contribution in log-odds, from the served v2 model's built-in per-feature contributions
(XGBoost `pred_contribs`, no extra package). Anonymised IEEE-CIS columns keep their code name
plus "(anonymised)". Contributions explain the model's score, not the cause of fraud.

- **Feature vector:** rebuilt with the point-in-time batch code the model was trained with
  (the Feast serving path equals it), for the validation window, by
  `python -m src.dashboard.explain build` (host, about 1 min; writes the gitignored
  `artifacts/explain/features.csv.gz`, which Compose mounts read-only with the served bundle).
- **Parity check, per case:** contributions + bias = the model's margin, and sigmoid(margin)
  = the stored score within 1e-6; otherwise the panel shows a warning instead of bars. All
  5,093 demo alerts pass (max |score difference| 7.0e-8, max |sum - margin| 5.4e-6).
- Screenshot: [case 1502, why flagged](docs/screenshots/case-why-flagged.png).
- Minikube: the dashboard image was rebuilt on 2026-11-16 from main b62c29b (with sign-in), but
  the cluster has no feature file, so the panel shows "No explanation data in this deployment".

**Sessions** (sign-in from 2026-11-15; checked 2026-11-15 in Edge with Playwright, headless
and visible, view only):

- The signed-in name is kept in the Streamlit server's memory, in the session of one open
  page. It is not in the URL, a cookie or browser storage. The only cookie is Streamlit's
  `_streamlit_xsrf`.
- The page holds the random session id in JavaScript memory only. A reload (F5 or the reload
  button), a new tab or a fresh browser profile opens a new session, which shows the sign-in
  form. Copying the URL does not carry the sign-in.
- A short network drop without a reload reconnects to the same session, so the user stays
  signed in. Streamlit keeps a disconnected session for 120 s (`server.disconnectedSessionTTL`).
- Sign-out removes the name on the server. Neither a reconnect nor a reload afterwards
  brings it back.
- Restarting the dashboard container signs everyone out. Sessions live only in memory, so this
  follows from the design; it was not tested separately.

## Setup from a fresh clone

> **Verified 2026-11-16 from a fresh clone of main b62c29b** (data copied; Kaggle download not
> tested; [docs/evidence/2026-11-16-m2/](docs/evidence/2026-11-16-m2/README.md) B4). On
> Windows 11 (Git Bash):
> - the install, step 1 (590,540 rows, 3.4990%) and step 2 (v2 rebuilt byte for byte) ran
>   without an error;
> - step 3 gave 709 passed, 3 skipped.
>
> The Quickstart was last run from a clone on 2026-10-28, before the API key and dashboard
> sign-in. Integration tests, lint/types and the GNN extras were not re-run in a clone (they ran
> in the working checkout on the same day).

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
   - Training that logs to MLflow **refuses a dirty tree** (uncommitted tracked changes, or
     untracked files under `src/`, `configs/` or `requirements*`), so every logged model can be
     rebuilt from its `git_commit` tag. `--allow-dirty` overrides it and tags the run
     `allow_dirty=true` (same flag on `src.monitoring.run_drift` and
     `src.models.imbalance_comparison`; `--no-track` skips MLflow and the check).
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
   - Why-flagged panel (dashboard case page): `.venv\Scripts\python -m src.dashboard.explain
     build` (about 1 min, needs the dataset) writes `artifacts/explain/features.csv.gz`, which
     the Compose dashboard mounts read-only. Without it the panel says "No explanation data".
   - Registry link (optional; `GET /model`, [docs/api.md](docs/api.md)): add `--register` to the
     training command above (registers the run and points `champion` at it), then
     `.venv\Scripts\python -m src.models.link_registry --alias champion` writes
     `artifacts/baseline/registry.json` (only if the registry's `model.json` equals the served
     one; `--check` compares without writing). Without the file the API and consumers still
     start: they log a WARNING "no registry.json ... not linked" and `/model` returns
     `linked: false` with null registry fields.
3. **Unit tests** (no services needed): `.venv\Scripts\python -m pytest
   --ignore=tests/test_integration.py`. Expect **709 passed, 3 skipped** without the GNN extras
   (726 passed, 1 skipped with `requirements-gnn.txt` installed; 2026-11-16), about 9-18 minutes
   on an idle laptop.
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

**`.env` (gitignored, never commit it):** replace every placeholder from `.env.example`.
Compose refuses to start without `API_KEY` and `DASHBOARD_USERS`.

- `POSTGRES_PASSWORD`: any value, local only.
- `API_KEY`: the key for `POST /score` (header `X-API-Key`, at least 16 characters). Generate
  one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- `DASHBOARD_USERS`: one `name:<bcrypt hash>` entry per analyst, comma-separated and
  **single-quoted** (the hashes contain `$`). `python -m src.dashboard.auth hash alice` prompts
  for a password (at least 10 characters) and prints the entry; only the hash is stored.

```
copy .env.example .env                       # then replace the placeholders (next paragraph)
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
([docs/feature_store.md](docs/feature_store.md)). The dashboard asks for a sign-in (a user
from `DASHBOARD_USERS`). Every review records the signed-in name as the analyst. `POST /score`
needs `-H "X-API-Key: <API_KEY>"`; `/health` does not. Scoring details:
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
[docs/kubernetes.md](docs/kubernetes.md). Last redeployed 2026-11-16 from main b62c29b, with
Secret `fraud-auth`:
- all pods Ready;
- `POST /score` 401 without the key;
- 20 new events scored identically to the demo database;
- dashboard sign-in and all pages ok.

Evidence: [docs/evidence/2026-11-16-m2/](docs/evidence/2026-11-16-m2/README.md) B5.

```
minikube start --driver=docker --cpus=4 --memory=4g
k8s/build-image.sh                 # a bash script: use Git Bash on Windows
kubectl apply -k k8s
```

(The full order, including the namespace, data stores and the materialize Job, is in
docs/kubernetes.md "Deploy".)

## Scheduled retraining (host, Windows Task Scheduler)

Task `FraudDrift-Retrain` runs the drift job with automatic retraining every 6 hours:
`scripts/scheduled_drift.ps1` -> `python -m src.monitoring.run_drift` (retraining on, no
`--force-retrain`). When a rule in `configs/drift.toml` fires, it trains a challenger
(alias `challenger`) and runs the bootstrap promotion gate; `champion` moves only if the gate
passes, and `production` is never moved. It uses the repo's `mlflow.db` and the demo database
(password read from `.env`, never logged), checks PostgreSQL first, and logs to `logs/`.

```powershell
# from the repo root; the task is registered DISABLED
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1            # (re)register, disabled
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Enable    # start the schedule
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Disable   # pause it
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Unregister
Get-ScheduledTask -TaskName FraudDrift-Retrain | Get-ScheduledTaskInfo   # last run, result, next run
```

While enabled, every run adds a `drift_reports` row to the demo database, and a run that fires
registers a new challenger version in the real registry; the demo stack (at least `postgres`)
must be running, otherwise the run exits with code 2. A run that fires on a tree with
uncommitted code changes does not train: it still stores its `drift_reports` row with
`promotion_decision = "blocked: dirty tree (...); retraining not run"` (Drift page: Retrain
"blocked (dirty tree)"), logs an ERROR and exits with code 4. The scheduled script never passes
`--allow-dirty`. Proof run (started by Task Scheduler, on
copies): [docs/evidence/2026-10-30-scheduled-retrain/](docs/evidence/2026-10-30-scheduled-retrain/README.md).

## Brief to evidence

Every requirement of the project brief, with its status, evidence and gap:
[docs/brief-evidence.md](docs/brief-evidence.md).
See the [current completion matrix](docs/aiml-01-completion.md) for the release
gates and the [interview walkthrough](docs/interview-walkthrough.md) for the
model comparisons, data flow and engineering tradeoffs.

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
- Local demo: single-node Kafka, no TLS, no secrets manager, local MLflow
  (SQLite, no server).
- Kubernetes manifests are not hardened (hardening was dropped from scope on 2026-11-16):
  - the consumers have a liveness probe but no readiness probe;
  - images use the mutable tag `:k8s-local`, not a git SHA or digest;
  - the Kafka and drift init containers run as root;
  - no NetworkPolicy, PodDisruptionBudget, Ingress or TLS;
  - one replica except the consumers (3).

  Resource requests and limits are set on every workload ([docs/kubernetes.md](docs/kubernetes.md)
  "Not production").
- Demo Kafka topic: 118,109 offsets vs 118,108 stored rows. The offsets were already 118,109 on
  2026-10-24, and the messages are gone (retention), so the cause cannot be proven. The likely
  cause: a send retried by the event producer, which was not idempotent before 2026-11-12 (M1);
  the consumer dropped the duplicate by its `TransactionID` key. No row is missing or doubled
  ([docs/serving.md](docs/serving.md)).
- Scheduled retraining (Task Scheduler, every 6 h) runs on the host only, and only while the
  laptop is on, awake and the user is logged on; a missed run starts when the laptop is back.
  The Minikube CronJob still runs drift checks only (`--no-retrain`): in-cluster retraining
  needs an MLflow server with a shared artifact store. `production` is never moved
  automatically ([docs/monitoring.md](docs/monitoring.md)).
- The drift trigger fires without any injected shift: replayed on 11 in-training-period
  windows (days 61-137, frozen rules, no threshold change) it fired on **5 of 11** (days 96-130):
  top-feature drift in 4 (addr2, card3, R_emaildomain), the performance rule in 3, whose
  delayed-label rows were in the served model's training data (in-sample, not counted as
  evidence); the share and PSI rules never fired
  ([docs/evidence/2026-11-13-drift-calibration/](docs/evidence/2026-11-13-drift-calibration/README.md)).
  Retraining is gated by the bootstrap promotion check, so a fire costs a retrain, not a model
  change. Since 2026-11-12 the trigger needs at least 1,000 rows in both the current and the
  reference window (fixed a priori; below it the run is stored as "insufficient data" and never
  retrains; docs/monitoring.md).
- The served v2's MLflow run is tagged `git_dirty=true`, but it rebuilds identically: a fresh
  clone of main reproduced it byte for byte on 2026-10-28 (same model.json sha256, see Setup).
- The threshold is chosen on a training holdout and applied to the refit model, so the alert
  rate can drift.
- IEEE-CIS has no account ID: "user" is a proxy (card + address). The default GNN graph has no
  account node; the optional account graph adds one but links accounts to devices only through
  transactions (a direct edge needs edge-level time), and it did not improve the GNN.
- Fraud rings: a uid with a missing addr1 (`<NA>`) is a card's bucket of address-less
  transactions, not a customer, so these uids are excluded from ring linking (none in the 215
  rings). Coverage cost: 65,706 transactions (11.13%) and 2,601 of 43,018 uids have a missing
  addr1, with a fraud rate of 11.78% vs 3.50% overall, and rings can no longer surface them.
  Fraud transactions inside a ring fell from 645 to 482: 180 left and 17 entered (a device
  shared by more than 10 uids can now fall within the limit). With the exclusion the ring
  ranking shows no lift on the strict temporal check (docs/gnn.md "Ring ranking check").
- Feast: late events are approximate, concurrent writes to one entity can lose an update, and
  a failed push is only logged ([docs/feature_store.md](docs/feature_store.md)).
- Authentication is minimal (2026-11-15):
  - one shared API key for `POST /score`, with no per-client keys, rotation or rate limit;
  - dashboard sign-in from bcrypt hashes in `DASHBOARD_USERS`, with no roles or lockout;
  - no idle timeout: a dashboard tab that stays open and connected stays signed in until
    sign-out, a reload or a dashboard restart (see Dashboard, "Sessions");
  - no TLS; Kafka has no authentication.
  - Reviews made before 2026-11-15 keep their self-declared analyst names.
- Dashboard: no connection pool, one replica.
- End-to-end latency is measured to the row's `scored_at`, the start of the database write, so it excludes the Feast state push (~14–16 ms p50) and the offset commit. HTTP `/score` latency was load-tested on a bench copy (Results, "Load test").
- API concurrency: the API's store is a psycopg connection pool (`DB_POOL_SIZE`, default 10), but `/score` still runs in one uvicorn process. Throughput at 5 clients did not rise when the lock was removed (2026-11-15), which points to CPU-bound scoring in that process (not proven). More uvicorn workers are the next step. Each worker would have its own pool (workers x `DB_POOL_SIZE` connections) and its own copy of the model.
- PostgreSQL connection budget: `max_connections` 100; configured worst case 42 (API 10 + 3 consumers x 10 + dashboard + drift job), 82 with the throughput benchmark running as well ([docs/evidence/2026-11-15-api-pool/](docs/evidence/2026-11-15-api-pool/README.md)).
- Security scan results and accepted risks: [docs/security.md](docs/security.md). Accepted
  pip-audit risks (expire 2027-01-31) include three feast 0.47.0 RCE advisories:
  CVE-2025-11157 (Kubernetes materializer job, not used) and, added 2026-11-11, CVE-2026-18948
  and CVE-2026-56121 (RCE through the Feast feature/registry server): embedded Feast, local
  registry, no server exposed. Fixing them means feast >= 0.67 (numpy 2).

## Next steps

- Retrain from analyst feedback labels (`case_reviews`, latest label per transaction).
- Model registry link: **done 2026-11-12**. `python -m src.models.link_registry --alias
  production` writes `artifacts/baseline/registry.json` only if the registry version's
  `model.json` equals the served one; serving verifies it at startup and reports it at
  `GET /model` ([docs/api.md](docs/api.md)). Still open: the registry version per scored row and
  on the dashboard tile.
- An MLflow server with a shared artifact store, so retraining can run in the cluster.
- Rank drift features by their impact on the score instead of by gain.
- API throughput: run several uvicorn workers and re-measure at 5 clients. Size `DB_POOL_SIZE` so that workers x pool size fits the connection budget, and account for one model copy per worker.
- Next optimization: `entity_keys` (~17 ms) and `feast_push` (~16 ms) are about two-thirds of scoring time; `predict` is ~3 ms (docs/serving.md "Throughput benchmark", backlog run).

## How this project was built

I (Manojkumar M Hegde) own this project. I planned the milestones, set the
requirements and engineering rules, made the design decisions (for example the
model choice, the Feast feature source, the drift-trigger rules and the
reliability approach), reviewed every change and verified the results.

I used Claude Code (Anthropic) as an AI coding assistant to implement most of
the code, tests and documentation under my direction. Commits it helped write
carry a "Co-Authored-By: Claude" line.
