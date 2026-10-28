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
| Feast used at **training** | Partial | Training does **not** read Feast: `src/models/compare.py` calls `compute_history_features` (pandas batch). Feast offline retrieval is tested equal to those batch features. | The brief asks for Feast in both training and serving; training uses the batch code path, with equality proven by tests instead. |
| Train/serve parity tests | Yes | `tests/test_feast_store.py`: `test_point_in_time_retrieval_equals_batch_features`, `test_point_in_time_retrieval_equals_batch_on_real_cards`, `test_stream_replay_with_batch_cold_start` (Feast features == batch training features); `tests/test_integration.py::test_feast_redis_replay_matches_the_store_path` (real Redis); `tests/test_entity_state.py::test_in_order_replay_equals_batch`. The demo showed the old store path skews 96% of rows (PR-AUC 0.2992 -> 0.2533, README "Why Feast is the default"). | |

## Step 3: XGBoost, imbalance, PR-AUC, MLflow

| Item | Status | Evidence | Gap |
|---|---|---|---|
| XGBoost | Yes | `src/models/train.py`, `compare.train_variant`; served v2 `baseline-9fdad27ea3b0` | |
| Class weights or SMOTE | Partial | `scale_pos_weight` implemented and compared (`compare.IMBALANCE_METHODS`, reports/imbalance_comparison.md): **no weight beats it by +0.0418** mean PR-AUC over 3 seeds, so the served v2 has no class weight. v1 (`scale_pos_weight` 27.46) is registered. | SMOTE was never tried, and the served model deliberately uses no class weight (a documented, measured deviation). |
| PR-AUC | Yes | v2 validation PR-AUC **0.2992** (threshold 0.1281: 4.31% alerted, precision 0.3136, recall 0.3930); v1 0.2504; reports/model_v1_vs_v2.md | |
| All runs in MLflow | Partial | Logged: `src.models.train`, `imbalance_comparison`, `version_comparison`, `src.graph.train` / `stack` / `blend`, drift retraining (`src/monitoring/run_drift.py`). Read-only count in mlflow.db: `fraud-xgboost` 18 runs, `fraud-gnn` 6. | Not logged (reports only): `src.models.compare` (reports/model_comparison.md), `src.models.robustness` (model_comparison_seeds.json), `src.models.evaluation_study`, `src.models.robust_eval` (robust_evaluation*.md), `src.models.leakage_audit`; also challenger v8 exists only in an mlflow.db copy (evidence run). |
| Reproducibility of the champion | Partial | v2 tags `git_commit 7754fd4`, `git_dirty=true` (CLAUDE.md Known limitations) | The served champion was trained from a dirty tree and cannot be rebuilt exactly from the repo. |

## Step 4: graph, GNN, rings, blend

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Graph of accounts, devices, cards | Partial | `src/graph/build.py` `ENTITY_COLUMNS`. GNN graph nodes: `tx`, `card` (card1-card6), `device` (`DeviceInfo`), `p_email`, `r_email`, `addr1`. **No uid / account node.** docs/gnn.md "Graph design". | Accounts appear only as card + addr1 nodes, and `DeviceInfo` is a device model / OS string (`Windows`, `iOS Device`), not a device ID. |
| GNN (PyG) | Yes | Hetero GraphSAGE with temporal sampling (`src/graph/model.py`, `data.py`, `train.py`); official validation PR-AUC **0.2021** (MLflow run c233ecd0, reproduced, scores bit-identical) vs XGBoost 0.2992; leakage tests in `tests/test_graph.py` | It loses to XGBoost by about 0.10 PR-AUC; not tuned. |
| Fraud rings | Yes | `src/graph/rings.py` `ring_components`: union-find connected components of the bipartite **uid - DeviceInfo** graph; devices shared by 2-10 uids only, hub uids (> 3 devices or > 1,000 tx) excluded; card_id never a link. `summarize_rings`, `write_table` -> table `fraud_rings`. **The GNN ranks rings (mean score), it does not build them.** reports/fraud_rings.md: 337 rings, 1,378 users. | Rings rest on a weak link (device model strings) and hand-picked limits. |
| Ring quality vs base rate | Partial | Measured: `summarize_rings` `fraud_rate` = labelled fraud rate of the ring's transactions. reports/fraud_rings.md: **top-20 ranked rings 19.83%, all rings 4.06%, overall 3.50%.** | Measured per transaction only, not per ring member (uid); the top rings are 2-3 users, so the evidence is weak. |
| GNN as XGBoost feature | Yes (measured, not kept) | reports/gnn_stacking.md: mean 0.2827 vs 0.2935 over 3 seeds (-0.0108) | |
| Blend | Yes (measured, not served) | `src/graph/blend.py`; reports/gnn_blend.md; PR #8. transaction w=0.50: AP 0.2749 vs 0.2992, CI [-0.0346, -0.0137]; entity w=0.45: 0.2797 vs 0.2992, CI [-0.0263, -0.0118]. Both worse; not served. | |

## Step 5: streaming and scoring

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Kafka producer replay | Yes | `src/streaming/producer.py` (validation-period only, time order enforced, labels and D1 never sent); 118,108 events streamed in the demo | |
| Consumer fetches features, returns a score | Yes | `src/streaming/consumer.py` -> `processor.process_message` -> `Scorer.score_event` (Feast read, XGBoost, PostgreSQL write, Feast push); DLQ, retry, commit-after-write (`tests/test_streaming_reliability.py`, chaos script `scripts/chaos_kafka_restart.py`) | The consumer is a Kafka consumer, not a FastAPI service; FastAPI serves the same scorer synchronously at `POST /score` (`src/serving/api.py`). |
| Performance | Yes | 46.8 events/s on 3 consumers, scorer p50 48 ms (README "Consumer throughput"); Feast features+score p50/p95 107.5 / 179.0 ms | Local single-node numbers only. |

## Step 6: drift and retraining

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Evidently drift reports | Yes | `src/monitoring/drift.py`, `run_drift.py`, `configs/drift.toml` (Evidently 0.7.23); rows in `drift_reports`, HTML in reports/drift/ (gitignored) | |
| Retraining trigger + promotion gate | Yes | `src/monitoring/rules.py`, `retrain.py`; paired day-block bootstrap gate; `tests/test_monitoring.py` | |
| Fired without `--force` | Yes | docs/evidence/2026-10-27-drift-trigger/ (PR #9, #10): row 10, default window days 176-183, **did not fire**; row 11, days 170-182 (chosen after that), fired on R_emaildomain, `forced = false`; challenger v8 (copy DB only), champion kept: delta +0.0053, CI [-0.0160, +0.0254] | The firing window was chosen after the default window did not fire. |
| Automatic | Partial | The k8s CronJob runs every 6 h but with `--no-retrain` (docs/kubernetes.md "Drift CronJob") | Nothing schedules the full drift -> retrain -> promote loop; retraining runs only by hand on the host. |
| Known limitations | Yes | CLAUDE.md Known limitations: top-5-by-gain rule added after the first runs did not fire, not calibrated on drift-free periods, no minimum sample size (the k8s run fired on 30 rows) | |

## Step 7: dashboard and Kubernetes

| Item | Status | Evidence | Gap |
|---|---|---|---|
| Streamlit dashboard: alerts, case status | Yes | `src/dashboard/` (Overview, Alert queue, Case detail, Fraud rings, Drift); workflow `workflow.py`, optimistic locking; `tests/test_dashboard.py` drives the real user path; docs/dashboard.md | No authentication (self-declared analyst name). |
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
| Demo | Partial | Docker Compose demo stack with recorded counts (README "Demo"; 118,108 / 5,093); k8s run in docs/kubernetes.md | There is no recorded demo or step-by-step demo script. |
| Clean commits | Yes | `git log --oneline main \| grep -i wip`: 0 hits; PRs #3-#10 with CI | One commit message on main says "notes to follow" (0e7db49; the notes followed in dc7eed6); a local, unpushed branch `security-wip-304ae06` remains. |
| README + setup | Partial | README "Setup (Windows, Python 3.11)"; Kaggle download steps in data/README.md | Fresh-clone setup incl. the Kaggle download is **Not verified** (never tested in a clean folder); README "Planned" still lists the finished GNN, drift and dashboard, "Progress" stops at Milestone 3, and "Documentation" says docs "will be added". |
| Architecture diagram | Partial | ASCII diagrams in docs/serving.md and docs/kubernetes.md | No system-wide diagram, and the docs/serving.md diagram still shows history read from PostgreSQL (Feast is the default). |
| API / data docs | Yes | docs/serving.md (`/health`, `/score`), docs/features.md, docs/feature_store.md, data/README.md, docs/dashboard.md (schema) | |
| Test results + numbers | Partial | docs/test-results-unit.txt (495 passed with the GNN stack), docs/test-results-integration.txt (17 passed), CI #30-#36 green; model, latency and throughput numbers above | The recorded test-result files date from 2026-10-25 and predate the dashboard, k8s dashboard and GNN blend; docs/dashboard.md still says "Not in Kubernetes yet". |
| Presentation | No | None found in the repo | No slides or presentation exist. |
| Reflection | No | README "How this project was built" covers ownership and AI assistance only | No reflection on decisions, results and lessons. |

## Top gaps (ordered by how badly an evaluator would judge them)

1. **No presentation and no reflection**: two named deliverables are missing.
2. **README is stale and has no system architecture diagram**: "Planned" lists finished work,
   "Documentation will be added", Progress stops at Milestone 3, and the fresh-clone setup
   (incl. the Kaggle download) was never verified.
3. **Feast is not used by training**: training uses batch pandas features (proven equal by
   tests), while the brief asks for Feast in both training and serving.
4. **Retraining is not automatic end to end**: the CronJob runs `--no-retrain`, retraining is
   manual on the host, the trigger is uncalibrated (it fired only on a window chosen after the
   default did not), and analyst feedback labels are not used for retraining.
5. **Not every run is in MLflow**: five evaluation modules (compare, robustness,
   evaluation_study, robust_eval, leakage_audit) write reports only; the champion v2 came from
   a dirty tree.
6. **The graph is weaker than the brief implies**: no account node, `DeviceInfo` is a model
   string, the GNN loses to XGBoost (0.2021 vs 0.2992), and ring quality is measured per
   transaction only on tiny rings.
7. **Stale supporting docs**: test-result files (2026-10-25), docs/serving.md diagram,
   docs/dashboard.md "Not in Kubernetes yet".
8. **No recorded demo**.
9. **Imbalance method**: no SMOTE, and the served model uses no class weight (measured and
   documented, so minor).
