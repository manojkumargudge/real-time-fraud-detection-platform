# Brief vs evidence (requirement audit, 2026-10-28)

Read-only audit of `main` 5be7ddf against the project brief. Nothing was run or changed except
this file. Status: **Yes** = done and evidenced; **Partial** = done with a material deviation or
missing piece; **No** = not done. Every gap is stated in one sentence.

CI on `main`: runs #30, #32, #34 and #36 (5be7ddf) green (lint + mypy strict + unit, integration
with PostgreSQL / Kafka / Redis, security scans, image push).

## Step 1: dataset, EDA, class imbalance

| Item | Status | Evidence | Gap |
|---|---|---|---|
| IEEE-CIS loader with validation | Yes | `src/data/load.py`; data/README.md; `tests/test_load.py` | |
| EDA + data quality | Yes | `src/data/eda.py`, `src/data/quality.py`; reports/eda_summary.md, reports/data_quality.md, notebooks/01_eda.ipynb | |
| Class imbalance stated | Yes | reports/eda_summary.md §2: fraud rate **3.499%** (validation 3.44%), daily 1.10%-6.99% | The brief says "<1%"; the real rate is about 3.5% (README says so). |

## Step 2: features and Feast

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Velocity | Yes | `uid_prior_txn_count`, `uid_txn_count_{1h,24h,7d,30d}`, `uid_seconds_since_prev_txn` (`src/features/history.py` `compute_history_features`; docs/features.md §3) | |
| Amount vs user average | Yes | `uid_prior_amt_mean`, `uid_amt_to_prior_mean` (same function) | |
| Device / location change | Yes | `card_{deviceinfo,devicetype,addr1,addr2}_changed` (`DEFAULT_CHANGE_COLUMNS`) | "User" is the proxy `uid` = card_id + addr1; IEEE-CIS has no account ID. |
| Point-in-time / leakage | Yes | docs/audit_milestone2.md (18 checks); `tests/test_features.py` | |
| Feast used at **serving** | Yes | The consumer and `POST /score` both call `Scorer.score_event` (`src/serving/scorer.py`), which with `FEATURE_SOURCE=feast` (the default, `src/serving/config.py`) calls `FeastFeatures.read` (`src/features/feast_store.py`) against the Redis online store at scoring time, then `push` after the save. | Feast stores per-entity **state**; the features are computed from it in Python (`entity_state.history_features`), and serving reads Redis through a private Feast API (`FeatureStore._get_provider()`). |
| Feast used at **training** | Yes | `src/features/feast_training.py` builds the training matrix with `get_historical_features` from the Feast offline store (PR #12); the training CLI defaults to it (`python -m src.models.train --feature-source feast`). docs/feature_store.md "Training from the Feast offline store": proven identical to the pandas matrix v2 was trained on (472,432 training / 118,108 validation rows, three sha256 hashes equal on both sides). | Automatic retraining by the drift job uses the pandas path (`build_model_frame` in `src/monitoring/run_drift.py`), which is proven identical; Feast takes about 38 min vs 2 s. |
| Train/serve parity tests | Yes | `tests/test_feast_store.py`: `test_point_in_time_retrieval_equals_batch_features`, `test_point_in_time_retrieval_equals_batch_on_real_cards`, `test_stream_replay_with_batch_cold_start` (Feast features == batch training features); `tests/test_integration.py::test_feast_redis_replay_matches_the_store_path` (real Redis); `tests/test_entity_state.py::test_in_order_replay_equals_batch`. The demo showed the old store path skews 96% of rows (PR-AUC 0.2992 -> 0.2533, docs/results.md "Why Feast is the default"). | |

## Step 3: XGBoost, imbalance, PR-AUC, MLflow

| Item | Status | Evidence | Gap |
|---|---|---|---|
| XGBoost | Yes | `src/models/train.py`, `compare.train_variant`; served v2 `baseline-9fdad27ea3b0` | |
| Class weights or SMOTE | Partial | Class weights implemented and compared: v1 was trained with `scale_pos_weight` 27.46 (still the CLI default, `compare.IMBALANCE_METHODS`); over 3 seeds and two later time windows no weight scored higher (mean validation PR-AUC 0.2935 vs 0.2517, +0.0418; W1 +0.0137, W2 +0.0430; reports/imbalance_comparison.md), so v2 is served without weights (`--imbalance none`; automatic retraining uses `none` too, configs/drift.toml). | SMOTE was not used: it creates synthetic transactions with no real history, which breaks the history features. The served model deliberately uses no class weight (a documented, measured deviation). |
| PR-AUC | Yes | v2 validation PR-AUC **0.2992** (threshold 0.1281: 4.31% alerted, precision 0.3136, recall 0.3930); v1 0.2504; reports/model_v1_vs_v2.md | |
| All runs in MLflow | Yes (drift: back-filled) | Training: `src.models.train`, `imbalance_comparison`, drift challengers (`fraud-xgboost`). Evaluation: `compare`, `robustness`, `evaluation_study`, `robust_eval`, `leakage_audit` log their report at the end of `main()` (`tracking.log_report_run`, experiment `fraud-evaluation`); GNN: `src.graph.train` / `stack` / `blend` (`fraud-gnn`). Back-filled on 2026-10-29 by `scripts/mlflow_backfill.py`: 8 evaluation reports incl. v1 vs v2 and the imbalance decision, `drift_reports` 1-9 (`fraud-drift`), the GNN stacking (c233ecd0, scores sha256 `041078eb4343…`) and rings reports. Registry before/after: aliases and model versions unchanged (README "Experiment tracking"). | New drift checks (incl. the Minikube CronJob) write only to PostgreSQL, not MLflow; `version_comparison` reads the registry but logs nothing itself (its report is back-filled); challenger v8 exists only in an mlflow.db copy (evidence run). |
| Reproducibility of the champion | Yes | v2 (tagged `git_commit 7754fd4`, `git_dirty=true`) was rebuilt byte for byte from a fresh clone of main on 2026-10-28 (`python -m src.models.train --feature-source pandas --imbalance none`): same model.json sha256 `9fdad27ea3b0…`, validation PR-AUC 0.29923830601075374, threshold 0.12806208431720734; the uncommitted changes did not affect training (README "Setup from a fresh clone"). | The run is still tagged `git_dirty=true`; a guard against dirty-tree training runs is not built. |

## Step 4: graph, GNN, rings, blend

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Graph of accounts, devices, cards | Yes (account graph optional; no gain) | `src/graph/build.py` `ENTITY_COLUMNS`. Default GNN graph nodes: `tx`, `card` (card1-card6), `device` (`DeviceInfo`), `p_email`, `r_email`, `addr1`. `--graph account` adds an `account` (uid) node with tx-account and account-card edges; accounts link to devices through transactions. Evaluated 2026-10-29 (docs/gnn.md "Account node"): GNN alone 0.2008 vs 0.2021, blends still worse than XGBoost v2 (CI below zero), not served. Default graph verified unchanged (edge_index sha256 per edge type). | No direct account-device edge (needs edge-level time, which would change the existing graph's sampling); no account node for uids with a missing addr1; `DeviceInfo` is a device model / OS string (`Windows`, `iOS Device`), not a device ID. |
| GNN (PyG) | Yes | Hetero GraphSAGE with temporal sampling (`src/graph/model.py`, `data.py`, `train.py`); official validation PR-AUC **0.2021** (MLflow run c233ecd0, reproduced, scores bit-identical) vs XGBoost 0.2992; leakage tests in `tests/test_graph.py` | It loses to XGBoost by about 0.10 PR-AUC; not tuned. |
| Fraud rings | Yes | `src/graph/rings.py` `ring_components`: union-find connected components of the bipartite **uid - DeviceInfo** graph; devices shared by 2-10 uids only, hub uids (> 3 devices or > 1,000 tx) excluded; card_id never a link. `summarize_rings`, `write_table` -> table `fraud_rings`. **The GNN ranks rings (mean score), it does not build them.** reports/fraud_rings.md: 337 rings, 1,378 users. | Rings rest on a weak link (device model strings) and hand-picked limits. A missing addr1 becomes part of the uid (`<NA>`), so a card's transactions without an address count as one customer: of the top 20 rings in `fraud_rings`, 16 contain such a uid and 9 consist only of them (45 of 73 members). |
| Ring quality vs base rate | Partial (exploratory) | Measured: `summarize_rings` `fraud_rate` = labelled fraud rate of the ring's transactions. reports/fraud_rings.md: top-20 ranked rings 19.83%, all rings 4.06%, overall 3.50% (account graph: 31.54%). Strict temporal check (docs/gnn.md "Ring ranking check": rank on training blocks 1-2, rate on validation rows only): **7.96% (9 frauds) vs 2.89%** in the same 203 rings; account graph 8.14% (7 frauds). No in-sample scores are used. | The reported rate reuses labels within a ring (all ring rows, incl. unscored block 0 and rows whose labels trained the scoring GNN); measured per transaction only, not per ring member; the clean check rests on 7-9 frauds. |
| GNN as XGBoost feature | Yes (measured, not kept) | reports/gnn_stacking.md: mean 0.2827 vs 0.2935 over 3 seeds (-0.0108) | |
| Blend | Yes (measured, not served) | `src/graph/blend.py`; reports/gnn_blend.md; PR #8. transaction w=0.50: AP 0.2749 vs 0.2992, CI [-0.0346, -0.0137]; entity w=0.45: 0.2797 vs 0.2992, CI [-0.0263, -0.0118]. Both worse; not served. | |

## Step 5: streaming and scoring

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Kafka producer replay | Yes | `src/streaming/producer.py` (validation-period only, time order enforced, labels and D1 never sent); 118,108 events streamed in the demo | |
| Consumer fetches features, returns a score | Yes | `src/streaming/consumer.py` -> `processor.process_message` -> `Scorer.score_event` (Feast read, XGBoost, PostgreSQL write, Feast push); DLQ, retry, commit-after-write (`tests/test_streaming_reliability.py`, chaos script `scripts/chaos_kafka_restart.py`) | The consumer is a Kafka consumer, not a FastAPI service; FastAPI serves the same scorer synchronously at `POST /score` (`src/serving/api.py`). |
| Performance | Yes | 46.8 events/s on 3 consumers unthrottled (scorer p50 48 ms in that 1,000-event backlog run; 41 ms p50 at a steady 5 events/s) (docs/results.md "Consumer throughput"); Feast features+score p50/p95 107.5 / 179.0 ms | Local single-node numbers only. |

## Step 6: drift and retraining

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Evidently drift reports | Yes | `src/monitoring/drift.py`, `run_drift.py`, `configs/drift.toml` (Evidently 0.7.23); rows in `drift_reports`, HTML in reports/drift/ (gitignored) | |
| Retraining trigger + promotion gate | Yes | `src/monitoring/rules.py`, `retrain.py`; paired day-block bootstrap gate; `tests/test_monitoring.py` | |
| Fired without `--force` | Yes | docs/evidence/2026-10-27-drift-trigger/ (PR #9, #10): row 10, default window days 176-182, **did not fire**; row 11, days 170-181 (chosen after that; days are last day included), fired on R_emaildomain, `forced = false`; challenger v8 (copy DB only), champion kept: delta +0.0053, CI [-0.0160, +0.0254] | The firing window was chosen after the default window did not fire. |
| Automatic | Partial | The trigger is automatic: when a rule fires, the job trains a challenger and runs the promotion gate with no manual step (row 11 fired and trained v8 without `--force-retrain`, `forced = false`, on a copy of mlflow.db; docs/evidence/2026-10-27-drift-trigger/). | What is manual is running the job (`python -m src.monitoring.run_drift`) on the host. The k8s CronJob (every 6 h) runs checks only (`--no-retrain`) because MLflow is a local SQLite file on the host (docs/kubernetes.md "Drift CronJob"). |
| Known limitations | Yes | CLAUDE.md Known limitations: top-5-by-gain rule added after the first runs did not fire, not calibrated on drift-free periods, no minimum sample size (the k8s run fired on 30 rows) | |

## Step 7: dashboard and Kubernetes

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Streamlit dashboard: alerts, case status | Yes | `src/dashboard/` (Overview, Alert queue, Case detail, Fraud rings, Drift); workflow `workflow.py`, optimistic locking; `tests/test_dashboard.py` drives the real user path; docs/dashboard.md; screenshots of every page in [docs/screenshots/](screenshots/) (01-overview to 08-drift, demo database, 2026-10-28) | No authentication (self-declared analyst name). |
| Feedback labels | Partial | Append-only `case_reviews`; `src.dashboard.data.latest_labels` returns the latest label per transaction | Labels are captured but never used: feedback-label retraining is not built (CLAUDE.md "Next"). |
| Kubernetes (Minikube) | Yes | `k8s/` (kustomization), docs/kubernetes.md "Results" (2026-10-25: 30 events, scores identical to host batch, liveness restart verified) | Not production: single replicas, no Ingress/TLS, no secrets manager. |

**What runs where:**

| In Minikube (namespace `fraud-k8s`) | On the host only |
|---|---|
| Kafka (single-node KRaft), PostgreSQL, Redis (Feast online state) | MLflow (local SQLite `mlflow.db` + `mlruns/`, no server) |
| API (FastAPI), consumer x3 | Training, retraining and champion promotion |
| Dashboard (reads the cluster's 20-event DB) | GNN training, stacking, blend, fraud rings |
| Jobs: Feast materialize, producer (one-off) | EDA, reports, evaluation studies |
| CronJob `drift` (checks only, `--no-retrain`) | The full demo (Docker Compose: 118,108 events, 5,093 alerts) |

## Deliverables

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Demo | Partial | Docker Compose demo stack with recorded counts (docs/results.md "Demo: unseen validation data"; 118,108 / 5,093); k8s run in docs/kubernetes.md | There is no recorded demo or step-by-step demo script. |
| Clean commits | Yes | `git log --oneline main \| grep -i wip`: 0 hits; PRs #3-#10 with CI | One commit message on main says "notes to follow" (0e7db49; the notes followed in dc7eed6); a local, unpushed branch `security-wip-304ae06` remains. |
| README + setup | Yes | README rewritten (PR #14) and corrected from a fresh-clone test on 2026-10-28 (PR #22): clone from GitHub, install, data check, training (reproduced v2 byte for byte), unit tests (573 passed, 2 skipped without GNN extras) and the full Compose quickstart (materialize, 2,000 events, /health, dashboard with alerts) ran without an error on Windows 11 | Not re-run in the clone: the Kaggle download itself (the CSVs were copied), integration tests, lint/types and the GNN extras; macOS/Linux untested. |
| Architecture diagram | Yes | System-wide Mermaid diagram with what runs in Compose, Minikube and on the host only: [docs/architecture.md](architecture.md) (PR #14). The docs/serving.md diagram now shows `Scorer.score_event -> FeastFeatures.read` (fixed in PR #13). | |
| API / data docs | Yes | [docs/api.md](api.md) (`/health`, `/score`: fields, real example, errors, measured latency) with [docs/openapi.json](openapi.json) exported from the app; [docs/data.md](data.md) (Kafka topics and messages, the five PostgreSQL tables generated from the schema code, Feast objects); also docs/serving.md, docs/features.md, docs/feature_store.md, data/README.md | |
| Test results + numbers | Yes | Regenerated at 915ffdb (2026-10-28): docs/test-results-unit.txt (580 passed with the GNN stack), docs/test-results-integration.txt (41 passed); CI green on every PR; model, latency and throughput numbers above | |
| Presentation | No | None found in the repo | No slides or presentation exist. |
| Reflection | No | README "How this project was built" covers ownership and AI assistance only | No reflection on decisions, results and lessons. |

## Top gaps (ordered by how badly an evaluator would judge them)

1. **No presentation and no reflection**: two named deliverables are missing.
2. **Fresh-clone setup partly verified**: the README was rewritten (PR #14) with results, a
   system architecture diagram and dashboard screenshots (PR #18), and the setup was tested from
   a fresh clone on 2026-10-28 (PR #22: install, data check, training, unit tests and the
   Compose quickstart worked). Not re-run in the clone: the Kaggle download itself, integration
   tests, lint/types and the GNN extras; macOS/Linux untested.
3. **Running the retraining job is manual**: the trigger itself is automatic (row 11 fired and
   trained v8 without `--force-retrain`), but the job that can retrain is started by hand on the
   host, and the k8s CronJob runs checks only (`--no-retrain`) because MLflow is a local SQLite
   file. The trigger is uncalibrated (it fired only on a window chosen after the default did
   not), and analyst feedback labels are not used for retraining.
4. **MLflow coverage (mostly fixed 2026-10-29)**: the five evaluation modules (compare,
   robustness, evaluation_study, robust_eval, leakage_audit) now log their reports to
   `fraud-evaluation`, and the existing reports and `drift_reports` 1-9 were back-filled. Still
   open: new drift checks write only to PostgreSQL. (The champion v2 is tagged
   `git_dirty=true` but was rebuilt byte for byte from a fresh clone on 2026-10-28.)
5. **The graph is weaker than the brief implies**: the account node exists only in the
   optional account graph (2026-10-29) and links to devices through transactions; it did not
   help (GNN alone 0.2008, blends still worse than XGBoost). `DeviceInfo` is a model string,
   the GNN loses to XGBoost (0.2021 vs 0.2992), and ring quality is measured per transaction
   only on tiny rings.
6. **Stale supporting docs (fixed)**: the docs/serving.md diagram, docs/dashboard.md "Not in
   Kubernetes yet" and other stale doc claims were fixed in PR #13; test results were
   regenerated at 915ffdb (580 unit, 41 integration passed). README.md stale items remain
   (gap 2).
7. **No recorded demo**.
8. **Imbalance method**: no SMOTE, and the served model uses no class weight (measured and
   documented, so minor).
