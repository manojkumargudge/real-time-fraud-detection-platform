# Load test of `POST /score` on the bench copy, rerun (2026-11-11)

**A bench-copy measurement on one laptop, not a production number.** Rerun of the
[2026-10-30 load test](../2026-10-30-load-test/README.md) with the same script and API image,
under the conditions asked for in the audit: demo application stopped, at least 3,000 replay
events, 60 s window, c=1 and c=5.

## Conditions

| | 2026-10-30 | 2026-11-11 (this run) |
|---|---|---|
| Demo stack | API, 3 idle consumers, dashboard, Kafka, PostgreSQL, Redis running | **stopped** except PostgreSQL and Kafka (the bench needs them: `fraud_bench` lives on the demo PostgreSQL, the idle bench consumers join the demo Kafka); demo API, consumers, dashboard and Redis stopped |
| Bench | `bench_throughput setup --replay 30000` (88,108 rows kept) | `setup --replay 6000` (**112,108** rows kept; replay events from TransactionDT 15,627,808) |
| API | temporary container, image `585e02c00550` (model v2), 127.0.0.1:18001, `FEATURE_SOURCE=feast`, `bench-redis` | same |
| Levels | 1, 5, 10, 20 | **1, 5** |
| Window | 10 s warm-up + 60 s measured, 3 runs, `reset` between runs | same |
| Machine | i5-1235U, 15.7 GB, AC power, CPU 29% before | same laptop, **AC power** (checked), CPU 3-43% before run 1 |

Command per run: `python -m scripts.load_test_score --url http://127.0.0.1:18001 --run N
--levels 1,5 --warmup 10 --duration 60`; before every run the script checked that none of the
6,000 events was in `fraud_bench` (0 present, all three runs), and `bench_throughput reset`
restored `fraud_bench` and `bench-redis` between runs.

## Results

Error rate 0.0% and duplicates 0 in every run. Latency = client round trip (ms).

| Concurrency | Run | Requests | Throughput (req/s) | p50 | p95 | p99 | max | Events used |
|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 1,455 | 24.2 | 34.3 | 70.2 | 114.2 | 1,831.0 | 1,695 |
| 1 | 2 | 1,572 | 26.2 | 32.3 | 65.7 | 108.3 | 647.3 | 1,850 |
| 1 | 3 | 1,708 | 28.5 | 31.3 | 60.2 | 86.2 | 435.8 | 1,958 |
| 5 | 1 | 857 | 14.3 | 331.1 | 566.1 | 718.9 | 980.4 | 990 |
| 5 | 2 | 779 | 13.0 | 363.8 | 638.5 | 844.5 | 1,106.2 | 924 |
| 5 | 3 | 751 | 12.5 | 352.3 | 755.8 | 1,365.1 | 1,617.9 | 892 |

**Summary:** one client **24-29 req/s, p50 31-34 ms, p95 60-70 ms**; five clients **12.5-14.3
req/s, p50 331-364 ms, p95 566-756 ms**. The 2026-10-30 figures (one client about 35 req/s,
p50 25 ms; five clients about 22 req/s, p50 about 215 ms) were **not reproduced**: today's
numbers are 20-30% lower at c=1 and about 40% lower at c=5, with the demo app stopped.

The knee is unchanged: going from 1 to 5 clients lowers throughput and multiplies p50 by about
10, because the API's store is one PostgreSQL connection behind a lock
(`src/serving/store.py`) and the endpoint runs in one uvicorn process.

**Not explained.** Differences between the two days that could matter, none tested in isolation:
a later and larger replay slice (bench copy 112,108 vs 88,108 rows, so longer per-customer
history and larger tables), the laptop's background load (not controlled), and Docker Desktop
restarted the same morning. The demo app being stopped did not make it faster.

## Files

- `latencies_run{1,2,3}.csv`: every request (run, concurrency, phase, transaction_id, start
  offset, latency ms, HTTP status, duplicate, error).
- `summary_run{1,2,3}.json`: per-level summaries.
- Bench teardown afterwards: `fraud_bench`, bench topics, group and Redis removed;
  `reports/bench/setup.json` restored from `backups/bench-setup-2026-11-11-pretest.json`
  (sha256 `69881acb…`, unchanged).
