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
`.streamlit/config.toml` (copied into the image) sets `client.toolbarMode = "viewer"`, which
hides Streamlit's Deploy button and developer menu.

## Layout (`src/dashboard/`)

| Module | Role |
|---|---|
| `workflow.py` | Status transitions and the feedback label: pure functions, no I/O |
| `data.py` | **All SQL** (parameterised only). Every function takes a borrowed connection |
| `db.py` | Connections: every read or write gets its own connection; none is shared between Streamlit sessions (threads) |
| `ui.py` | Repository (one connection per call), read cache (`st.cache_data`, 15 s TTL, cleared after a write), analyst name, schema check, display labels |
| `fmt.py` | Display formatting, pure: times in IST, whole numbers without ".0", "missing" for blank fields, "—" for no label, dollar amounts |
| `app.py` | Entry point: `st.navigation` over the five pages |
| `views/*.py` | Thin pages: Overview, Alert queue, Case detail, Fraud rings, Drift. No SQL |

**Pages:**
- **Overview:**
  - one tile per status, in workflow order;
  - precision of reviewed alerts as "X% (fraud of reviewed)". Below
    `ui.MIN_REVIEWS_FOR_PRECISION` (30) reviewed alerts it shows "Too few reviews (n=N)", with
    a tooltip explaining why;
  - the production model: the model id (the first 12 hex digits of the model file's SHA-256)
    in large text, the full `model_version` of the newest scored row as a caption;
  - alerts per **day from the dataset start** (`TransactionDT // 86400`). IEEE-CIS TransactionDT
    is seconds from an unspecified start date, so days are relative. Creation dates would put
    every replayed alert on two days.
- **All pages:**
  - tables use plain column labels (`ui.COLUMN_LABELS`), and statuses are shown as
    "Confirmed fraud" etc.;
  - times are shown in IST, with "IST" in the label;
  - IDs and codes have no trailing ".0";
  - a review with no label shows "—";
  - the Overview chart's y-axis starts at 0.
- **Alert queue:** filter by status, score range and transaction-day range, highest score first,
  50 per page (`LIMIT/OFFSET` plus a count query). A selected row (or an alert id) opens the case.
  - The amount is in dollars with 2 decimals, and there is no Version column.
  - Tooltips and a caption explain that product codes are anonymised in IEEE-CIS, and that
    Card is the anonymised card key, not a customer.
- **Case detail, other alerts for this customer:** up to 20 other alerts with the same `uid`
  (card key + addr1), highest score first, with the total.
  - It is never grouped by card key, which many customers share.
  - When addr1 is missing (`…|<NA>`), a warning says the group is every customer of that card
    with no billing region.
  - Blank transaction fields show "missing".
- **Case detail:**
  - the transaction fields (the stored event), score, threshold and per-case model version;
  - status and version;
  - the linked ring, only when the alert's uid is a ring member;
  - the status-change form and the full review history:
    - the status dropdown offers the allowed next statuses only, and starts empty ("Choose a
      status");
    - Save is disabled, with the reason shown, until an analyst name and a status are set;
    - there is no `st.form`, so Enter and Ctrl+Enter never submit; Save submits only on a click.
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
| `submit_token` | TEXT NULL, UNIQUE: the one-time token of the form the review was saved from (NULL for rows written before it existed) |

  - Indexes: `(alert_id, id)`, `(transaction_id, id)` and the unique
    `ux_case_reviews_submit_token`. The token column and its index are an additive migration.
  - A trigger rejects row-level UPDATE and DELETE.
  - TRUNCATE does not fire row triggers; only the benchmark clone uses it
    (`scripts/bench_throughput.py`, guarded to `fraud_bench`).

**One-time form token** (`src/dashboard/case_form.py`;
docs/incidents/2026-10-26-unintended-reviews.md):

- **Creation.** A token is created when the form is first rendered for an alert. It records the
  alert id, version and status the analyst saw, and is never updated by later renders.
- **Use.** The token names the form's widgets and is passed to the Save callback, which saves
  against the token's seen version. `change_status` stores the token in `case_reviews` and
  rejects a token that was already used (`DuplicateSubmitError`). The unique index is the backstop
  for concurrent saves.
- **Retirement.** After a save (or a rejected one), or when the alert id changes, the token and
  the form's contents are discarded: the next render has a new token and an empty form, and a
  submit still carrying the old token is rejected.
- **Logging.** Every save attempt logs one line (alert, from, to, analyst, token, version,
  result) to the container log.

**Optimistic locking:** `change_status` locks the row (`FOR UPDATE`) and checks, in order:

1. the token is unused;
2. the version is still the token's seen version;
3. the transition is valid.

It then writes `status`, `version + 1` and `updated_at` and the review **in one transaction**.
If the version differs, it raises `StaleAlertError`: "This case changed since you loaded it:
alert N was changed by <analyst> at <time>; it is now <status> (version V, you had version W).
Reload the case ... nothing was saved". The page also warns before the save when the case
changed under an open form. Re-reading the version at submit time defeated the lock once
(docs/evidence/2026-10-26-lock-bug), and refreshing it on every render let stray submits through
(the 2026-10-26 incident).

## Tests

- **`tests/test_dashboard.py`** (unit, no database):
  - all 25 status pairs;
  - labels, unknown statuses and drift-report lookup;
  - AppTest smoke tests of every page with an in-memory repository, the case form save, the
    optimistic-lock message and the missing-schema error;
  - unintended saves (the 2026-10-26 incident). Each of these writes 0 rows: a second (stray)
    submit after a save, a save against a stale seen version, a save with no status, and an
    alert id change followed by reruns. Save is disabled without a name or status, the page has
    no form, a used token is rejected, and every save is logged.
- **`tests/test_integration.py`, "dashboard data access"** (test-stack PostgreSQL):
  - setup from 6 threads at once, then a rerun (constraint not recreated);
  - the upgrade of an old-schema database (rows kept, defaults added);
  - the append-only trigger, and TRUNCATE;
  - status change and review; lock conflict; invalid and anonymous changes; 8 concurrent
    threads on their own connections;
  - the submit token: its additive migration (old rows keep NULL); a resubmitted token, including
    one sent from 4 concurrent threads, writes once; a stale seen version and a missing status
    are rejected with 0 rows written;
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
- **Model version:** "production model" is the model id from the newest scored row's
  `model_version`, not the MLflow registry version. The bundle stores no MLflow run id, and the
  registry is a host-only SQLite file.
- **Single instance, 15 s cache:** reads are cached for 15 s per server process, and a write
  clears that process's cache. A second dashboard replica would show stale reads until its TTL
  expires; the optimistic-lock check is always made against the database, so no write is lost.
- **No connection pool:** one connection per call (18 ms connect inside the container). Fine
  for a few analysts; add psycopg-pool before scaling.
- **Kubernetes:** also runs in the local Minikube deployment (`k8s/dashboard.yaml`, PR #6,
  merged 2026-10-27): one replica (Streamlit session state is per pod), Service `dashboard:8501`,
  reached with `kubectl -n fraud-k8s port-forward svc/dashboard 8502:8501`. It reads the
  cluster's own small database (a 20-event replay), not the Compose demo database; fraud rings
  show as "not run in this deployment" (a host batch job).
