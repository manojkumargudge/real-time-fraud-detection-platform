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

## Current project state (2026-10-23, after the GNN merge)

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
- GNN (docs/gnn.md, `src/graph/`, batch only, never in `/score`): heterogeneous GraphSAGE on a
  transaction-entity graph (card_id, DeviceInfo, P/R_emaildomain, addr1; hub values with more
  than 1,000 training transactions dropped), PyG temporal neighbor sampling with
  `disjoint=True` and seed time `TransactionDT - 1` (strictly earlier, per seed). Install with
  `pip install -r requirements-gnn.txt`: **torch 2.8.0+cpu**, torch_geometric 2.8.1, pyg-lib
  0.4.0+pt28cpu. torch 2.10.0 was tried first but fails on Windows (WinError 1114 in
  `c10.dll`) whenever pandas is imported first, because pandas ships an older `msvcp140.dll`.
  - Results (validation PR-AUC, seed 42): hubs dropped 0.1766, hubs kept 0.1730, vs XGBoost v2
    0.2992. **The GNN loses**; not tuned.
  - Stacking (GNN score as XGBoost feature, forward-chained over 3 time blocks): mean 0.2827 vs
    0.2935 over 3 seeds (-0.0108, spread 0.0115) -> **no gain**, not kept; served model unchanged.
  - card_id is a coarse proxy: 77 values cover 41.9% of training transactions.
  - Fraud rings (`python -m src.graph.rings`): uid + device links only (never card_id), device
    values shared by <= 10 uids, uids with <= 3 devices and <= 1,000 transactions; 337 rings,
    largest 38 users (2.8%), 16 unscored (first time block); top-20 labelled fraud rate 19.83%
    vs 3.50% (weak evidence). reports/fraud_rings.md + table `fraud_rings` in the demo Postgres.
- CI/CD (.github/workflows/ci.yml): lint + mypy strict + unit tests; integration job with
  PostgreSQL 16, Kafka 3.8.1 (KRaft), Redis 7.4.1 service containers; on push to `main`, image
  pushed to `ghcr.io/manojkumargudge/real-time-fraud-detection-platform` (`<sha>`, `latest`).
  Nothing is deployed. Repo is public; `gh` is not logged in (watch runs via the public API).
- Security job (README "Security"): pip-audit (all requirements files; fails on any advisory not
  in `security/pip-audit-ignore.txt`, any severity), bandit `-ll` on `src/`, Trivy on the serving
  image (HIGH/CRITICAL with a fix, minus `.trivyignore`). Image push needs test + integration +
  security. Accepted risks expire 2027-01-31. New advisories can fail CI with no code change.
- MLflow 3.17.2: the local `mlflow.db` was migrated (`mlflow db upgrade`, one-way) on
  2026-10-24; pre-migration backup `mlflow.db.bak-2026-10-24` (gitignored). Older mlflow cannot
  read it.
- Drift monitoring (README "Monitoring", `src/monitoring/`, `configs/drift.toml`, Evidently
  0.7.23): `python -m src.monitoring.run_drift [--window-start-day D --window-end-day D]
  [--no-retrain] [--synthetic-amount-factor F]`.
  - now = max `transaction_dt` in scored_transactions; label cutoff = now - 30 days (inclusive).
  - Reference = last 30 days of the champion's training window. Its scores come from the
    champion recipe refit on earlier rows (out-of-sample).
  - Fires on drifted-feature share > 0.5, OR PSI >= 0.2, OR delayed PR-AUC < champion - 0.05,
    OR any of the champion's top 5 features by gain drifts (added 2026-10-24 after runs 1-3;
    the top 5 are read from the booster: card3, card6, R_emaildomain, addr2, ProductCD).
  - `--force-retrain` runs retrain + promotion even if nothing fired (`forced = true`).
  - The delayed-label window is the same in every run, by design: days 146-153.
  - When it fires: challenger trained on usable labels before the holdout (last 7 days before
    the cutoff), alias `challenger`. `champion` moves only if the paired day-block bootstrap CI
    lower bound (1,000 resamples, seed 42) is > 0.01. `production` is never moved.
  - Overlap of a training window with the holdout fails the job. Results go to PostgreSQL
    `drift_reports` and `reports/drift/*.html` (gitignored).
  - Retraining runs on the host only (Windows file:/// artifact paths in the SQLite registry).
    Compose `--profile monitoring` service `drift` = checks only, image built with
    `REQUIREMENTS=requirements-monitoring.txt`.
  - Demo 2026-10-24 (drift_reports ids 1-3): none fired.
    - Run 1, days 141-145: share 0.179, PSI 0.0034.
    - Run 2, days 170-183: share 0.286, PSI 0.0027.
    - Run 3, synthetic TransactionAmt x2: share 0.393, PSI 0.0097.
    - Delayed PR-AUC 0.3238 in all runs (days 146-153, 617 frauds).
    - Retraining and promotion never ran on the real registry.
  - Second demo (ids 5-9, top-feature rule): every run fired.
    - Run 1 fired on card3 (Wasserstein 0.1041); runs 2-4 fired on R_emaildomain. The synthetic
      2x TransactionAmt shift is still not what fires (TransactionAmt is not a top-5 feature).
    - Challenger vs champion on the holdout: 0.3290 vs 0.3238, CI [-0.0160, +0.0254], kept
      champion.
    - Registry: champion = 2, production = 2, challenger = 7 (v3-v7 are identical challengers).
    - Row 8 = forced run stored before the `forced` fix; row 9 is the corrected one.
  - Velocity features verified (naive recount and served scores match). The late-window drift
    is a heavy tail of bursty uids.
  - The two stray runs `challenger-4750731` were soft-deleted on 2026-10-24 (`delete_run`,
    restorable).

### Demo stack (docker-compose.yml, project `fraud-platform`)
- Services: kafka (localhost:9092), postgres (15432), redis (16379), api (18000), consumer
  (3 replicas by default via `deploy.replicas` in docker-compose.yml, one per `transactions`
  partition; `--scale` is not persisted, so it was lost once the containers were removed). `.env` holds `POSTGRES_PASSWORD` (never print it). Host ports 5432/5433/8000 are
  taken by other projects (kasparro, healthcare-crm containers also run on this machine).
- Model bundle mounted read-only from `artifacts/baseline/` (= MLflow v2); v1 backup in
  `../fraud-backup-2026-10-22/served-bundle-v1`.
- Reset on 2026-10-23 (backup: `../fraud-backup-2026-10-22/demo-reset-2026-10-23`: pg_dump,
  Redis RDB, Kafka metadata + messages). Feast materialized from pre-cut-off history; the
  producer streams **validation-period transactions only**, in time order
  (`python -m src.streaming.producer --skip N --limit M`, refuses earlier or out-of-order
  events; messages keyed by `card_id`). **The whole validation window is streamed**: 118,108
  events (days 141.1-183.0), 5,093 alerts. Further `--skip` values have nothing left to send.
  Rest-of-validation stream (2026-10-24): 8.05 events/s, scorer p50/p95/p99 207/549/843 ms
  (docs/serving.md). Consumers crashed twice on retriable commit errors (`NOT_COORDINATOR`;
  pre-existing bug, not fixed); no loss or duplicates.
- Throughput: 7.3 events/s with 1 consumer, 12.0 with 3 (1.6x; per-event latency doubled,
  likely CPU-bound on one laptop, not profiled). Redis state verified against batch after the
  rebalance (300 uids / 216 cards, 0 mismatches).

### Commands
- Unit tests (no services; conftest blocks live services and forces `FEATURE_SOURCE=store`):
  `.venv/Scripts/python -m pytest --ignore=tests/test_integration.py` -- **466 passed** with the
  GNN stack installed; **456 passed, 1 skipped** without it (2026-10-24, drift-rules) (as in CI: `tests/test_graph.py`
  skips when torch_geometric is missing; the ring tests need no torch).
- Integration tests run **only** against the separate stack `docker-compose.test.yml`
  (project `fraud-platform-test`, ports 25432/29092/26379, test-only password `test-only`):
  `docker compose -f docker-compose.test.yml up -d --wait`, then
  `TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
  TEST_KAFKA_BOOTSTRAP=localhost:29092 TEST_REDIS=localhost:26379 python -m pytest
  tests/test_integration.py` -- 17 passed (2026-10-24, drift-rules). Never point them at the demo stack.
- GNN: `python -m src.graph.train`, `python -m src.graph.stack --degree-limit 1000 --epochs 7`,
  `DATABASE_URL=... python -m src.graph.rings` (needs requirements-gnn.txt; 10-45 min on CPU).
- Lint/types: `ruff check .`, `ruff format --check .`, `python -m mypy` (strict).
- Results files: docs/test-results-unit.txt, docs/test-results-integration.txt.

### Environment notes
- Windows 11, 16 GB RAM, no GPU; Python 3.11.9 in `.venv`; numpy 1.26.4, pandas 2.2.3,
  xgboost 2.1.4, pyarrow 18.0.0 (pinned by Feast), mlflow 3.17.2, fastapi 0.143.0, starlette 1.7.0,
  cryptography 50.0.2, pytest 9.1.1; serving image base `python:3.11.16-slim-bookworm`.
- Scanners: throwaway venv `%TEMP%\sec` (pip-audit 2.10.1, bandit 1.9.4); Trivy via
  `aquasec/trivy:0.75.0` with Docker volume `trivy-cache`. CI-like venv without the GNN stack:
  `%TEMP%\civ`.
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
- Consumer: a commit failing with `NOT_COORDINATOR` / `COORDINATOR_LOAD_IN_PROGRESS` crashes it
  (only rebalance errors are tolerated); Docker restarts it and the events are redelivered.
- Not production-ready: single-node Kafka (RF=1), no auth/TLS, local MLflow, drift job not
  scheduled.

### Next
- Planned: analyst dashboard (can read tables `fraud_rings`, `drift_reports`), Kubernetes
  (Minikube; drift job as a CronJob), consumer commit-error fix.
- Open decision: the top-feature rule fires on every window (noisy) and missed the
  TransactionAmt shift. Keep it, raise its threshold or widen it (user's call).
- Windows gotcha: venvs under the long scratchpad path break psycopg's DLL load ("filename or
  extension is too long"); use a short path such as %TEMP%\civ for throwaway venvs.
