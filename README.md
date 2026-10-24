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
- Kubernetes (Minikube)

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
| pip-audit 2.10.1 | `requirements.txt`, `requirements-serving.txt`, `requirements-dev.txt`, `requirements-gnn.txt` (with its PyTorch/PyG indexes) | any known vulnerability not in `security/pip-audit-ignore.txt` |
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

## Setup (Windows, Python 3.11)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest --ignore=tests/test_integration.py   # unit tests, no services
.venv\Scripts\ruff check .
.venv\Scripts\python -m mypy        # strict type check of src/ and tests/ (pyproject.toml)
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

## Documentation

Architecture, installation instructions, API documentation, test results,
and demonstration details will be added as development progresses.
