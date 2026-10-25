# Real-Time Financial Fraud Detection Platform

[![CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/workflows/ci.yml)

CI runs ruff, strict mypy, the unit tests, the integration tests (throwaway PostgreSQL,
Kafka and Redis) and the security scans (see "Security") on every push and pull request to
`main`; CD then builds the serving image and
pushes it to `ghcr.io/manojkumargudge/real-time-fraud-detection-platform` (tags: commit SHA and
`latest`). Nothing is deployed.

An AI/ML project for detecting suspicious financial transactions,
identifying potential fraud networks, and supporting analyst investigations.

## Project Status

Work in progress: a tested local demo, not a production system.

### Done

- IEEE-CIS dataset loader and EDA ([reports/eda_summary.md](reports/eda_summary.md))
- Data-quality report ([reports/data_quality.md](reports/data_quality.md))
- Leakage-safe point-in-time features with an 18-check audit
  ([docs/audit_milestone2.md](docs/audit_milestone2.md); check 18, D1 semantics, is blocked
  pending provider documentation, so D1 stays out of serving)
- Chronological train/validation split
- XGBoost model served: `baseline-9fdad27ea3b0` (MLflow `fraud-xgboost` version 2, no class
  weight, seed 42): validation PR-AUC 0.2992 against a 3.44% fraud rate
  ([reports/imbalance_comparison.md](reports/imbalance_comparison.md)). Previous served model:
  version 1, `baseline-6026acdc8e2e` (`scale_pos_weight` 27.46), validation PR-AUC 0.2504
  ([reports/model_comparison.md](reports/model_comparison.md))
- Kafka producer and consumer with a retry policy and a dead-letter queue (`transactions.dlq`)
- Idempotent PostgreSQL writes (`TransactionID` primary key)
- FastAPI `/health` and `/score`
- Docker Compose stack (Kafka, PostgreSQL, Redis, API, consumer)
- Feast feature store, **the default feature source** ([docs/feature_store.md](docs/feature_store.md)):
  per-entity state (`uid`, `card_id`) in Redis, started from the training history (all
  transactions before the validation cut-off) and updated after every saved event; features are
  computed from state + event. Point-in-time retrieval, the cold-start replay and the live demo
  equal the batch (training) features. The older store path (each card's event history in
  PostgreSQL) stays behind `FEATURE_SOURCE=store`
- MLflow tracking and model registry (local SQLite `mlflow.db`, `python -m src.models.train`;
  registered as `fraud-xgboost`; alias `champion` = best candidate, `production` = currently
  deployed; both point to version 2 since 2026-10-23)
- 396 unit tests ([docs/test-results-unit.txt](docs/test-results-unit.txt)) and 15 integration
  tests ([docs/test-results-integration.txt](docs/test-results-integration.txt))

#### Experiment: class imbalance

- Removing class weighting (`scale_pos_weight` 27.46 -> none) raised mean validation PR-AUC
  from 0.2517 to 0.2935 over 3 seeds; the gain (+0.0418) is about 3 times the seed spread
  (0.0134).
- Confirmed on two other time windows with seed 42: W1 +0.0137 (small), W2 +0.0430.
- Trade-off: on W1 at the 1% alert budget, `none` was slightly worse; it was better at 5%.
- Deployed on 2026-10-23: version 2 is now `production` and served by the API and consumer
  (`/health` reports `baseline-9fdad27ea3b0`).
- Details: [reports/imbalance_comparison.md](reports/imbalance_comparison.md). Rerun with
  `python -m src.models.imbalance_comparison` and
  `python -m src.models.imbalance_comparison --confirm`.

#### Served model v1 vs v2 at fixed alert budgets

Same validation window (118,108 rows, 3.44% fraud); at each budget both models flag the same
number of transactions ([reports/model_v1_vs_v2.md](reports/model_v1_vs_v2.md),
`python -m src.models.version_comparison`):

| Budget | Recall v1 | Recall v2 | Precision v1 | Precision v2 | Threshold v1 | Threshold v2 |
|---|---|---|---|---|---|---|
| 1% | 0.1321 | 0.1550 | 0.4543 | 0.5330 | 0.8967 | 0.3640 |
| 2% | 0.2266 | 0.2507 | 0.3898 | 0.4312 | 0.8401 | 0.2433 |
| 5% | 0.3991 | 0.4181 | 0.2746 | 0.2877 | 0.6922 | 0.1121 |

Where v2 is worse: at its own deployed threshold (0.1281) it flags 4.31% of transactions and
catches 39.30% of frauds, against v1's 5.47% and 41.93% at 0.6721; and on window W1 at the 1%
budget (confirmation above). Thresholds are chosen per model, by max F1 on the training
holdout, so the alert rate changed with the model.

Known limitation: the threshold is chosen by max F1 on the stage-1 model's holdout scores but
applied to the refit model (trained on all training rows), so the deployed alert rate can differ
from the holdout's. Next step: an alert-budget threshold, checked on post-deployment data.

### Planned

- PyTorch Geometric GNN for fraud rings
- Evidently drift reports and a retraining trigger
- Analyst dashboard
- Kubernetes (Minikube): local deployment with Feast, liveness heartbeat and a drift CronJob
  ([docs/kubernetes.md](docs/kubernetes.md)); not production (no replication, Ingress or secrets manager)

Note: the IEEE-CIS fraud rate is about 3.5% (3.499% overall), not under 1%.

### Why Feast is the default: train/serve skew

The store path only knows events the service has ingested. With a stream that starts at the
validation cut-off (as in production, after training), 96% of streamed rows get history
features that differ from training, validation PR-AUC falls from **0.2992 to 0.2533**, and
**2% of alert decisions flip** (2,345 of 118,108). Feast starts from the training history, so
its features equal the training features. The cost is latency: features + score take p50
107.5 ms with Feast vs 61.1 ms with the store path (p95 179.0 vs 88.8 ms), because of Feast
Python SDK overhead (a Redis round trip is 2 ms); see docs/feature_store.md.

### Demo: unseen validation data (reset 2026-10-23)

The demo stack streams **only validation-period transactions** (`TransactionDT` >=
12,192,900), in time order; the producer refuses anything earlier or out of order, and labels
are never sent. Feast was materialized from the 472,432 earlier transactions (39,087 uid and
13,792 card states). Earlier demo data was backed up and cleared.

| | First 5,000 events, 1 consumer | Next 5,000 events, 3 consumers |
|---|---|---|
| Throughput while a backlog existed | 7.3 events/s | 12.0 events/s (1.6x, not 3x) |
| Per-event latency in the consumer, p50 / p95 | 112.2 / 239.8 ms | 214.3 / 422.2 ms |
| Peak sampled consumer lag (whole batch published at once) | 3,910 | 3,130 |
| Errors, dead-lettered events, consumer restarts | 0 / 0 / 0 | 0 / 0 / 0 |

1.6× with 3 consumers on one laptop; per-event latency doubled, likely CPU-bound on a single host (not profiled).

First 5,000 events: 248 alerts (4.96%), 107 of them true fraud (precision 0.4315, recall
0.4693; 228 frauds). This window (the first ~1.9 days of validation) has a higher fraud rate
than validation as a whole, 4.56% vs 3.44%, and with 228 frauds the recall's 95% margin is
about +-0.065, so these numbers flatter the model. **The model's real performance is the
full-validation figure at the deployed threshold: precision 0.3136, recall 0.3930** (4.31% of
transactions alerted). Serving added no error: live scores matched offline batch scores
exactly (max difference 9×10⁻¹⁰), with identical alerts.

Three consumers did not scale linearly: per-event latency doubled, which points to a shared
bottleneck (the laptop's CPU or the PostgreSQL / Redis round trips); not yet profiled. Events
are keyed by `card_id` (events without card1 have no card key and use `id:<TransactionID>`),
so each card's events stay in one partition, in order, and each card's Feast state has one
writer among the consumers. After the rebalance, the Redis state of 300 sampled streamed uids
(216 cards) equalled the state rebuilt from batch data, and their next unseen events got the
same features as batch.

Earlier (2026-10-22) the at-least-once path was checked on the first IEEE-CIS transactions:
2,300 messages = 2,000 unique `TransactionID`s + 300 duplicates, each stored once.

### Consumer throughput (2026-10-25)

Benchmark on a clone of the demo database (`fraud_bench`), with 3 consumers exactly as in the
demo (`FEATURE_SOURCE=feast`) and the same 4,000 held-out events
(`python -m scripts.bench_throughput`; details in docs/serving.md "Throughput benchmark").

| | Before | After |
|---|---|---|
| Unthrottled throughput, 3 consumers | 13.1 events/s | **46.8 events/s** |
| End-to-end latency at 5 events/s, p50 / p95 | 114 / 669 ms | 31 / 61 ms |
| Scorer latency under load, p50 / p95 | 203 / 357 ms | 48 / 105 ms |
| Consumer CPU under load | ~1.8 cores each | ~0.5 cores each |

**Cause:**
- PostgreSQL, Redis and Kafka were not the bottleneck.
- XGBoost used all 12 cores for every one-row prediction, so 3 consumers oversubscribed the
  CPU.
- The Feast scoring path rebuilt the full batch feature frame per event, only to overwrite it.
- The Feast SDK deserialised every feature view on each read.

**Fix:** single-threaded predict, the per-event work done once, and Feast feature views
resolved once. Delivery, model, features and feature source are unchanged. The 4,000 replayed
scores are bit-identical to the scores the demo stored.

### Consumer crash fix and chaos test (2026-10-25)

**The crash:**
- During the full-validation stream on 2026-10-24, all 3 consumers exited twice.
- Cause: an offset commit failed with the retriable Kafka errors `NOT_COORDINATOR` /
  `COORDINATOR_LOAD_IN_PROGRESS` (broker under heavy load). The consumer tolerated only
  rebalance errors and re-raised everything else.
- Docker restarted the consumers and nothing was lost or duplicated, but the stream stalled
  each time.

**The fix** (`src/streaming/consumer.py`):
- The retriable coordinator and timeout errors (and any error librdkafka flags as retriable)
  are retried 3 times (0.2 / 0.5 / 1 s).
- If the commit still fails, it counts as `commit_rejected` and the consumer carries on. The
  message is redelivered later and the `TransactionID` primary key deduplicates it
  (at-least-once).
- If commits keep failing for more than 30 s, an ERROR is logged every 30 s with
  `consecutive_commit_failures`. Other errors still stop the consumer. Details: docs/serving.md
  ("Offsets").

**Chaos test** (test stack only, `python -m scripts.chaos_kafka_restart [--mode restart|kill|pause]`):
- Each mode ran 500 events and disrupted test Kafka mid-stream: restart, kill + start, or a
  70 s pause.
- In every mode, all 3 consumers stayed alive, 500 of 500 rows were written, there were 0
  duplicate IDs, lag was 0 and the DLQ was empty.
- `commit_rejected` was 0, 0 and 3. The 3 in the pause run came from a rebalance and were
  redelivered and skipped as duplicates.
- None of the runs triggered `NOT_COORDINATOR` on a commit. librdkafka waits for the
  coordinator instead, so the new retry path is verified by unit tests only. Results:
  docs/serving.md ("Chaos test").

## Security

CI job `security` (.github/workflows/ci.yml) runs on every push and pull request; the image is
only pushed to GHCR when it passes. The scanners gate differently:

- **Trivy** fails on any HIGH/CRITICAL finding in the image that has a fix, unless it is in
  `.trivyignore`. Findings without a fix and MEDIUM/LOW findings are skipped silently.
- **pip-audit** fails on **any** advisory, of any severity, that is not in
  `security/pip-audit-ignore.txt`. New advisories can turn CI red without a code change.
- **bandit** fails on any medium- or high-severity finding in `src/`; low findings are not shown.
- An accepted-risk entry past its expiry date also fails the job.

| Scanner | Scope | Gate |
|---|---|---|
| pip-audit 2.10.1 | `requirements.txt`, `requirements-serving.txt`, `requirements-monitoring.txt`, `requirements-dev.txt`, `requirements-gnn.txt` (with its PyTorch/PyG indexes) | any known vulnerability not in `security/pip-audit-ignore.txt` |
| bandit 1.9.4 | `src/` | medium severity and above (`-ll`) |
| Trivy 0.75.0 | the serving image built from `Dockerfile` | HIGH/CRITICAL with a fix available (`--ignore-unfixed`), minus `.trivyignore` |

Not auditable: the `+cpu` torch and pyg-lib wheels are not on PyPI, so pip-audit skips them in
`requirements-gnn.txt`; torch is audited through its PyPI-equivalent version (`--no-deps`), and
pyg-lib cannot be audited.

Fixed on 2026-10-23 (first scan):

- fastapi 0.115.6 -> 0.143.0 with starlette 0.41.3 -> 1.7.0 (7 starlette advisories: multipart
  and Range-header DoS, Host-header / URL injection, ...): PYSEC-2026-1941 (CVE-2025-54121),
  PYSEC-2026-1942 (CVE-2025-62727), PYSEC-2026-161 (CVE-2026-48710), PYSEC-2026-2280
  (CVE-2026-48817), PYSEC-2026-2281 (CVE-2026-48818), PYSEC-2026-248 (CVE-2026-54282),
  PYSEC-2026-249 (CVE-2026-54283). pip-audit reports each one twice for 0.41.3; 7 are unique.
- mlflow 3.15.2 -> 3.17.2 (3.16.1 first; 3.17.2 fixes CVE-2026-53995 and CVE-2026-115886,
  found 2026-10-24) and cryptography 49.0.0 -> 50.0.2; pytest 8.3.5 -> 9.1.1
- **`main` now requires mlflow 3.17.2+** for the local registry. Newer mlflow refuses an older
  `mlflow.db` ("out-of-date database schema"). Back it up, then run
  `mlflow db upgrade sqlite:///mlflow.db`. The migration is **one-way**: older mlflow versions
  cannot read the file afterwards (keep the backup to go back)
- serving image: base `python:3.11.9-slim` (Debian 12.7) -> `python:3.11.16-slim-bookworm`
  (Debian 12.15) plus `apt-get upgrade` and fixed setuptools/wheel: Trivy HIGH/CRITICAL findings
  133 -> 65, of which 63 have no Debian fix yet
- bandit: no medium/high findings (6 low: asserts, the `<NA>` token, the `git` subprocess)

Accepted risks (reason and expiry in `security/pip-audit-ignore.txt` / `.trivyignore`; CI fails
again after the expiry date, 2027-01-31):

| Package | Advisory | Why accepted |
|---|---|---|
| feast 0.47.0 | CVE-2025-11157 (RCE) | only in the Kubernetes materializer job, which is not used; 0.54 needs numpy 2 / newer pydantic |
| pyarrow 18.0.0 | CVE-2026-25087 | use-after-free when reading Arrow IPC files; only our own parquet is read; pinned by feast |
| torch 2.8.0 (GNN, batch only) | 8 advisories | affected operators (`nan_to_num`, `linalg.lu`, `jit.script`, `lstm_cell`, ...) are not used and nothing is `torch.load`-ed; 2.10 fails to load on Windows. torch is imported only by the batch GNN (`src/graph/`), which nothing in `src/serving/` or `src/streaming/` imports; `requirements-serving.txt` does not install it and the built serving image contains no torch (checked with `pip list` in the container) |
| zlib1g 1:1.2.13.dfsg-1 (Debian 12) | CVE-2023-45853 (CRITICAL) | Debian status `will_not_fix`: a rebuild will **not** fix it. The overflow is in minizip's `zipOpenNewFileInZip4_64`; in the serving image `libz.so.1` does not export that function and no minizip library is installed (checked 2026-10-24). Listed in `.trivyignore` |
| Debian 12 OS packages | 62 more HIGH/CRITICAL without a fix (55 `affected`, 7 `fix_deferred`) | no fixed package exists yet; a rebuild picks up Debian fixes once they are released |

## Monitoring (drift and retraining trigger)

`python -m src.monitoring.run_drift` is a standalone batch job (not part of the API), so it
can later become a Kubernetes CronJob. Every threshold lives in one file,
[configs/drift.toml](configs/drift.toml). All values are required, unknown keys are rejected,
and the values were set before the first real run. Evidently 0.7.23 (current `Report` /
`Dataset` API), with telemetry switched off.

**Time.** "Now" is the latest `transaction_dt` in `scored_transactions`, never wall-clock
time. Every window below is in transaction time and is printed and stored with each run.

**What is compared**

- **Reference**: the last 30 days of the champion's training window, with features from the
  same point-in-time code as serving. Not the whole window: history features (prior counts,
  time since the previous transaction) depend on how much history exists. The cold-start early
  weeks would look like drift against any later window.
- **Current**: scored transactions in the chosen window. Features are recomputed with the same
  code (equal to what Feast served, docs/feature_store.md), and the scores are the ones
  production stored.
- **Reference scores**: the champion's recipe is refit on the training rows *before* the
  reference window, then scored on it. These scores are out-of-sample, like production.
  In-sample scores would be overconfident and would look like prediction drift by themselves.

**Checks and trigger** (any one fires)

| Check | Method | Fires when |
|---|---|---|
| Feature drift | Evidently `DataDriftPreset` on the model's input features: Wasserstein (numeric), Jensen-Shannon (categorical); a feature drifts at score >= 0.1 | share of drifted features > 0.5 |
| Prediction drift | Evidently `ValueDrift` on the score, PSI | PSI >= 0.2 |
| Delayed-label performance | PR-AUC of stored scores on labels in (cutoff - 7 days, cutoff], needing at least 30 frauds | PR-AUC < champion validation PR-AUC - 0.05 |
| Top-feature drift (added later, see below) | the same per-feature test and threshold, on the champion's top 5 features by XGBoost gain (read from the champion's booster on every run, not hardcoded) | any of them drifts |

**Why the top-feature rule exists.** It was added on 2026-10-24, *after* the first demo runs
(runs 1 to 3 below). Those runs showed that the share rule (more than half of 28 features) did
not fire on a synthetic 2x TransactionAmt shift: only 11 of 28 features drifted, although
TransactionAmt itself was flagged. A shift in one or two important features is invisible to a
dataset-level share. The new rule watches the features the model relies on most. Every other
threshold is unchanged. Each row also stores the top-5 list and the ones that drifted.

**The matured-label window is the same in every run, by design.** The delayed-label PR-AUC
uses labels in (cutoff - 7 days, cutoff], with cutoff = now - 30 days. "Now" is the latest
scored transaction, not the run's current window, so with the full validation stream this is
always days 146 to 153 (20,264 rows, 617 frauds), whatever window is checked for drift. It
answers "how is the model doing on the newest labels we are allowed to see", which does not
depend on the drift window.

**Label delay.** Chargebacks arrive weeks after a transaction. A label is used only if its
transaction is at least 30 days older than now: cutoff = now - 30 days, and a label exactly at
the cutoff is usable. `usable_labels` is the only place labelled rows are selected. Tests poison
every label newer than the cutoff and show that the results do not change.

**When it fires** (host only):

1. Train a challenger with the champion's recipe on every usable label before the holdout. The
   holdout is the last 7 days of usable labels, (cutoff - 7 days, cutoff].
2. Log it to MLflow and register it with alias `challenger`.
3. Score champion and challenger on the same holdout. Paired bootstrap of the PR-AUC
   difference: 1,000 resamples, seed 42, resampling whole days.
4. Move `champion` only if the lower bound of the 95% CI of (challenger - champion) is above
   the margin of 0.01. Both point estimates, the CI and the reason are logged.

The job **fails** if the champion's or the challenger's training window overlaps the holdout:
the comparison would then be in-sample.

`--force-retrain` (host only) runs steps 1 to 4 even when no check fired. The row is then
stored with `forced = true`, and the promotion rule and the `production` guard are unchanged.
It cannot be combined with `--no-retrain`.

**Challenger reuse.** Step 1 is skipped when the current `challenger` was trained on exactly the
planned training rows. The check compares the run's tags `train_end_dt` and
`train_data_sha256` (SHA-256 of the sorted TransactionIDs and labels, the window bounds, the
seed and the imbalance method) with the planned data. The job then logs `reused challenger vN`
and runs the same bootstrap comparison with that model, so repeated runs on the same labels
stop adding identical registry versions. Challengers registered before this guard (v7) have
no such tags, so the next real run trains one more time.

**Design trade-off**

- **The trigger is cheap and sensitive by design.** A false alarm costs one retrain (about
  73 s on this laptop), and a reused challenger costs nothing.
- **Production is protected by the bootstrap promotion gate, not by the trigger.** `champion`
  moves only if the 95% CI lower bound of the PR-AUC gain is above the margin, and
  `production` is never moved by the job.
- **Real-data result:** challenger +0.0053 PR-AUC, 95% CI [-0.0160, +0.0254], so the champion
  was kept.
- **Known limits:**
  - The share rule (more than half of 28 features) missed a 2x TransactionAmt shift.
  - The top-5-by-gain rule fires on borderline drift (card3 at 0.1041 vs the 0.1 threshold)
    and also misses that shift, because TransactionAmt is not in the top 5 by gain.
  - **Next step (not built):** rank features by their drift impact on the score instead of by
    gain.

**Storage.** Each run writes one row to PostgreSQL table `drift_reports`. The row holds:

- the windows, now and the label cutoff;
- the drifted-feature share and the top drifted features;
- the PSI and whether prediction drift was detected;
- the delayed-label PR-AUC;
- whether it triggered and why;
- the retrain run ID, the challenger version and the promotion decision (point estimates and
  CI);
- the path of the Evidently HTML report in `reports/drift/` (gitignored).

Runs on a shifted copy are marked `synthetic = true`, and their HTML title starts with
`SYNTHETIC=TRUE`.

**What is not automated, and why**

- `production` is never moved by the job. `set_alias` refuses it, and the alias is re-read
  after every promotion. Moving `champion` does not change what is served: serving loads
  `artifacts/baseline/`. Deploying a new model is a human step.
- **Retraining runs on the host only.** The MLflow registry is a local SQLite file whose
  artifact paths are Windows `file:///C:/...` paths, which a Linux container cannot open. The
  Compose service therefore runs drift checks only:
  `docker compose --profile monitoring run --rm drift [--window-start-day D --window-end-day D]`.
  The fix is an MLflow server with a shared artifact store, which is out of scope here.
- No scheduler yet. The Compose service is one-shot, like the planned CronJob.
- The reference scores come from a fresh XGBoost refit, which is not bit-identical across
  platforms. On the same window, host and container gave the same drifted share (0.179) but a
  PSI of 0.0034 vs 0.0050 (drift_reports ids 1 and 4). Container rows store the container path
  of the report (`/app/reports/drift/...`, mounted to `reports/drift/`).

```
# host, with retraining (uses the MLflow champion):
set DATABASE_URL=postgresql://fraud:<password>@localhost:15432/fraud
.venv\Scripts\python -m src.monitoring.run_drift --window-start-day 141 --window-end-day 145
```

**First demo (2026-10-24, real data, before the top-feature rule).** All runs used the full validation stream in
`scored_transactions` (118,108 events, so now = day 183.0 and the label cutoff = day 153.0). The
config was unchanged between runs.

- Windows: champion training = days 1.00 to 141.12; challenger training (planned) = days 1.00
  to 146.00; holdout = (146.00, 153.00]; reference = days 111.1 to 141.1.
- The delayed-label check is the same in every run: (146.0, 153.0], 20,264 rows, 617 frauds.

| Run | Current window | Drifted features | Top drifted | PSI (score) | Delayed PR-AUC | Fired |
|---|---|---|---|---|---|---|
| 1 | days 141-145 (9,747 rows) | 5 / 28 = 0.179 | DeviceInfo, uid_txn_count_24h, uid_txn_count_7d | 0.0034 | 0.3238 | no |
| 2 | days 170-183 (34,502 rows) | 8 / 28 = 0.286 | uid_txn_count_1h, uid_txn_count_24h, DeviceInfo | 0.0027 | 0.3238 | no |
| 3, **synthetic** (TransactionAmt x2 on a copy of run 2's window) | days 170-183 | 11 / 28 = 0.393 | uid_txn_count_1h, uid_txn_count_24h, TransactionAmt, uid_amt_to_prior_mean | 0.0097 | 0.3238 | no |

Thresholds: share > 0.5; PSI >= 0.2; PR-AUC < 0.2992 - 0.05 = 0.2492.

- **Real drift grows with time** (0.179 to 0.286 of features), mostly in the short-window
  history counts and DeviceInfo. It stays below the threshold.
- **The score distribution barely moves** (PSI below 0.01).
- **Performance on matured labels is above the champion's validation PR-AUC** (0.3238 vs
  0.2992).
- **The synthetic shift is detected per feature** (TransactionAmt and the amount ratio), but it
  does not reach the dataset-level threshold.

No run fired, so the retraining and promotion path did not run on the real registry. It is
covered end to end on synthetic data by the integration test.

**Second demo (2026-10-24, with the top-feature rule; every other threshold unchanged).** The
champion's top 5 features by gain are `card3`, `card6`, `R_emaildomain`, `addr2` and
`ProductCD`. TransactionAmt is not among them.

| Run (drift_reports id) | Window | Share | PSI | Drifted top-5 features | Fired | Retrain / promotion |
|---|---|---|---|---|---|---|
| 1 (5) | days 141-145 | 0.179 | 0.0034 | card3 | **yes** (top-feature) | challenger v3, kept champion |
| 2 (6) | days 170-183 | 0.286 | 0.0027 | R_emaildomain | **yes** (top-feature) | challenger v4, kept champion |
| 3 (7), **synthetic** x2 | days 170-183 | 0.393 | 0.0097 | R_emaildomain | **yes** (top-feature) | challenger v5, kept champion |
| 4 (9), `--force-retrain` | days 170-183 | 0.286 | 0.0027 | R_emaildomain | yes, `forced = true` | challenger v7, kept champion |

What the second demo shows:

- **The new rule fires on every window, including run 1, which is close to training.** In run 1
  it fires on `card3` (Wasserstein 0.1041 vs threshold 0.1, just over the line), so it is noisier than the share
  rule.
- **In the synthetic run it fired on real drift in `R_emaildomain`, not on the 2x
  TransactionAmt shift**, because TransactionAmt is not a top-5 feature. The rule does not
  catch the shift it was added for.
- I did not change any threshold. Whether to keep the rule, raise its threshold or widen it is
  an open decision.

**Promotion (identical in every run: same training data, same seed).** On the holdout
(146.00, 153.00], 20,264 rows:

- challenger PR-AUC 0.3290 vs champion 0.3238;
- paired day-block bootstrap of the difference (1,000 resamples): mean +0.0053, 95% CI
  [-0.0160, +0.0254];
- the lower bound -0.0160 is below the 0.01 margin, so **the champion was kept**: `champion` =
  v2, `production` = v2.

Each fired run registered a new challenger version (v3 to v7; `challenger` = v7). Row 8 is an
earlier `--force-retrain` run, stored as `forced = false` because the trigger also fired. The
flag now means "the flag was given"; row 9 is the corrected run.

**Velocity features (days 141-145): checked, no feature bug.**

- An independent naive recount of `uid_txn_count_1h` / `_24h` from the raw file matches the
  drift job on 1,000 of 1,000 sampled rows.
- The scores production stored (Feast features) equal the champion's prediction on the drift
  job's batch features for all 9,747 rows (max difference 1.9e-9).
- The large late-window drift score is a heavy tail. Median, p90 and the share of rows with
  activity are almost unchanged, but the p99 of `uid_txn_count_1h` goes from 5 to 127 (mean
  0.45 to 3.07, std 1.1 to 18.2). The Wasserstein distance is scaled by the reference std
  (1.13), so a small set of very bursty uids dominates it. Which uids they are was not
  investigated.

## Setup (Windows, Python 3.11)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest --ignore=tests/test_integration.py   # unit tests, no services
.venv\Scripts\ruff check .
.venv\Scripts\python -m mypy        # strict type check of src/, tests/ and scripts/ (pyproject.toml)
```

Integration tests run against a **separate Compose project** (`docker-compose.test.yml`:
project `fraud-platform-test`, ports 25432 / 29092 / 26379, its own volumes, test-only
credentials), so they never touch the demo stack's PostgreSQL, Kafka or Redis:

```
docker compose -f docker-compose.test.yml up -d --wait
set TEST_DATABASE_URL=postgresql://fraud:test-only@localhost:25432/fraud
set TEST_KAFKA_BOOTSTRAP=localhost:29092
set TEST_REDIS=localhost:26379
.venv\Scripts\python -m pytest tests/test_integration.py
docker compose -f docker-compose.test.yml down -v   # removes only the test project's volumes
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
- Milestone 2 audit: point-in-time, split, encoder and online-parity checks with evidence
  ([docs/audit_milestone2.md](docs/audit_milestone2.md)).
- Milestone 3: real-time scoring slice: baseline model training, FastAPI `/health` and
  `/score`, Kafka producer/consumer, PostgreSQL persistence with duplicate handling, Docker
  Compose ([docs/serving.md](docs/serving.md)). Local demo only; see its limitations.

## How this project was built

I (Manojkumar M Hegde) own this project. I planned the milestones, set the
requirements and engineering rules, made the design decisions (for example the
model choice, the Feast feature source, the drift-trigger rules and the
reliability approach), reviewed every change and verified the results.

I used Claude Code (Anthropic) as an AI coding assistant to implement most of
the code, tests and documentation under my direction. Commits it helped write
carry a "Co-Authored-By: Claude" line.

## Documentation

Architecture, installation instructions, API documentation, test results,
and demonstration details will be added as development progresses.
