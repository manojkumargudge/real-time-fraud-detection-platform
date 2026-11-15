# API connection pool: load test, replay and restart check (2026-11-15)

Branch `api-db-pool` (code at d1f1229): `PostgresStore` uses a `psycopg_pool.ConnectionPool`
(`DB_POOL_SIZE`, default 10, min 1, `check_connection` on checkout) instead of one connection
behind a lock. Writes are unchanged (`INSERT ... ON CONFLICT DO NOTHING`, score row and alert in
one transaction).

**Outcome: merged as a robustness change, not a performance change.** The pool removed the
lock, but throughput at 5 concurrent clients did not improve.

## Load test, `POST /score` (bench copy only)

Same method as [2026-10-30](../2026-10-30-load-test/README.md): `fraud_bench` clone of the demo
database without its last 30,000 validation events, separate bench Redis, a temporary API on
127.0.0.1:18001 (image built from d1f1229, `FEATURE_SOURCE=feast`, `DB_POOL_SIZE=10`). Levels 1
and 5 clients, 10 s warm-up + 60 s measured, 3 runs. New: `--skip N`. Each run used the next
unused slice of the replay events (skip 200, 2,989, 5,354), so no `bench_throughput reset`
(which deletes rows) was needed between runs. Before each run: 0 of its events already stored.

| Run | c=1 req/s | c=1 p50 / p95 ms | c=5 req/s | c=5 p50 / p95 ms | errors | duplicates |
|---|---|---|---|---|---|---|
| 1 | 27.9 | 30.4 / 66.4 | 13.8 | 334 / 630 | 0 | 0 |
| 2 | 21.4 | 40.6 / 87.2 | 13.2 | 344 / 639 | 0 | 0 |
| 3 | 15.0 | 53.6 / 135.8 | 7.6 | 605 / 1,089 | 0 | 0 |

Compared with the locked single connection on 2026-11-11 (same laptop, same method): c=1 24-29
req/s, p50 31-34 ms; c=5 12.5-14.3 req/s, p50 331-364 ms. **The 5-client throughput did not
change.** The pre-agreed bar for a performance claim (c=5 >= 1.5x the 2026-11-11 figure) was
not met.

**Laptop noise:** throughput fell from run to run (c=1 27.9 -> 15.0 req/s) with no code
change. In the same session the 200-event consumer replay ran at 22.9 events/s (scorer p50
104 ms), against 45.6 events/s (50.3 ms) on 2026-11-11. The figures are one laptop's, not
capacity numbers. Run 1, the fastest, also showed no c=5 gain, so the verdict does not depend
on the slow runs.

**Likely bottleneck (diagnosis, not proven):** at c=5 the API's own scorer latency was
p50 558 ms / p95 962 ms. The pool gave connections without waiting: peak 5 API connections, 0
errors or pool timeouts. `/score` is a sync endpoint on uvicorn's thread pool in **one
process**, and scoring (feature frame, Feast read/push, predict) is CPU-bound Python under the
GIL. More threads then add queueing, not throughput. Not verified with a profiler or with more
workers.

## Replay through the consumers (bench copy)

200 replay events through the 3 bench consumers (image from d1f1229), compared with the demo
database (read-only): demo 200 rows / 6 alerts, bench 200 rows / 6 alerts, 0 rows differ in
score, alert flag, threshold or model version (max |score diff| 0.0). 0 duplicate IDs, DLQ 0,
consumer lag 0.

## PostgreSQL restart (test stack only)

A temporary API on the test stack (`docker-compose.test.yml`, `FEATURE_SOURCE=store`) scored 3
new events. `docker restart` of the test PostgreSQL followed, then 3 more events, with the API
not restarted. One trial per version:

| Code | After the restart | Rows |
|---|---|---|
| main a867bea (one connection, lock, reconnect on error) | first request **HTTP 500** (`OperationalError: terminating connection due to administrator command`), store logged "reconnecting"; the retry and later requests 200 | 6/6 |
| api-db-pool d1f1229 (pool, `check_connection`) | all 3 requests 200 on the first attempt | 6/6 |

Both recover without restarting the API; recovery is **not** new. The difference is the one
failed request: the pool checks a connection before handing it out and replaces a dead one.

## Connection budget (demo PostgreSQL, `max_connections` = 100, 3 reserved for superusers)

Configured maximum per process: API 10 + 3 consumers x 10 + dashboard ~1 (one connection per
call) + drift job 1 = **42**. With the benchmark running as well (3 bench consumers x 10 + a
temporary API 10) = **82**, margin 15. Observed peak during the c=5 runs: 13 connections in
total, 8 on `fraud_bench` (3 consumers + 5 API). A consumer is one thread, so it holds 1
connection in practice. Kubernetes uses its own PostgreSQL (Minikube, default 100): 10 + 30 + 1
+ 1 = 42.

Raw output (gitignored): `reports/load_test/2026-11-15-pool/` (summaries, latencies, connection
samples, API logs, old-code restart log) and `reports/bench/s3-pool-200.*`.
