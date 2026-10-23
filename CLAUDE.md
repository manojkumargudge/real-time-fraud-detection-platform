# Project: Real-Time Financial Fraud Detection Platform

## Objective
Build a tested, reproducible fraud detection platform with transaction scoring,
graph-based fraud analysis, streaming ingestion, analyst review, monitoring,
and containerized deployment.

## Required stack
- Python, Pandas, XGBoost, PyTorch Geometric
- Apache Kafka, Feast, MLflow
- FastAPI, PostgreSQL
- Evidently AI, React
- Docker Compose, Kubernetes/Minikube
- pytest

## Engineering rules
- Inspect the repository and Git status before making changes.
- Implement one small, verifiable milestone at a time.
- Preserve working code and avoid unrelated rewrites.
- Use type hints, input validation, structured logging, and clear interfaces.
- Never hardcode or commit credentials, API keys, or personal data.
- Do not commit raw datasets, local environments, or generated model artifacts.
- Prevent target leakage and use appropriate validation for transaction data.
- Keep training and serving feature definitions consistent.
- Add meaningful tests for each feature and run them before reporting completion.
- Never claim tests, benchmarks, or deployments succeeded without verification.
- Ask before destructive operations or changes that may discard data.
- Avoid unnecessary dependencies, repeated explanations, and large file dumps.

## Workflow
Inspect -> plan briefly -> implement -> test -> report.

At each milestone, summarize changed files, actual test results, remaining risks,
and the next smallest task.

## Quality standard
Aim for production-style engineering, but document operational, security,
scalability, and compliance limitations honestly. Do not claim financial-sector
production readiness without appropriate evidence.

## Current project state (2026-10-23)

Read this first in a new session; details are in README.md and docs/. Verify anything that
matters (files, numbers, running services) before relying on it.

### Done (on `main`)
- Data: IEEE-CIS loader + EDA + data-quality report (`src/data/`, reports/). Fraud rate 3.499%.
- Features: leakage-safe point-in-time history features per `uid` / `card_id`
  (`src/features/history.py`, `entity.py`), 18-check audit (docs/audit_milestone2.md; check 18,
  D1 semantics, blocked -> D1 never served). Chronological split: validation =
  `TransactionDT >= 12,192,900` (last 20% of time, `split.validation_start`), 118,108 rows,
  3.44% fraud; training 472,432 rows.
- Model: XGBoost baseline, threshold = max F1 on a stage-1 chronological holdout of the training
  window, then refit on all training rows (`src/models/train.py`, `compare.train_variant`).
  - v1 `baseline-6026acdc8e2e` (`scale_pos_weight` 27.46): validation PR-AUC 0.2504, threshold 0.6721.
  - v2 `baseline-9fdad27ea3b0` (no class weight, seed 42): validation PR-AUC **0.2992**,
    threshold 0.1281, at threshold: 4.31% alerted, precision 0.3136, recall 0.3930. **Served.**
  - Imbalance study: `none` beats `scale_pos_weight` by +0.0418 mean PR-AUC over 3 seeds
    (seed spread 0.0134); confirmed on windows W1 (+0.0137, small) and W2 (+0.0430)
    (reports/imbalance_comparison.md). v1 vs v2 at 1/2/5% budgets: reports/model_v1_vs_v2.md.
- MLflow: local `sqlite:///mlflow.db` + `mlruns/` (gitignored, `MLFLOW_TRACKING_URI`
  overrides); experiment `fraud-xgboost`; registered model `fraud-xgboost`, aliases `champion`
  and `production` both on version 2. `src/models/tracking.py`.
- Serving: FastAPI `/health`, `/score` (`src/serving/`), Kafka consumer with retry policy, DLQ
  `transactions.dlq`, offsets committed only after the DB write or a confirmed DLQ write
  (`src/streaming/`). Idempotent PostgreSQL writes (`TransactionID` PK). Scorer order: dedupe ->
  features -> save -> Feast push (no push on duplicates).
- Feast (docs/feature_store.md): `feast[redis]==0.47.0`. Redis stores per-entity **state**,
  not features: `uid_state` (counts/sums split into "before latest timestamp" vs "at it",
  latest/previous timestamp, timestamps within 30 days) and `card_state` (last known value per
  change column, strictly earlier / at latest timestamp). Features =
  `entity_state.history_features(state, event)`; computed locally by default (an on-demand view
  exists for offline retrieval). **`FEATURE_SOURCE=feast` is the default**; `store` = old
  path (card history from PostgreSQL), kept behind the flag.
  - Why default: the store path has train/serve skew when the stream starts after training:
    96% of rows differ, PR-AUC 0.2992 -> 0.2533, 2% of alerts flip. Feast starts from the
    training history and equals batch features.
  - Cold start: `FEAST_REDIS=localhost:16379 python -m src.features.feast_store materialize
    --before validation` (batch transactions before the cut-off; ~80 s, 52,879 keys).
  - Feast gotchas handled: Redis drops writes whose timestamp is not newer (strictly increasing
    `*_write_ts`); historical retrieval collapses rows with equal keys+timestamp
    (`historical_features` splits them); online requests reject None (`"\x00"` sentinel);
    `FeatureStore` needs `repo_path` or it writes `registry.db` to the cwd; Windows paths must
    be relative to the repo dir.
- Latency (features + score, p50/p95): store 61.1/88.8 ms, Feast 107.5/179.0 ms (SDK overhead).
- CI/CD (.github/workflows/ci.yml): lint + mypy strict + unit tests; integration job with
  PostgreSQL 16, Kafka 3.8.1 (KRaft), Redis 7.4.1 service containers; on push to `main`, image
  pushed to `ghcr.io/manojkumargudge/real-time-fraud-detection-platform` (`<sha>`, `latest`).
  Nothing is deployed. Repo is public; `gh` is not logged in (watch runs via the public API).

### Demo stack (docker-compose.yml, project `fraud-platform`)
- Services: kafka (localhost:9092), postgres (15432), redis (16379), api (18000), consumer
  (currently scaled to 3 replicas; `docker compose up -d --no-deps --scale consumer=1 consumer`
  to go back). `.env` holds `POSTGRES_PASSWORD` (never print it). Host ports 5432/5433/8000 are
  taken by other projects (kasparro, healthcare-crm containers also run on this machine).
- Model bundle mounted read-only from `artifacts/baseline/` (= MLflow v2); v1 backup in
  `../fraud-backup-2026-10-22/served-bundle-v1`.
- Reset on 2026-10-23 (backup: `../fraud-backup-2026-10-22/demo-reset-2026-10-23`: pg_dump,
  Redis RDB, Kafka metadata + messages). Feast materialized from pre-cut-off history; the
  producer streams **validation-period transactions only**, in time order
  (`python -m src.streaming.producer --skip N --limit M`, refuses earlier or out-of-order
  events; messages keyed by `card_id`). Streamed so far: 10,000 events (skip 0-9,999), 464 alerts.
- Throughput: 7.3 events/s with 1 consumer, 12.0 with 3 (1.6x; per-event latency doubled,
  likely CPU-bound on one laptop, not profiled). Redis state verified against batch after the
  rebalance (300 uids / 216 cards, 0 mismatches).

### Commands
- Unit tests (no services; conftest blocks live services and forces `FEATURE_SOURCE=store`):
  `.venv/Scripts/python -m pytest --ignore=tests/test_integration.py` -- 396 passed on d375de9.
- Integration tests run **only** against the separate stack `docker-compose.test.yml`
  (project `fraud-platform-test`, ports 25432/29092/26379, test-only password `test-only`):
  `docker compose -f docker-compose.test.yml up -d --wait`, then
  `TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
  TEST_KAFKA_BOOTSTRAP=localhost:29092 TEST_REDIS=localhost:26379 python -m pytest
  tests/test_integration.py` -- 15 passed on d375de9. Never point them at the demo stack.
- Lint/types: `ruff check .`, `ruff format --check .`, `python -m mypy` (strict).
- Results files: docs/test-results-unit.txt, docs/test-results-integration.txt.

### Environment notes
- Windows 11, 16 GB RAM, no GPU; Python 3.11.9 in `.venv`; numpy 1.26.4, pandas 2.2.3,
  xgboost 2.1.4, pyarrow 18.0.0 (pinned by Feast), mlflow 3.15.2.
- Git Bash: `docker exec` paths need `MSYS_NO_PATHCONV=1`, which breaks Windows paths for
  other tools (use `cygpath -w`). Working tree uses CRLF (`core.autocrlf=true`).
- The laptop's Modern Standby suspends long runs (apparent hangs).

### Known limitations
- Threshold chosen on the stage-1 holdout but applied to the refit model; alert rate can
  drift. Fix: alert-budget threshold checked on post-deployment data.
- Feast: late events are approximate; concurrent writes to one entity can lose an update (fix:
  atomic per-entity update or single writer); a failed push is only logged (fix: catch-up
  replay per entity); Feast SDK adds ~50 ms per read.
- Consumer throughput does not scale linearly on one host.
- Not production-ready: single-node Kafka (RF=1), no auth/TLS, local MLflow, no monitoring.

### Next (approved)
- GNN (PyTorch Geometric) fraud-ring detection, batch only, on a separate branch; stack
  approved: torch 2.10.0+cpu, torch_geometric 2.8.1, pyg-lib 0.6.0+pt210cpu in
  `requirements-gnn.txt` (fallback torch 2.8.0+cpu, torch_geometric 2.8.1, pyg-lib
  0.4.0+pt28cpu). Stop at the day-1 checkpoint (graph, hub handling, leakage tests, first
  validation PR-AUC, memory and time) before XGBoost combination and rings.
- Still planned: Evidently drift + retraining trigger, analyst dashboard, Kubernetes (Minikube).
