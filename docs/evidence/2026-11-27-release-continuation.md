# November 27 release continuation

## Recovered evidence

- The authorized host is `fraud-release-capacity-1126`, instance
  `938503370005181237`, in `us-central1-a`. The guarded pilot verified eight
  visible CPUs, 33,646,927,872 usable bytes in Linux and Docker, 28% disk use,
  and all nine cached manifest digests. The legacy private Caddy stack
  conflicted with the rehearsal's loopback port. Stopping that legacy project
  preserved its volumes and allowed the replacement proxy to become healthy.
- The interrupted `release-1127c` archive SHA-256 is
  `5b837a156b727ed1e51ad630e60b5de46fe1fe81865a017b9bf7604225596476`.
  It records one completed ten-minute main level with 16,050 requests,
  26.75 requests/s, p50 36.94 ms, p95 44.02 ms, p99 48.00 ms, and zero HTTP
  errors, timeouts or duplicate responses. Its drift run failed because the
  synthetic current data had an entirely missing card2 column. The later
  `release-1127d` attempt reproduced that fixture failure and its archive was
  exported before stopping the VM. Neither attempt is a completed repeat.
- The hosted short benchmark at `ebf5497` failed the existing one-client
  p95 gate. Main was 70.075 ms and the candidate's direct checkpoint path
  was 79.48 ms, or 113.42% of main against the 110% limit. Redis fsync
  samples reached 422 ms. These observations do not establish a source-code
  cause. The job logged zero request errors or timeouts in those samples.
  [Failed hosted benchmark](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45973577229)
- The same head passed 717 unit tests with 11 skips and 54 integration
  tests. Strict types, lint, configured security jobs, Kafka, Feast, and
  coordinated recovery succeeded. Keep the failed benchmark visible when
  evaluating the overall check rollup.
  [Hosted regression](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45973577180)

## Corrections and independent verification

The sustained HTTP fixture now sends complete analyst and transaction
attributes and preserves the hot-card key. The drift input is generated from
the same cursor, with identity data in the separate IEEE-CIS identity CSV and
earlier transactions for the same entities to populate history features.
The preflight verifies that the new card keys are disjoint from the
champion's frozen feature seed. This repairs the fixture without lowering
the drift, sample size, reconciliation, or latency gates.

The local Linux drift container evaluated 1,000 scored synthetic transactions
with the corrected fixture. It persisted one PostgreSQL report and one HTML
report. The controlled input triggered feature drift (85.7% of features) and
prediction drift (PSI 17.4004). This verifies the drift job and report paths.

The hosted short benchmark at `2bf0990` failed its one-client throughput
gate. Across two runs per mode, median one-client throughput was 59.715 rps
for main, 44.125 rps for the candidate, and 40.650 rps for direct checkpoint.
The minimum was 95% of main. Median p95 was 34.605 ms for main, 56.315 ms for
the candidate, and 138.855 ms for direct checkpoint. The 18 batches (256
requests each) recorded zero errors and timeouts. The changing failure modes
and Redis fsync observations require diagnosis with the matched sustained run.
[Hosted benchmark and raw evidence](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/46004787701)

CI on `2bf0990` passed 718 unit tests with 11 skips and 54 service-backed
integration tests. Strict mypy checked 121 files. Kafka, Feast, coordinated
recovery and configured security checks passed. [Regression workflow](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/46004787695)

The isolated local kind deployment used Kubernetes v1.36.4. PostgreSQL,
Redis, Kafka, API, three consumers and dashboard rolled out. The four local
PVCs bound. PostgreSQL contained one score per each of 20 synthetic Kafka
transactions. Offsets per partition were 4, 6 and 10 with zero lag. API and
dashboard health passed, unauthenticated scoring returned HTTP 401, and an
authorized replay returned the existing score. This is the local Kubernetes
demo, with locally rebuilt images and the store-backed serving configuration.

The [champion calibration audit](../../reports/champion_calibration.md)
reproduced the original model PR-AUC and measured calibration on 118,108
validation rows. The champion and threshold remain unchanged.

The active Free Trial balance, expiry and no-out-of-pocket terms were
confirmed by the billing owner. Completed start/stop audit operations
through `1127d` represent about 50.5 minutes of VM time, or US$0.23 at the
documented compute and IPv4 rates. This estimate excludes variable usage and
comes from operation times. Persistent disk retention is separate. Immediately
before staging `1127e`, the Compute Engine API reported both VMs `TERMINATED`
and the capacity VM still configured for automatic `STOP` after 14,400 seconds.

Guarded run `release-1127e` entered `main-1` with cached manifests and the
corrected fixture. Its background drift job wrote report 1 with prediction
PSI 17.2029. This is an in-run drift check during the matched baseline.
The first pretraffic telemetry sample flagged Kafka before initial offsets
existed. An isolated librdkafka group reproduced the `- 0 -` tuple for three
assigned, empty partitions. A targeted parser test returned lag 0 for that
case, 15 for committed work with 15 records pending, and an error for both
uncommitted nonempty and unassigned partitions. The probe script was replaced
atomically with SHA-256 `9b4ed6de10e5fda9622666374e68a36970e6f53c8187a1c07e0fbab73f63de88`
before the candidate incident. The guest retains the previous script with
SHA-256 `23ecf141d3db1df474147c8b679e9637d2e631b17ca0e9d4394c417bc3bbfe4f`.
This change affects the telemetry interpretation of an active, empty group.

`1127e` finished all four main-1 segments and reconciled 76,968 score rows,
including 49,968 HTTP scores with warmup and 27,000 Kafka scores. Kafka
end and committed offsets were `[8547, 10020, 8433]`, with zero unexpected
dead letters. The main oracle reported zero score differences. CPU p95 was
59%, peak memory was 13.25% and peak filesystem use was 32% over 80 CPU
samples. The four 10-minute measured segments were:

| Pattern | Requests | HTTP requests/s | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Spread, 1 client | 12,859 | 21.432 | 46.03 | 58.20 | 66.76 |
| Spread, 4 clients | 11,727 | 19.545 | 201.55 | 269.85 | 313.02 |
| Spread, 8 clients | 10,321 | 17.202 | 459.91 | 607.50 | 674.05 |
| Hot card, 8 clients | 10,551 | 17.585 | 450.74 | 589.87 | 665.24 |

All four reported zero HTTP errors, timeouts and duplicates. At 19:36 IST,
systemd stopped and restarted the unit during candidate startup. The next
process exited on the existing session directory guard. The guard exported
the available evidence and stopped the VM, independently confirmed
`TERMINATED` along with the original rehearsal VM. The archive SHA-256 is
`9322f616c4b861cf7f9afd064f6be43a7eec1a6e8711da1b84c8db9fa88d6c5d`.
The resumed run verifies the archive and matched host, kernel, baseline image,
workload scripts and completed audit before reusing main-1. The candidate and
final baseline still require full sustained measurements.
