# Throughput acceptance protocol

The comparison uses two sequential runs per revision and 1, 4 and 8 concurrent clients. Compare the median of run-level metrics against main. At one client require throughput at least 95% of main and p95 at most 110% of main. At eight clients require throughput at least 95% of main. Report four-client results and p50/p95/p99 at every level. Require zero errors, timeouts, score differences, alert differences and history-count differences at every level. For a release decision repeat on the target Linux VM with durable stores and a representative sustained workload.

## Hosted profiling results

The first controlled hosted comparison used main `6f1a39f`, candidate `a5dae19` application behavior, and an isolated direct-serialization prototype. One Linux runner, one Uvicorn worker, one synthetic champion, 16 cards, 16 warmups and 96 measured requests per client level were shared across revisions. PostgreSQL and Redis were isolated, with Redis AOF and `appendfsync always`. Both repeats produced the same score, decision and history digest at every client level. All 1,728 measured requests succeeded without timeouts.

| Revision | Clients | Requests/s | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Main | 1 | 60.73 | 16.27 | 17.43 | 19.01 |
| Current candidate | 1 | 56.38 | 17.58 | 18.84 | 19.80 |
| Direct prototype | 1 | 70.74 | 13.89 | 15.29 | 17.94 |
| Main | 4 | 47.78 | 82.31 | 102.33 | 137.43 |
| Current candidate | 4 | 47.07 | 85.24 | 99.08 | 108.03 |
| Direct prototype | 4 | 64.10 | 61.92 | 76.20 | 83.66 |
| Main | 8 | 44.67 | 175.78 | 234.26 | 252.46 |
| Current candidate | 8 | 41.15 | 185.26 | 270.10 | 390.26 |
| Direct prototype | 8 | 61.56 | 127.38 | 165.55 | 192.93 |

The table reports medians across the two repeats. At eight clients, individual throughput was 44.02–45.31 requests/s on main, 40.79–41.50 on the candidate and 61.25–61.86 on the prototype. Candidate Feast conversion was 52.8–54.1 ms at p50 versus 0.19 ms for the prototype. Candidate Feast reads were 3.68–4.73 ms, Redis write calls 8.09–8.22 ms, advisory-lock SQL round trips 4.20–4.23 ms and PostgreSQL pool checkout 8.84–10.18 ms. The pending journal read, atomic save and journal deletion each took roughly 4–5 ms. Wall-clock stages can include scheduler delay and overlap with checkout or surrounding timings. Redis reported synchronous fsync latency events of 1–2 ms; the monitor's 1 ms threshold excludes faster events. Advisory-lock SQL round-trip time includes client/server work and is an upper bound on lock wait. At eight clients, the candidate process used 2.99–3.03 CPU seconds per sample and roughly 360 MiB peak RSS, while the prototype used 1.98–1.99 CPU seconds and roughly 359 MiB. These runs establish a conversion bottleneck under distinct-card load. They leave same-card contention and sustained full-stack sizing to the release rehearsal. [Hosted metrics and raw samples](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45560887469)

The resumed hosted comparison at `f3c72af` passed the relative gate with the enabled direct writer. All 4,608 measured requests shared one score, decision and history digest, with zero errors or timeouts. These medians are from the same Linux runner with two repeats per configuration and 256 requests per client level. The enabled writer used 2.84–3.23 CPU seconds and 384.7–384.9 MiB peak RSS per eight-client sample. The run exercised HTTP, PostgreSQL and Redis AOF. Final-image Kafka ingestion and whole-host headroom remain in the sustained rehearsal. [Hosted comparison and raw samples](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45638233044)

| Clients | Main requests/s | Enabled requests/s | Main p50/p95/p99 (ms) | Enabled p50/p95/p99 (ms) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 77.17 | 89.27 | 12.29 / 13.98 / 23.32 | 10.80 / 13.00 / 22.63 |
| 4 | 83.14 | 106.93 | 47.15 / 60.11 / 72.19 | 35.64 / 45.51 / 123.88 |
| 8 | 73.06 | 99.21 | 103.35 / 136.75 / 252.99 | 76.98 / 110.10 / 171.48 |
