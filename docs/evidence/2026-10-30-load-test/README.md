# Load test of `POST /score` on the bench copy (2026-10-30)

**A bench-copy measurement on one laptop, not a production number.** Real validation
transactions sent directly to the FastAPI `/score` endpoint of a temporary API container that
scores into `fraud_bench` (a copy of the demo database) with its own Feast Redis. The demo
database, the demo Redis and the demo API were not used (checked before and after).

## Why a copy

`/score` writes: for a transaction that is not stored yet it inserts into `scored_transactions`
(and `alerts` above the threshold) and pushes the customer/card state to the Feast online
store (`src/serving/scorer.py` `score_event`; `store.save`). For an already stored transaction it
only reads and returns the stored score with `duplicate = true`. Every validation transaction is
already in the demo database, so a test against the demo API would either measure that
read-only shortcut or write to the demo data. Hence the copy. Kafka is not touched by `/score`.

## Method

1. `python -m scripts.bench_throughput setup --replay 30000`: clone of the demo database as
   `fraud_bench` without its last 30,000 validation events (88,108 rows left), a separate Redis
   (`bench-redis`) with the Feast state built from the batch transactions before the cut, and 3
   bench consumers that stay idle (nothing is published to their topic).
2. A temporary API from the **same image as the demo API** (`585e02c00550`, model v2), bound to
   127.0.0.1 only:

   ```
   docker run --rm -d --name fraud-loadtest-api --network fraud-platform_default \
     -p 127.0.0.1:18001:8000 \
     -e MODEL_DIR=/app/artifacts/baseline \
     -e DATABASE_URL=postgresql://fraud:<POSTGRES_PASSWORD>@postgres:5432/fraud_bench \
     -e FEAST_REDIS=bench-redis:6379 -e FEAST_REPO_DIR=/tmp/feast -e FEATURE_SOURCE=feast \
     -v "<repo>/artifacts/baseline:/app/artifacts/baseline:ro" fraud-platform-api:latest
   ```
3. `python -m scripts.load_test_score --url http://127.0.0.1:18001 --run N`: the 30,000 replay
   events (real validation transactions, built by the producer's own code, in time order).
   Before each run it checks that none is in `fraud_bench` (0 present in all 3 runs). Levels
   1, 5, 10, 20 concurrent clients (httpx, asyncio, keep-alive), each 10 s warm-up + 60 s
   measured; only requests *started* in the 60 s count. Events come from one cursor and are
   never reused within a run; no level ran out of events. Every response is checked: a
   `duplicate = true` (not a fresh score) stops the run; there were none.
4. Between runs, `bench_throughput reset` put `fraud_bench` and `bench-redis` back to the
   post-setup state, so every run scores the same events fresh. Before every reset and the
   final teardown, the targets were printed and asserted to be the bench copy (`fraud_bench`,
   topics `bench-transactions*`, group `bench-scorer`, project `fraud-bench` Redis).
5. Runs 1 and 2 differed by more than 20% in the tail (p99) at c=1 and c=20, so a third run was
   made. All three are reported.

**Machine:** Intel Core i5-1235U (10 cores / 12 threads), 15.7 GB RAM, Windows 11, on AC power;
Docker Desktop VM with 12 CPUs and 9.7 GiB. The load generator, the API, PostgreSQL and Redis
all ran on this laptop, with the demo stack (API, 3 idle consumers, dashboard, Kafka) and the 3
idle bench consumers also running. Clipchamp was closed; Microsoft Teams, WhatsApp and Lenovo
Vantage were running in the background. CPU load before the test 29%.

## Results

Latency = client-side round trip of one `POST /score` (ms). Throughput = successful responses
per second in the 60 s window. Error rate 0.0% and duplicates 0 in every run.

| Concurrency | Run | Requests | Throughput (req/s) | p50 | p95 | p99 | max | Events used |
|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 2,038 | 34.0 | 25.7 | 49.5 | 95.8 | 164.2 | 2,385 |
| 1 | 2 | 2,166 | 36.1 | 25.9 | 40.4 | 50.5 | 87.5 | 2,546 |
| 1 | 3 | 2,245 | 37.4 | 24.8 | 39.2 | 50.9 | 165.3 | 2,598 |
| 5 | 1 | 1,326 | 22.1 | 214.3 | 333.2 | 393.4 | 1,094.5 | 1,537 |
| 5 | 2 | 1,301 | 21.7 | 219.3 | 336.9 | 400.8 | 932.4 | 1,529 |
| 5 | 3 | 1,321 | 22.0 | 216.4 | 325.8 | 368.2 | 460.7 | 1,530 |
| 10 | 1 | 1,193 | 19.9 | 469.2 | 750.0 | 1,139.9 | 1,763.3 | 1,398 |
| 10 | 2 | 1,172 | 19.5 | 484.8 | 703.0 | 1,173.0 | 1,618.7 | 1,388 |
| 10 | 3 | 1,216 | 20.3 | 482.7 | 645.6 | 762.8 | 1,075.1 | 1,421 |
| 20 | 1 | 1,248 | 20.8 | 933.4 | 1,226.1 | 1,373.4 | 1,547.0 | 1,479 |
| 20 | 2 | 1,067 | 17.8 | 973.3 | 1,383.3 | 6,562.1 | 6,829.7 | 1,262 |
| 20 | 3 | 910 | 15.2 | 1,026.7 | 1,705.9 | 16,936.4 | 20,585.8 | 1,135 |

(Events used includes the warm-up requests.)

## What it shows

- **One client:** about 35 req/s, p50 25 ms, p95 39-50 ms.
- **The knee is between 1 and 5 concurrent clients.** At c=5 throughput *drops* to about
  22 req/s and p95 jumps about 7x (to about 330 ms); from there latency grows roughly in
  proportion to concurrency (p50 about 215 / 480 / 975 ms at 5 / 10 / 20) while throughput stays
  at 15-22 req/s. More clients only add queueing.
- **Why:** the API's PostgreSQL store uses **one connection behind a lock**
  (`src/serving/store.py`, `PostgresStore`), so the dedupe read and the insert of every
  request are serialised; the endpoint runs in uvicorn's thread pool in one process, so the
  per-request Python work (Feast state read and push, features, XGBoost) also contends for one
  interpreter. Fixes (not made; serving is frozen): a connection pool, several uvicorn workers
  or API replicas.
- **Tail stalls at c=20 in runs 2 and 3:** 21 and 28 requests took over 3 s, clustered in a few
  seconds of the window (run 2 around s 32-36, run 3 around s 58-69); none in run 1. The API
  logged no error or warning and no PostgreSQL checkpoint started in run 3's window. The cause
  was not found; background load on the laptop is a candidate. Treat p99/max at c=20 as
  unreliable; p50 and throughput are stable across the runs (within 20%).

## How this relates to the 46.83 events/s figure

The README's **46.83 events/s** is end-to-end *streaming* throughput: Kafka -> 3 consumer
processes -> score -> PostgreSQL, each consumer with its own database connection and
interpreter (docs/serving.md "Throughput benchmark"). This test calls the HTTP API directly: one
API process with one database connection, no Kafka. The two are different paths and are not
comparable one to one; the API was built for single scoring calls, while bulk traffic goes
through Kafka.

## Limits

- One laptop: load generator, API, PostgreSQL and Redis share the same CPU and memory; network
  latency is loopback. Not a production number.
- A bench copy: `fraud_bench` starts at the 88,108 rows before the replay; the demo database has
  118,108 rows.
- **Out of order at concurrency > 1:** concurrent requests of the same customer can reach the
  Feast state out of time order, which the online state treats approximately (a known
  limitation, docs/feature_store.md). Scores on the copy may therefore differ slightly from the
  demo's; **latency is the measurement here**, not the scores.
- 60 s per level, 3 runs; the c=20 tail varied a lot between runs (see above).
- Client-side latency only; no server-side stage breakdown for this run.

## Files

- `latencies_run{1,2,3}.csv`: every request (run, concurrency, phase warmup/measure,
  transaction_id, start offset s, latency ms, HTTP status, duplicate, error).
- `summary_run{1,2,3}.json`: the per-level summaries above.
- Script: `scripts/load_test_score.py`.

## Before / after (checked)

- Demo database: 118,108 / 5,093 / 9 / 20 / 6 before and after; after teardown the PostgreSQL
  server holds only `fraud` and `postgres` (`fraud_bench` dropped).
- Demo Redis (localhost:16379): 57,911 keys, sha256 over every key's value `93874038…`, before
  and after: identical.
- Images unchanged: api `585e02c00550`, dashboard `7621290406d0`, consumer `9c282577c727`.
- The temporary API was stopped (removed with `--rm`); `reports/bench/setup.json` restored from
  `backups/bench-setup-2026-10-25.json` (identical hash).
