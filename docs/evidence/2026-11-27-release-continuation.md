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
