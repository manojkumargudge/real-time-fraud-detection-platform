# November 28 release validation

**Production verdict: NOT READY.**

Production Kafka consumers now disable implicit offset reset after explicit
assignment and leave the group if a reset error arrives during retry polling.
This closes a retention race during fetch. The original historical incident
trigger remains unproven. [Kafka reliability](../serving.md#reliability)

The full Windows unit rerun passed 745 tests with two environment-dependent
skips. All 54 PostgreSQL, Kafka and Redis integration tests passed. Strict
mypy passed on 121 files with the declared CI dependencies. Ruff, Bandit,
Compose rendering and Kustomize rendering passed. The local tracked-file
secret scan found zero matches, and CI now runs the same check.

The Linux serving image built successfully and passed an isolated champion
load and Kafka client configuration smoke test under UID 10001. Scoped
disposition of unfiltered image findings, matched sustained capacity, live
alert and recovery verification, and public end-to-end validation are open.
[Release checklist](../production-readiness.md)

The rebuilt proxy ran as UID and GID 10001 with zero effective capabilities
and no-new-privileges. Local HTTP, verified TLS and certificate persistence
across restart passed. Its unfiltered image scan had zero High or Critical
findings. The CI-aligned Windows unit suite passed 728 tests with four
environment-dependent skips. The earlier capacity image set used a different
proxy digest and remains historical for this proxy change.

The Pub/Sub verifier now skips unrelated or malformed payloads and accepts
wrapped base64 in addition to decoded JSON. Its six focused tests and the
734-test CI-aligned Windows unit run passed, with four environment-dependent
skips. Strict mypy passed across 122 files.

The requirements matrix records the supporting model, feature-store, dashboard
and MLOps evidence. The project owner approved the existing Streamlit dashboard.
[Requirements matrix](../aiml-01-completion.md)

## Checkpoint continuation

Draft PR #49 had 16 passing checks on `9b4b24f`. The PR image publication
job was skipped by its existing condition. The hosted benchmark's first
attempt measured a median one-client direct p95 of 15.275 ms against a main
p95 of 12.445 ms and a 13.690 ms limit. One direct repeat recorded Redis AOF
fsync spikes of 19 ms and 17 ms and a p95 of 18.24 ms. The other recorded
1 ms fsync samples and a p95 of 12.31 ms. The timing is consistent with
storage variability, with no per-request correlation proving the cause.
The same-commit rerun passed the unchanged limits. Main and direct median
one-client p95 were 21.715 ms and 17.385 ms. Median throughput was
66.59 and 68.08 requests/s at one client and 60.20 and 71.60 at eight
clients. All 18 rows had identical score digests and zero errors or timeouts.
[Benchmark attempts and artifact](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/46105941880)

The retained `release-1128c-results.tgz` has SHA-256
`7f1b62d5df9c17e58fc822c939a3ad6647598441674faa8c85d2267d91e286b0`.
The guest ran kernel `7.0.0-1018-gcp`. `main-1` and `release-1` both
finished spread-1, spread-4, spread-8 and hot-8. All eight segments had
zero HTTP errors, timeouts and duplicates. Each repeat reconciled 27,000
Kafka scores with zero oracle differences and zero dead letters. They
measured 57.05% and 56.05% CPU p95, respectively. `release-2` completed
only spread-1, then failed the zero-timeout gate with one warmup timeout.
Its remaining three segments, final reconciliation and all of `main-2`
are absent. The failed repeat needs a fresh start. The earlier `1128b`
archive contains preflight evidence only. The completed `1128c` workloads
apply to the retained manifest. Its proxy predates the non-root build.
The matched final-image sustained comparison remains open.

The controlled incident ran from 09:15:20 to 09:18:29 IST. The retained
Pub/Sub collector recorded `closed` at 09:25:54 IST. The earlier rollback
rehearsal recorded an `open` under a different policy. Same-incident
opening and closure delivery remains unverified.

Compute Engine still reports both VMs `TERMINATED`. Cloud audit operations
show three November 28 starts adding 138.80 minutes from request to stop
completion. Added to the previously documented 155.66 capacity-host
minutes, the compute and IPv4 estimate is about US$1.34. With the existing
US$0.22 transfer and telemetry allowance, up to about US$0.54 remains of
the cumulative US$2.10 cap, before any additional eligible charges.
The four-run workload alone requires 176 minutes, about US$0.80 at the
documented rate. Project billing linkage was confirmed. Current applicable
Welcome credit was not independently verified. The VM was left stopped.

The hosted Linux/amd64 inventory on `9b4b24f` confirmed zero Critical
findings and the same 13 High CVEs on serving, dashboard, drift and MLflow.
The optional retrain image carries nine additional High CVEs. Caddy, Kafka,
PostgreSQL and Redis had zero High or Critical findings. The per-CVE review
in the security checkpoint remains the disposition record. Absent verified
fixes or explicit release-owner approval for the exact publishable bytes,
the shared 13 findings hold the release. Keep retrain disabled under its
existing release policy. [Security checkpoint](../release-image-security.md)
[Hosted image inventory](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/46105941842)

## Timeout and notification follow-up

The only `release-2` timeout occurred during the spread-1 warmup,
04:38:23.751–04:39:23.751 UTC. The 15,562 measured requests succeeded and
had a maximum latency of 146.31 ms. Resource samples recorded zero observed
container restarts. The first guest probe found Kafka startup lag with a
healthy HTTP endpoint. Cloud Logging confirmed the Kafka-only flag at
04:38:24.948 UTC and all-zero component flags at 04:38:57.948 and 04:39:31.149
UTC. Subsequent early samples showed healthy HTTP, PostgreSQL and Redis and
Kafka lag of one. The database dump, Redis rewrite
and Evidently job were scheduled five minutes into the workload, outside
warmup. Those results leave a short-lived dependency, host or transport
stall possible. The archive lacks an individual timeout timestamp and
exception class. It establishes neither reproducibility nor environmental
causation. Preserve the failed run and the unchanged zero-timeout gate.

Cloud Monitoring readback confirmed an enabled policy with both OPENED and
CLOSED prompts. The notification-channel log had zero entries between
03:40 and 04:00 UTC, which leaves an individual publish outcome unverified.
The dedicated subscription has a 10-second acknowledgment
deadline and no acknowledged-message retention. The original collector
used `--auto-ack`, allowing the CLI to discard messages before filtering,
durable recording or recovery from a subprocess interruption. The missing
opening has no retained envelope, so the exact historical loss mechanism
remains unproven. The revised collector persists matching receipts before
explicit acknowledgment and resumes the same verification window after an
interruption. An isolated regression exercises a matching opening and
closure across a simulated interruption and rejects an unrelated incident
pair. Live Monitoring-to-Pub/Sub delivery still requires a fresh approved
controlled incident with one collector and a private evidence file.

## Cumulative capacity estimate

Iowa e2-standard-8 on-demand compute is US$0.26804568/hour. In-use standard
IPv4 is US$0.005/hour before any free allowance. At US$0.27304568/hour,
294.46 audited or previously documented capacity-host minutes estimate
US$1.34002. Retaining the US$0.22 prior transfer and telemetry allowance
leaves US$0.53998 under US$2.10. These are planning values rather than
billing-ledger balances. Existing disk retention is excluded by approval.
[Compute pricing](https://cloud.google.com/products/compute/pricing/general-purpose)
[IPv4 pricing](https://cloud.google.com/vpc/network-pricing)

| Final-image session | Compute and IPv4 | Shortfall against US$0.53998 |
| --- | ---: | ---: |
| 176 minutes of measured workload and warmup only | US$0.80093 | US$0.26095 |
| 210-minute operator window including startup, reconciliation and export | US$0.95566 | US$0.41568 |
| 230-minute currently configured cloud STOP | US$1.04668 | US$0.50669 |
| 240-minute cloud STOP ceiling | US$1.09218 | US$0.55220 |

Reserve a further US$0.02 for transfer and US$0.20 for telemetry at the
cloud STOP ceiling. The new-session planning ceiling is US$1.31219
rounded up, yielding a proposed **US$2.88 cumulative cap** before tax and
retained disks. That represents a US$0.78 increase to the approved cap.
Keeping the verified 230-minute setting bounds the same estimate to
US$1.26668 incremental and **US$2.83 cumulative** after rounding upward.
Confirm accumulated eligible charges, the approved STOP ceiling, trial status,
remaining applicable credit and expiry before start. Actual runtime, transfer,
telemetry and taxes determine the invoice. The estimate excludes production
hosting and release publication.
