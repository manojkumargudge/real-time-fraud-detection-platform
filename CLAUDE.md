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
- Dashboard tests must drive the real user path (type into widgets, navigate via
  open_case/switch_page), not inject session_state.
- Every comparison check (backup vs live, before vs after, A vs B) must assert both sides are
  non-empty and print row counts.
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

## Current project state (2026-10-30: scheduled retraining on the host; freeze otherwise final)

Read this first in a new session; details are in README.md and docs/. Verify anything that
matters (files, numbers, running services) before relying on it.

### 2026-10-30: scheduled retraining (freeze lifted for this change only)
- Windows Task Scheduler task `FraudDrift-Retrain` (every 6 h, **registered DISABLED**):
  `scripts/register_drift_task.ps1 [-Enable|-Disable|-Unregister]` ->
  `scripts/scheduled_drift.ps1` -> `run_drift` with retraining on, no `--force-retrain`.
  The script sets PYTHONUTF8/PYTHONUNBUFFERED, checks PostgreSQL (exit 2), builds the demo
  DATABASE_URL from .env (password never logged), logs to `logs/` (gitignored), and exits with
  the job's code. No src/ or configs/drift.toml change.
- Proof run started by Task Scheduler on copies (fraud_evidence on the test stack, mlflow.db
  copy with artifact locations redirected): docs/evidence/2026-10-30-scheduled-retrain/. Fired
  on R_emaildomain (days 170-181), challenger v8 in the copy only, champion kept (+0.0053,
  CI [-0.0160, +0.0254]); a second run reused v8. Two script bugs found by the proof run and
  fixed (cmd.exe quoting; -Enable ignoring a new -At).
- When enabled, each run writes a drift_reports row to the demo DB and a firing run registers a
  challenger in the real registry; the demo postgres must be running.
- Backups before: backups/*-2026-10-30-pre-auto-retrain.* (mlflow.db, mlruns zip, demo dump,
  artifact sha256).

### 2026-10-29 end of day
- **Feature freeze is final** (user decision 2026-10-29): docs fixes only from now on.
- `main` = merge of PR #27 `docs-final` on top of 305a75a (PR #26 gnn-account) and 6218477
  (PR #25 mlflow-coverage); CI green on all three. Verify with `git log --first-parent`.
- Demo DB: 118,108 / 5,093 / drift_reports 9 / fraud_rings 20 / case_reviews 6. Registry
  aliases champion v2, production v2, challenger v7. Demo stack stopped (`docker compose
  stop`, volumes kept); Minikube stopped. State: backups/state-2026-10-29-eod.txt.
- Tests: 598 unit passed with the GNN extras (local); CI: 573 passed + 10 skipped without them,
  41 integration passed (run #70).
- **Ring ranking is exploratory** (docs/gnn.md "Ring ranking check"): no in-sample GNN
  scores are used, but the reported top-20 rate (19.83% default, 31.54% account) reuses labels
  within a ring. Strict temporal check (rank on training blocks 1-2, rate on validation rows):
  7.96% (9 frauds) default, 8.14% (7 frauds) account, vs 2.89% in the same 203 rings.

### 2026-10-29: account node + MLflow coverage (freeze reopened for these two only)
- **GNN account node** (branch `gnn-account`, docs/gnn.md "Account node"): `--graph account` on
  `src.graph.stack` / `blend` / `rings` adds `account` (uid) nodes, tx-account and account-card
  edges; no node for `<NA>` uids; no direct account-device edge (needs edge-level time, which
  PyG cannot mix with node-level time). Default graph unchanged (edge_index sha256 test vs
  d8241d7). Results: GNN alone 0.2008; stacking 0.2798 vs 0.2935; blends worse than v2
  (transaction -0.0245 [-0.0354, -0.0140], entity -0.0144 [-0.0205, -0.0081]) -> not served.
  Outputs in `artifacts/gnn-account/` and reports/*_account.md; `artifacts/gnn/scores.parquet`
  and table `fraud_rings` untouched (rings.py refuses DB writes from non-default scores).
- **MLflow coverage** (PR #25, merge 6218477): `tracking.log_report_run` (experiment
  `fraud-evaluation`) at the end of compare / robustness / evaluation_study / robust_eval /
  leakage_audit `main()`; `scripts/mlflow_backfill.py` back-filled 19 runs into the real
  mlflow.db (8 fraud-evaluation, 9 fraud-drift = drift_reports 1-9, 2 fraud-gnn). Registry
  unchanged (champion 2, production 2, challenger 7). New drift checks still write only to
  PostgreSQL. Unit tests always use a throwaway MLflow store (conftest).
- Backup before: `backups/mlflow.db.bak-2026-10-29-pre-step3` (sha256 dc016af4…).

### Where things stand (2026-10-26)
- Analyst dashboard (Streamlit, docs/dashboard.md) on branch `dashboard`, merged via PR (see
  the session summary for the PR, CI run and merge commit). The demo database was migrated
  (alerts.updated_at/version, case_reviews), after a restore from
  backups/fraud-demo-2026-10-26-pre-dashboard.dump that removed the Step 3 test reviews
  (evidence of the lock bug they exposed: docs/evidence/2026-10-26-lock-bug/).
  `case_reviews` holds only real analyst reviews from then on.
  - **Layout** (`src/dashboard/`):
    - `workflow.py`: transitions and labels, pure;
    - `data.py`: **all** SQL, static and parameterised;
    - `db.py`: one connection per call;
    - `ui.py`: repository, 15 s read cache, labels;
    - `app.py` + `views/`: thin pages Overview, Alert queue, Case detail, Fraud rings, Drift.
    - Compose service `dashboard` on 127.0.0.1:8501 (`requirements-dashboard.txt`,
      streamlit 1.66.1 + pillow 13.0.0; never in the API/consumer image), reports/drift
      mounted read-only, `.streamlit/config.toml` toolbarMode viewer.
  - **Case workflow:** open -> investigating | confirmed_fraud | false_positive;
    investigating -> confirmed_fraud | false_positive; confirmed_fraud <-> false_positive
    (correction) and -> closed; closed is terminal. The label follows the target status
    (confirmed_fraud = fraud, false_positive = not_fraud, others none).
  - **Schema** (owned by src/serving/store.py, idempotent, under
    `pg_advisory_xact_lock(7291406001)`; the dashboard never runs DDL):
    - alerts gained `updated_at` and `version` (optimistic locking);
    - the status CHECK has 5 values, re-added only if it differs;
    - `case_reviews` (id, alert_id, transaction_id, from_status, to_status, label
      fraud/not_fraud/NULL, analyst, notes, alert_version, created_at) is append-only (a
      trigger rejects UPDATE/DELETE; TRUNCATE works, used only on the fraud_bench clone).
  - **Retraining input:** `src.dashboard.data.latest_labels(conn)` returns
    (transaction_id, label, reviewed_at) from the latest labelled review per transaction, so
    corrections win. The scoring path cannot overwrite analyst fields (insert-only, ON CONFLICT
    DO NOTHING; replay test).
  - **Status changes** are checked against the version the analyst saw (the save runs in
    the form callback; bf63be0 fixed a re-read on submit that defeated the lock).
  - **Run:** `docker compose up -d --build dashboard`; on the host:
    `DATABASE_URL=... PYTHONPATH=. .venv/Scripts/streamlit run src/dashboard/app.py`.
  - **Known limitations:**
    - no authentication (the analyst name is self-declared);
    - no connection pool (fine for a few analysts; add psycopg-pool before scaling);
    - single instance with a 15 s read cache;
    - no scoring latency or feature explanations (not stored);
    - the model tile shows the model id, not the MLflow version;
    - rings are training-period data.

### Earlier (end of 2026-10-25)
- `main` = `origin/main` at the merge of `k8s-full` (0207862) or later; CI is green through
  run #14. Merged on 2026-10-25, in order:
  - `consumer-crash-fix` (CI #10): retriable commit errors are retried, then left for
    redelivery instead of killing the consumer; chaos script `scripts/chaos_kafka_restart.py`.
  - `consumer-throughput` (CI #11): 13.1 -> 46.8 events/s on 3 consumers, scores unchanged;
    benchmark `scripts/bench_throughput.py`.
  - README "How this project was built" (CI #12, direct on main).
  - `k8s-smoke` (CI #13) and `k8s-full` (CI #14): Minikube deployment, docs/kubernetes.md.
- Earlier (2026-10-24): `security`, `drift`, `drift-rules`, `drift-reuse`.
- State at shutdown: backups/state-2026-10-25.txt (gitignored backups/, with a pg_dump and an
  mlflow.db copy). Both Compose stacks were stopped cleanly (`docker compose stop`, volumes
  kept). Restart commands are in that file.
- Demo stack: API and consumers rebuilt from main ca57b2c (crash fix + throughput) on
  2026-10-25; no restream since, row counts unchanged (118,108 / 5,093 / 9 / 20).
- Kubernetes: Minikube 1.39.0 (Docker driver, 4 CPUs / 4 GB, Kubernetes 1.37.0).
  - The Docker VM has 10 GB via `%UserProfile%\.wslconfig`.
  - The cluster is stopped and kept; namespace `fraud-k8s` deleted (manifests in `k8s/`).
  - The node keeps the raw-data copy at `/data/fraud-raw` (on the `minikube` volume).
  - Images `fraud-serving:k8s-local` and `fraud-monitoring:k8s-local` are local only.
  - Docker Desktop's built-in Kubernetes cannot be enabled from the CLI, so it was not used.
- Other projects' containers (kasparro, healthcare-crm) were stopped for memory and left
  stopped; kasparro has restart policy `always`, so it comes back with every Docker start.
- **Trigger rules and thresholds are final** (user decision 2026-10-24). Do not change them
  without being asked.
- MLflow registry `fraud-xgboost`: champion = v2, production = v2, challenger = v7.
  - v1 and v2 are the trained baselines; v7 is the real-data challenger (not promoted).
  - v3-v6 were identical challengers. They were deleted with `delete_model_version`; their
    runs are kept, and the SQL store keeps tombstone rows (`Deleted_Internal`), so version
    numbers are not reused.
  - v7 has no reuse tags, so the next real retrain trains once more and registers v8.
- Demo stack: the whole validation window is streamed (118,108 events). `drift_reports` holds
  ids 1-9.

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
  - Official GNN-alone validation PR-AUC: **0.2021** (run c233ecd0, seed 42, reproduced from
    clean main, scores bit-identical), vs XGBoost v2 0.2992. **The GNN loses**; not tuned.
    0.1766 (hubs dropped) / 0.1730 (hubs kept) are early exploratory, not reproduced.
  - Score blend with XGBoost (reports/gnn_blend.md): both variants worse on validation; not
    served.
  - Stacking (GNN score as XGBoost feature, forward-chained over 3 time blocks): mean 0.2827 vs
    0.2935 over 3 seeds (-0.0108, spread 0.0115) -> **no gain**, not kept; served model unchanged.
  - card_id is a coarse proxy: 77 values cover 41.9% of training transactions.
  - Fraud rings (`python -m src.graph.rings`): uid + device links only (never card_id), device
    values shared by <= 10 uids, uids with <= 3 devices and <= 1,000 transactions; 337 rings,
    largest 38 users (2.8%), 16 unscored (first time block); top-20 labelled fraud rate 19.83%
    vs 3.50% (weak evidence; exploratory, strict temporal check 7.96%, see above). reports/fraud_rings.md + table `fraud_rings` in the demo Postgres.
- CI/CD (.github/workflows/ci.yml): lint + mypy strict + unit tests; integration job with
  PostgreSQL 16, Kafka 3.8.1 (KRaft), Redis 7.4.1 service containers; on push to `main`, image
  pushed to `ghcr.io/manojkumargudge/real-time-fraud-detection-platform` (`<sha>`, `latest`).
  Nothing is deployed. Repo is public; `gh` is logged in as manojkumargudge (checked
  2026-10-28), so PRs and runs go through `gh pr` / `gh run`.
- Security job (docs/security.md): pip-audit (all requirements files; fails on any advisory not
  in `security/pip-audit-ignore.txt`, any severity), bandit `-ll` on `src/`, Trivy on the serving
  image (HIGH/CRITICAL with a fix, minus `.trivyignore`). Image push needs test + integration +
  security. Accepted risks expire 2027-01-31. New advisories can fail CI with no code change.
- MLflow 3.17.2: the local `mlflow.db` was migrated (`mlflow db upgrade`, one-way) on
  2026-10-24; pre-migration backup `mlflow.db.bak-2026-10-24` (gitignored). Older mlflow cannot
  read it.
- Drift monitoring (docs/monitoring.md, `src/monitoring/`, `configs/drift.toml`, Evidently
  0.7.23): `python -m src.monitoring.run_drift [--window-start-day D --window-end-day D]
  [--no-retrain] [--synthetic-amount-factor F]`.
  - now = max `transaction_dt` in scored_transactions; label cutoff = now - 30 days (inclusive).
  - Reference = last 30 days of the champion's training window. Its scores come from the
    champion recipe refit on earlier rows (out-of-sample).
  - Fires on drifted-feature share > 0.5, OR PSI >= 0.2, OR delayed PR-AUC < champion - 0.05,
    OR any of the champion's top 5 features by gain drifts (added 2026-10-24 after runs 1-3;
    the top 5 are read from the booster: card3, card6, R_emaildomain, addr2, ProductCD).
  - `--force-retrain` runs retrain + promotion even if nothing fired (`forced = true`).
  - The delayed-label window is the same in every run, by design: days 146-152 (day windows in
    docs are written with the last day included; `--window-end-day` is exclusive).
  - When it fires: challenger trained on usable labels before the holdout (last 7 days before
    the cutoff), alias `challenger`. `champion` moves only if the paired day-block bootstrap CI
    lower bound (1,000 resamples, seed 42) is > 0.01. `production` is never moved.
  - Overlap of a training window with the holdout fails the job. Results go to PostgreSQL
    `drift_reports` and `reports/drift/*.html` (gitignored).
  - Retraining runs on the host only (Windows file:/// artifact paths in the SQLite registry).
    Compose `--profile monitoring` service `drift` = checks only, image built with
    `REQUIREMENTS=requirements-monitoring.txt`.
  - Demo 2026-10-24 (drift_reports ids 1-3): none fired.
    - Run 1, days 141-144: share 0.179, PSI 0.0034.
    - Run 2, days 170-182: share 0.286, PSI 0.0027.
    - Run 3, synthetic TransactionAmt x2: share 0.393, PSI 0.0097.
    - Delayed PR-AUC 0.3238 in all runs (days 146-152, 617 frauds).
    - Retraining and promotion never ran on the real registry.
  - Second demo (ids 5-9, top-feature rule, 2026-10-24, the first demo's windows 141-144 and
    170-182 again): all of these runs fired.
    - Run 1 fired on card3 (Wasserstein 0.1041); runs 2-4 fired on R_emaildomain. The synthetic
      2x TransactionAmt shift is still not what fires (TransactionAmt is not a top-5 feature).
    - Challenger vs champion on the holdout: 0.3290 vs 0.3238, CI [-0.0160, +0.0254], kept
      champion.
    - Registry then: challenger v3-v7, all identical. v3-v6 have since been deleted (see
      "Where things stand").
    - Row 8 = forced run stored before the `forced` fix; row 9 is the corrected one.
  - Evidence run 2026-10-27 (copies only, docs/evidence/2026-10-27-drift-trigger/): the
    default window (days 176-182, id 10) did NOT fire. Days 170-181, chosen after that, fired
    on R_emaildomain without `--force-retrain` (id 11). Challenger v8 exists only in the
    mlflow.db copy; champion kept (delta +0.0053, CI [-0.0160, +0.0254]). Real registry
    unchanged.
  - Velocity features verified (naive recount and served scores match). The late-window drift
    is a heavy tail of bursty uids.
  - The two stray runs `challenger-4750731` were soft-deleted on 2026-10-24 (`delete_run`,
    restorable).
  - Challenger reuse: if the current challenger's tags `train_end_dt` and `train_data_sha256`
    (sorted IDs + labels, window, seed, imbalance) match the planned training rows, retraining
    is skipped and it logs "reused challenger vN".
  - Design trade-off (README): the trigger is cheap and sensitive. Retraining takes about 73 s,
    and the bootstrap promotion gate is what protects production.

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
  events (days 141-182, last day included), 5,093 alerts. Further `--skip` values have nothing left to send.
  Rest-of-validation stream (2026-10-24): 8.05 events/s, scorer p50/p95/p99 207/549/843 ms
  (docs/serving.md). Consumers crashed twice on retriable commit errors (`NOT_COORDINATOR`);
  no loss or duplicates. Fixed 2026-10-25 (`consumer-crash-fix`, see Known limitations).
- Throughput (2026-10-25, `consumer-throughput`, docs/serving.md "Throughput benchmark"):
  **46.8 events/s on 3 consumers** (was 13.1 on the same benchmark), scorer p50 48 ms (was
  203). Causes:
  - XGBoost used all 12 cores per one-row predict, about 10 ms each, so 3 consumers
    oversubscribed the CPU;
  - the Feast model row recomputed the whole batch frame;
  - the Feast SDK rebuilt every feature view per call (~30 ms per read).
  Fixed without changing values: 4,000 replayed scores were bit-identical to the demo's.
  Remaining: Feast push 16 ms, event frame 17 ms. Older figures: 7.3 events/s with 1 consumer,
  12.0 with 3. Redis state verified against batch after the
  rebalance (300 uids / 216 cards, 0 mismatches).

### Commands
- Unit tests (no services; conftest blocks live services and forces `FEATURE_SOURCE=store`):
  `.venv/Scripts/python -m pytest --ignore=tests/test_integration.py` -- **598 passed** with the
  GNN stack installed (2026-10-29; 550 on 2026-10-26). Without it (CI's unit job),
  `tests/test_graph.py` skips because torch_geometric is missing; the ring tests need no torch.
- Integration tests run **only** against the separate stack `docker-compose.test.yml`
  (project `fraud-platform-test`, ports 25432/29092/26379, test-only password `test-only`):
  `docker compose -f docker-compose.test.yml up -d --wait`, then
  `TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
  TEST_KAFKA_BOOTSTRAP=localhost:29092 TEST_REDIS=localhost:26379 python -m pytest
  tests/test_integration.py` -- 41 passed (CI run #70, 2026-10-29; 30 on 2026-10-26). Never point them at the demo stack.
- GNN: `python -m src.graph.train`, `python -m src.graph.stack --degree-limit 1000 --epochs 7`,
  `DATABASE_URL=... python -m src.graph.rings` (needs requirements-gnn.txt; 10-45 min on CPU).
- Lint/types: `ruff check .`, `ruff format --check .`, `python -m mypy` (strict; src, tests, scripts).
- Chaos test (test stack only): `python -m scripts.chaos_kafka_restart [--mode restart|kill|pause]`.
- Dashboard: `docker compose up -d --build dashboard` -> http://localhost:8501 (docs/dashboard.md).
- Throughput benchmark (clone `fraud_bench` of the demo DB, own Redis/topic/group;
  docker-compose.bench.yml): `python -m scripts.bench_throughput setup|run|reset|restart|teardown`.
  Consumer env `STAGE_TIMINGS=1` logs per-stage ms per message.
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
- Serving reads/writes Feast through `FeatureStore._get_provider()` (private; feast 0.47.0 is
  pinned; tested equal to the SDK path, `FeastFeatures(direct_online=False)` keeps the SDK).
- Consumer commits (fixed 2026-10-25): retriable commit errors (`NOT_COORDINATOR`,
  `COORDINATOR_LOAD_IN_PROGRESS`, `COORDINATOR_NOT_AVAILABLE`, `REQUEST_TIMED_OUT`,
  `_TIMED_OUT`, `retriable()`) are retried 0.2/0.5/1 s, then counted as `commit_rejected` and
  left for redelivery; ERROR every 30 s while failing > 30 s (`consecutive_commit_failures`).
  Chaos test `python -m scripts.chaos_kafka_restart --mode restart|kill|pause` (test stack
  only): no loss, no duplicates, consumers alive. But no run produced `NOT_COORDINATOR` on a
  commit (librdkafka waits for the coordinator), so that path is unit-tested only.
- Not production-ready: single-node Kafka (RF=1), no auth/TLS, local MLflow; the drift job with
  retraining is scheduled only on the host (Task Scheduler, while the laptop is awake).
- Drift trigger: the top-5-by-gain rule was added on 2026-10-24 after the first runs did not
  fire. It is not calibrated on drift-free periods, and there is no minimum sample size (the
  2026-10-25 Kubernetes run fired on 30 rows). The default window did not fire on 2026-10-27
  (docs/evidence/2026-10-27-drift-trigger/).
- Dashboard: no authentication (self-declared analyst name), no connection pool (add
  psycopg-pool before scaling), single instance with a 15 s read cache (docs/dashboard.md).
- Served model verified byte-identical to MLflow registry v2 (run
  71c4f1bd820548c984341a06e51e33fc; model.json sha256 9fdad27ea3b0…, metadata.json sha256
  a65a6dfcb68b…; checked 2026-10-27). Serving does not read the registry at runtime; design for
  a link file + GET /model is in docs/next-steps/model-registry-link.md.
- Resolved 2026-10-28: champion v2 is tagged git_dirty=true (git_commit 7754fd4) but was rebuilt
  byte for byte from a fresh clone of main (`python -m src.models.train --feature-source pandas
  --imbalance none`): same model.json sha256 9fdad27ea3b0…, validation PR-AUC
  0.29923830601075374, threshold 0.12806208431720734. The CLI default `--imbalance
  scale_pos_weight` reproduces v1 exactly. Still open: a guard against dirty-tree training runs.

### Next (after the freeze; pick with the user; nothing is in progress)
- Feedback-label retraining: retrain from analyst labels (`src.dashboard.data.latest_labels`,
  latest labelled review per transaction). Not started.
- Model registry link: proposal only, cut as code on 2026-10-27; see
  docs/next-steps/model-registry-link.md (link file + GET /model; estimate 60-90 min).
- Dashboard on Kubernetes (manifest), planned 2026-10-27.
- Kubernetes beyond the local Minikube deployment (docs/kubernetes.md "Not production"):
  replication, Ingress/TLS, a secrets manager, an MLflow server for in-cluster retraining.
  Retraining would need an MLflow server with a shared artifact store first (host-only today).
- Idea, not built and not approved: rank features for drift by their impact on the score
  instead of by gain (both existing rules missed the 2x TransactionAmt shift).
- Windows gotcha: venvs under the long scratchpad path break psycopg's DLL load ("filename or
  extension is too long"); use a short path such as %TEMP%\civ for throwaway venvs.
