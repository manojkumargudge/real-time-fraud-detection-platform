# M2 evidence pass (2026-11-16, main b62c29b)

This pass finishes the audit plan started on 2026-11-11 (`../2026-11-11-audit/`). Every run
used code from `main` b62c29b, after Batch 1/2, the S3 connection pool and S7 auth.

Runs went only to copies: `fraud_bench` and the temporary API on it, a test-stack database, an
`mlflow.db` copy, a fresh clone, and the Minikube cluster's own database. The demo database was
only read. Its pg_stat_user_tables snapshot before and after B1 had 0 deltas on all 5 tables
(118,108 / 5,093 / 9 / 20 / 6). The real `mlflow.db` stayed at sha256 `dd198df9…`, and the
real `mlruns/` got no new files.

Paths in the logs are shortened to `<home>`.

## B1: streaming replay and API-side Feast log (bench copy)

`python -m scripts.bench_throughput setup --replay 30000` gave the same result as on 2026-11-15:
`fraud_bench` with 88,108 rows and 3,830 alerts, Feast state in `bench-redis` with 56,830
keys, and 3 bench consumers built from this checkout.

| Check | Result | File |
|---|---|---|
| Replay of events 0-199 through Kafka -> 3 consumers (`run --label m2-200 --index 0 --limit 200`) | 200 produced, 200 stored, statuses `scored` 200 | `b1-bench/m2-200.json`, `m2-200.stdout.txt` |
| Same 200 IDs in `fraud_bench` vs the demo DB (read-only) | 200 / 200 rows on both sides, same IDs; scores **bit-identical 200/200** (max diff 0.0); `is_alert` 200/200; alerts 6 vs 6; model `baseline-9fdad27ea3b0` | `b1-bench/m2-200.compare.txt` |
| Duplicates, DLQ, lag | 200 distinct IDs; `bench-transactions.dlq` offset 0; group `bench-scorer` lag 0 on all 3 partitions (83 + 50 + 67) | (commands in this README) |
| Feast at serving, consumer side | 50 + 83 + 67 = **200/200** `scored ... features=feast` lines; no scored line with another source | `b1-bench/m2-200.consumer-{1,2,3}.log` |
| Feast at serving, API side | Temporary API on `fraud_bench` (image `fraud-platform-api` c76a178b0e7b, `FEATURE_SOURCE=feast`, throwaway `API_KEY`). One `POST /score` without a key: 401. Then `load_test_score --levels 1 --warmup 0 --duration 1 --skip 200`: 33 fresh requests, 0 errors, 0 duplicates. Log: **33/33** `scored ... features=feast` lines; the key does not appear in the log | `b1-bench/m2-api.api.log`, `b1-bench/api-requests/` |
| Teardown (approved) | `fraud_bench`, bench topics, group and Redis removed; databases back to fraud, postgres, template0, template1 | `b1-bench/teardown-2026-11-16.stdout.txt` |

Earlier runs of the same replay: 2026-11-11 on ca83260 (200 stored, 0 duplicates, DLQ 0, lag 0,
200/200 `features=feast` lines) and 2026-11-15 on the pool code (200/200 identical to the demo,
6/6 alerts). Their raw output stays in the gitignored `reports/bench/`.

**Throughput is not re-measured here.** The 46.83 events/s figure is from 2026-10-25 on
ca57b2c (`consumer-throughput`). B1's 64.9 events/s over 200 events is a short burst and is not
comparable.

## B2: drift job fires and retrains on main (copies)

- Database `fraud_m2s6` on the test-stack PostgreSQL, restored from a demo dump taken today
  (7,252,496 bytes): 118,108 / 5,093 / 9 / 20 / 6.
- A fresh copy of `mlflow.db` (sha256 `dd198df9…`, aliases champion 2, production 2,
  challenger 7). Only in the copy, every experiment's `artifact_location` points at a folder
  next to it.
- Command: `python -m src.monitoring.run_drift --window-start-day 170 --window-end-day 182
  --retrain-feature-source pandas` (days 170-181, last day included), no `--force-retrain`.

| | 2026-11-16 (main b62c29b, pandas) | 2026-11-11 (ca83260, pandas and Feast) |
|---|---|---|
| Trigger | fired: top-feature drift, R_emaildomain; `forced = false`; 31,748 current rows (minimum 1,000) | same rule |
| Training matrix | 505,109 x 31, sha256 `5326f6fda279b2b6…54571` | same |
| Challenger | v8 **in the copy only**, `model.json` sha256 `5dfeb952e74c…`; tags `git_commit` b62c29b, `git_dirty = false`, `feature_source = pandas`, `forced = false` | same model hash |
| Promotion | NO: 0.3290 vs 0.3238, delta +0.0053, CI95 [-0.0160, +0.0254] | same |
| drift_reports row | id 10 in `fraud_m2s6` | id 10/11 in `fraud_p2` |
| Runtime | 163 s | 294 s (pandas), 2,726 s (Feast) |

Code that changed since 2026-11-11 on this path: the dirty-tree guard before training, the
1,000-row minimum, and stored blocked retrains with exit 4. None of it depends on the feature
source. Feast retraining parity (same matrix, same model) was proven on 2026-11-11
(`../2026-11-11-retrain-feast/`), so this run uses the pandas path. File:
`b2-drift-copy/run_pandas.log`.

## B3: full test and scan pass (main b62c29b, this laptop)

| Check | Result | File |
|---|---|---|
| Unit (`pytest --ignore=tests/test_integration.py -rfEs`, GNN extras installed) | **726 passed, 1 skipped** (opt-in `test_explain_real.py`), 8 min 39 s | `b3-tests-scans/unit.txt` |
| Integration (test stack, ports 25432/29092/26379) | **44 passed**, 9 min 25 s | `b3-tests-scans/integration.txt` |
| ruff check, ruff format --check, mypy strict | clean (116 files formatted; mypy 115 source files) | |
| Accepted-risk expiry | ok: 12 pip-audit + 5 Trivy entries, earliest 2027-01-31 (CI fails from 2027-01-18) | |
| pip-audit 2.10.1, all 6 requirements files + GNN equivalents | no known vulnerabilities apart from the listed accepted risks | `b3-tests-scans/pip-audit.txt` |
| bandit 1.9.4 `-r src -ll` | no findings | `b3-tests-scans/bandit.txt` |
| Trivy 0.75.0, serving and dashboard images | 0 HIGH/CRITICAL with a fix, after a `--no-cache` build | `b3-tests-scans/trivy-{serving,dashboard}.txt` |

**Trivy and the local layer cache.** The first local scan found 4 HIGH in each image: apt 2.6.1
(CVE-2026-109518) and libssl3/openssl 3.0.22-1~deb12u1 (CVE-2026-84782). The image build had
reused a cached Debian upgrade layer from before those fixes. CI builds without that cache:
CI #116 installed apt 2.6.2 and libssl3 3.0.22-1~deb12u2 and passed. A `--no-cache` rebuild
scanned clean. Local images built from a stale cache can lag behind the security updates.

## B4: fresh clone

- `git clone` of GitHub `main` (b62c29b) into a new folder, then `py -3.11 -m venv .venv` and
  `pip install -r requirements-dev.txt`. Exit 0, `pip check`: no broken requirements. It took 92
  min, while two `--no-cache` image builds ran in parallel, so the time is not comparable with
  the README's ~22 min.
- Dataset copied in (the Kaggle download was not tested). `python -m src.data.load`: 590,540
  rows, fraud rate 3.4990%.
- `python -m src.models.train --feature-source pandas --imbalance none`: model
  `baseline-9fdad27ea3b0`, `model.json` sha256 equal to the served bundle (**byte for byte**),
  256 s.
- Unit suite without the GNN extras: **709 passed, 3 skipped** (`test_graph.py` and
  `test_blend.py` without torch_geometric, plus the opt-in demo-DB test). It took 46 min under
  the same load. Files: `b4-fresh-clone/`.
- `.env.example` and README "Quickstart" cover `API_KEY` and `DASHBOARD_USERS` (placeholders,
  how to generate them, single quotes for bcrypt, Compose refuses to start without them).
- **The Compose quickstart was not run from the clone.** `docker-compose.yml` fixes
  `name: fraud-platform` and the host ports, so a second checkout would attach to the demo
  stack's volumes (README "A second checkout").

## B5: Minikube redeploy with Secret `fraud-auth`

- Before: namespace `fraud-k8s` existed (20 days old). The cluster database had 40
  `scored_transactions` (IDs 3459432-3459471) and 1 alert. Secret `fraud-postgres` only. The
  `drift` CronJob was not suspended but had no last schedule (it never ran in this namespace).
- Images built from main b62c29b with `k8s/build-image.sh` (serving, monitoring) plus the
  dashboard image, loaded with `minikube image load`. Secret `fraud-auth` created by a command:
  a random `api-key` and one bcrypt entry for `demo-analyst` (values never printed). Then
  `kubectl apply -k k8s` and `rollout restart` of api, consumer and dashboard. All pods 1/1
  Running.
- API through a port-forward: `/health` ok (`store_ok` true); `/model` linked to
  `fraud-xgboost` v2. `POST /score` without a key: 401; with a wrong key: 401; with the key on
  stored transaction 3459432: 200, `duplicate = true`, score 0.17915059626102448 (the demo's).
- 20 new events (validation events 40-59, IDs 3459472-3459491) through ConfigMap `m2-events` and
  Job `produce-events-m2`: 20 stored. Scores **identical to the demo database 20/20** (max diff
  0.0), alerts 1 vs 1. 9 + 6 + 5 = 20 consumer lines with `features=feast`. Group `fraud-scorer`
  lag 0 (17 + 20 + 23 = 60 offsets = 60 rows), `transactions.dlq` offset 0. Files: `b5-minikube/`.
- Dashboard (in the pod, Streamlit AppTest, view only): the first render shows only the sign-in
  form. After signing in, analyst = `demo-analyst`, and all 5 pages (Overview, Alert queue, Case
  detail, Fraud rings, Drift) render with 0 exceptions.
- Not run: the drift CronJob. On 60 rows it would store "insufficient data" (minimum 1,000).
