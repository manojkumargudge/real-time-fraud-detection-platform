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
