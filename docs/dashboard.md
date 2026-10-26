# Analyst dashboard (Streamlit)

A local analyst UI on the platform's PostgreSQL: the alert queue, case review with feedback
labels, fraud rings and drift reports. Analyst decisions go to an append-only table that
retraining reads (`latest_labels`).

## Run

```bash
docker compose up -d --build dashboard          # http://localhost:8501 (DASHBOARD_HOST_PORT)
# or on the host (repo root, Git Bash):
DATABASE_URL=postgresql://fraud:<pw>@localhost:15432/fraud PYTHONPATH=. \
  .venv/Scripts/streamlit run src/dashboard/app.py
```

The Compose service:
- built from the same Dockerfile as the API, with `REQUIREMENTS=requirements-dashboard.txt`
  (the serving runtime + `streamlit==1.66.1`, `pillow==13.0.0`), running as the non-root `app`
  user;
- on the demo network and database; it waits for the API to be healthy, because the API's
  startup creates the schema;
- healthcheck on `/_stcore/health`;
- `reports/drift` mounted read-only for the Evidently reports.

Type your analyst name in the sidebar once per session; writes are disabled until it is set.

## Layout (`src/dashboard/`)

| Module | Role |
|---|---|
| `workflow.py` | Status transitions and the feedback label: pure functions, no I/O |
| `data.py` | **All SQL** (parameterised only). Every function takes a borrowed connection |
| `db.py` | Connections: every read or write gets its own connection; none is shared between Streamlit sessions (threads) |
| `ui.py` | Repository (one connection per call), read cache (`st.cache_data`, 15 s TTL, cleared after a write), analyst name, schema check |
| `app.py` | Entry point: `st.navigation` over the five pages |
| `views/*.py` | Thin pages: Overview, Alert queue, Case detail, Fraud rings, Drift. No SQL |

**Pages:**
- **Overview:**
  - alert counts by status;
  - alerts per **transaction day** (`TransactionDT // 86400`). Creation dates would put every
    replayed alert on two days;
  - precision of reviewed alerts;
  - the production model version (`model_version` of the newest scored row).
- **Alert queue:** filter by status, score range and transaction-day range, highest score first,
  50 per page (`LIMIT/OFFSET` plus a count query). A selected row (or an alert id) opens the case.
- **Case detail:**
  - the transaction fields (the stored event), score, threshold and per-case model version;
  - status and version;
  - the linked ring, only when the alert's uid is a ring member;
  - the status-change form (allowed next statuses only, notes) and the full review history.
- **Fraud rings:** the stored rings with size and labelled fraud rate. The detail shows the
  member uids, their cards (the uid minus its last component), the shared devices and the
  members' alerts.
  - The rings come from the batch analysis of training-period data, so few live
    (validation-period) alerts link to them.
- **Drift:** the `drift_reports` rows (what drifted, the trigger reasons, retraining/promotion).
  - The Evidently HTML is embedded when its file is in the mounted folder, found by **file name
    only**: stored paths are Windows host paths and are not rewritten.

## Case workflow (`workflow.py`)

```
open            -> investigating | confirmed_fraud | false_positive
investigating   -> confirmed_fraud | false_positive
confirmed_fraud -> false_positive | closed      (correction)
false_positive  -> confirmed_fraud | closed      (correction)
closed          -> nothing
```

- **Labels:** the label follows from the target status: `confirmed_fraud` -> `fraud`,
  `false_positive` -> `not_fraud`, otherwise none. An analyst cannot record a label that
  contradicts the decision.
- **Corrections:** a correction writes a new review with the new label. A transaction's label is
  its **latest labelled review** (`data.latest_labels(conn)` -> `(transaction_id, label,
  reviewed_at)`), so closing a case keeps its label.
- **Rejected moves:** anything not in the table, including unknown statuses, raises
  `InvalidTransitionError` before any SQL runs.

## Schema (owned by `src/serving/store.py`)

The dashboard never runs DDL; if the schema is missing, every page shows a clear error.

- **Setup:** the API and every consumer run the idempotent setup at startup, in one
  transaction under `pg_advisory_xact_lock(7291406001)`, so concurrent startups are serialised.
- **`alerts` changes:**
  - `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` and `version INTEGER NOT NULL DEFAULT 1`
    are added. Existing rows only receive these defaults.
  - The status CHECK is widened to `open, investigating, confirmed_fraud, false_positive,
    closed`. It is dropped and re-added only when its definition differs, so a restart takes
    no ACCESS EXCLUSIVE lock. Existing `open` rows are unchanged.
- **The scoring path never touches analyst fields.** It inserts `(transaction_id, score)` only
  when the transaction itself is new, with `ON CONFLICT DO NOTHING`. A test replays a reviewed
  transaction and checks that status, score, timestamps, version and reviews survive.
- **`case_reviews`, append-only:**

| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `alert_id` | BIGINT NOT NULL -> alerts |
| `transaction_id` | BIGINT NOT NULL -> scored_transactions |
| `from_status`, `to_status` | TEXT NOT NULL |
| `label` | TEXT NULL, `fraud` / `not_fraud` |
| `analyst` | TEXT NOT NULL, non-blank |
| `notes` | TEXT NULL |
| `alert_version` | INTEGER NOT NULL (the alert's version after this change) |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |

  - Indexes: `(alert_id, id)` and `(transaction_id, id)`.
  - A trigger rejects row-level UPDATE and DELETE.
  - TRUNCATE does not fire row triggers; only the benchmark clone uses it
    (`scripts/bench_throughput.py`, guarded to `fraud_bench`).

**Optimistic locking:** the form carries the version it was rendered from. `change_status`
locks the row (`FOR UPDATE`), compares the version, validates the transition, then writes
`status`, `version + 1` and `updated_at` and the review **in one transaction**. If the version
differs, it raises `StaleAlertError`: "alert N was changed by <analyst> at <time>: it is now
<status> (version V, you had version W) ... nothing was saved".

## Tests

- **`tests/test_dashboard.py`** (unit, no database):
  - all 25 status pairs;
  - labels, unknown statuses and drift-report lookup;
  - AppTest smoke tests of every page with an in-memory repository, the case form save, the
    optimistic-lock message and the missing-schema error.
- **`tests/test_integration.py`, "dashboard data access"** (test-stack PostgreSQL):
  - setup from 6 threads at once, then a rerun (constraint not recreated);
  - the upgrade of an old-schema database (rows kept, defaults added);
  - the append-only trigger, and TRUNCATE;
  - status change and review; lock conflict; invalid and anonymous changes; 8 concurrent
    threads on their own connections;
  - pagination and filters;
  - `latest_labels` with a correction, and precision against hand-written SQL;
  - ring linking; the missing-schema report;
  - the replay of a reviewed transaction;
  - the benchmark cleanup on a `fraud_bench` with reviews.

## Known limitations

- **No authentication or authorisation:** the analyst name is self-declared, and anyone who
  can reach the port can change cases.
- **No scoring latency and no per-case feature explanations:** neither is stored by the scoring
  path.
- **Rings** come from training-period batch data, so few live alerts link to them.
- **Model version:** "production model" is the `model_version` of the newest scored row, not a
  read of the MLflow registry (the registry is a host-only SQLite file).
- **Caching:** reads are cached for 15 s per server process; a write clears that process's
  cache, but another dashboard replica would not see it until its TTL expires. The lock check
  is always made against the database.
- **Connections:** one per call, no pool: simple and thread-safe, about 1-3 ms of local connect
  cost per query. A pool (`psycopg_pool`) is the upgrade if traffic grows.
- **Not in Kubernetes yet** (planned for 2026-10-27).
