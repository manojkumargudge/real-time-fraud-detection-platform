# Demo walkthrough (Docker Compose demo stack)

A view-only walkthrough of the running demo, as it stands after the S7 sign-in (2026-11-15).
Nothing here writes to the demo database unless a step says so.

**Before you start**
- The demo stack has been streamed: 118,108 / 5,093 / 9 / 20 / 6 rows in
  `scored_transactions`, `alerts`, `drift_reports`, `fraud_rings` and `case_reviews`.
- `.env` holds `POSTGRES_PASSWORD`, `API_KEY` and `DASHBOARD_USERS` (README "Quickstart").
  The demo sign-in is kept outside the repo; never put it in a recording, a slide or git.
- Task Scheduler `FraudDrift-Retrain` stays **disabled** during a demo: a scheduled run adds a
  `drift_reports` row and can register a challenger.

## Steps

1. **Start** (repo root):
   ```
   docker compose start postgres redis kafka api consumer     # API healthy after ~1-3 min
   docker compose start dashboard
   curl http://127.0.0.1:18000/health                         # status ok, store_ok true
   curl http://127.0.0.1:18000/model                          # linked: true, registry v2
   ```
2. **API key** (read-only; the event is already stored, so the call returns `duplicate`):
   - `POST /score` without `X-API-Key` -> **401**;
   - with the key on a stored transaction (e.g. TransactionID 3459432) -> 200, `"duplicate":
     true`, the stored score.

   Do not post new transactions: `/score` writes them to the demo database.
3. **Dashboard sign-in:** open http://localhost:8501. Every page shows only the sign-in form
   until you sign in. Signing in as a `DASHBOARD_USERS` user makes that user the analyst; there
   is no free-text name field. A reload (F5) or a new tab asks again (README "Sessions").
4. **Pages** (view only):
   - Overview;
   - Alert queue: filter by status;
   - Case detail: e.g. case 1502, with the **Why flagged** panel and the review history (reviews
     made before 2026-11-15 keep their self-declared names);
   - Fraud rings;
   - Drift (reports 1-9).
5. **Do not save a review** unless you mean to: Save stays disabled until a target status is
   chosen, and a saved review appends to `case_reviews` (append-only, it cannot be removed).
6. **Sign out** (sidebar): back to the sign-in form; nothing replays the session.
7. **Afterwards:** check the five row counts above. If anything changed by mistake, stop and
   restore from the latest backup in `backups/` (a restore overwrites the demo database, so
   decide it explicitly).

## Optional parts

- **Streaming on a copy, not the demo:** `python -m scripts.bench_throughput setup` /
  `run` / `teardown` replays events into the `fraud_bench` copy
  ([evidence/2026-11-16-m2/](evidence/2026-11-16-m2/README.md) B1).
- **Kubernetes:** docs/kubernetes.md "Deploy" and "Verify"; the cluster has its own small
  database.

There is no recorded demo in the repo yet.
