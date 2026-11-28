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

## Final-image staging after cumulative cap approval

Both GCP VMs were reconfirmed `TERMINATED`. The capacity VM is `e2-standard-8`
with a 13,800-second STOP schedule. No instance or project startup script
metadata was present in the readback. The rehearsal ingress rule permits only
IAP TCP 22. Billing linkage and the INR 450 alert budget were present.
The US$2.83 cumulative cap was approved separately from Welcome-credit
verification. The US$1.26668 incremental ceiling remains an estimate, and
the billing owner confirmed active eligible credit covering at least
US$1.27, expiry on February 8, 2027, and current billed cost of INR 0.00.

The staged rehearsal manifest preserves the historical `1128c` references
and replaces Caddy with the locally retained non-root Linux/amd64 build.
The registry response body hashes to
`sha256:8ba637eea93f4a170b3cd12099415203127e4d80671356e1797d6d042542f440`.
Its config and layer digests match the exported Caddy archive, which has
SHA-256 `0b3ca254d20f879c9b6de9dc2d35f994fc41e0cebb6eb7ac42ef3f5c9e78de4f`.
The unfiltered archive scan reported zero High or Critical findings.
The staged manifest SHA-256 is
`6bb171280f3b62b34a1379efe515d8c77d811a131286db9584347274d60c56e4`.
Verify every guest registry digest and its retained scan before load.
The earlier hosted registry references document different image bytes.
Production publication is still gated on exact retained digests and the
13 unresolved High findings.

The retained serving registry manifest and configuration hashes were
checked locally. Its ordered root filesystem diff IDs match the unfiltered
serving scan with 13 High findings. The staged dashboard, drift and MLflow
digests match their archived 13-High scans. The retained Kafka, PostgreSQL
and Redis digests each have zero High and Critical findings in their
archived scans. Synthetic Compose renders for both the staged and prior
fixture manifests produced nine digest-pinned services with the proxy as
the sole published ingress. These checks preserve the current release hold.

The production planning basis remains an Iowa e2-standard-8 at 730 hours
per month, 100 GiB of balanced disk and one in-use IPv4 address. The core
subtotal is US$209.3233464 before tax or credits. The existing modeled
snapshot, off-VM backup, registry, egress, DNS, secrets and operations
allowances bring the illustrative total to US$231.0133464 per month.
Actual usage and account billing determine charges. Production funding
is separate from the approved temporary rehearsal.
[Production cost assumptions](../production-readiness.md)

### Attempt `release-1128d`

The guest checked all four uploaded artifacts and pulled all nine staged
references by digest, including the non-root Caddy manifest. The baseline
image identity matched the prior evidence. Guest maintenance preflight
stopped the run before workload startup. The guest was on
`7.0.0-1018-gcp`, with no reboot-required marker and both APT units inactive.
The unattended-upgrade timer was due at 07:54:47 UTC, about eight minutes
after preflight and inside the required 184-minute maintenance-free window.
No capacity repeat or controlled incident started. The operator verified the
exported preflight archive, which has SHA-256
`85cea451eec057fa7e734548ca3ba6d779ed5836a3f9de14b8394575ef0d7d8e`.
The guard stopped the capacity VM, and independent readback confirmed both
VMs `TERMINATED` and the capacity VM still configured for `13800` seconds
and automatic `STOP`.

Instance timestamps bracket 5 minutes 17.508 seconds of runtime. At the
documented compute and IPv4 rates this adds about US$0.02408 to the prior
estimate. Keeping the same transfer and telemetry reserves, another full
230-minute bounded attempt takes the cumulative planning total to
US$2.85078 before tax and retained disks. Round the requested ceiling up
to US$2.86. Resolve the maintenance-window precondition and confirm credit
coverage and the new ceiling before the next start.

The owner subsequently confirmed at least US$1.27 of eligible Welcome
credit remained and approved US$2.86 cumulatively for one retry, with
the same February 8, 2027 expiry and 230-minute automatic STOP. The next
session reuses the checksum-verified guest image bytes and permits only a
bounded wait for scheduled maintenance to finish. It verifies both APT
unit results, the next timer windows and the reboot marker before workload.

### Attempt `release-1128e`

The cached digests passed guest preflight again. At 07:56:39 UTC, the
unattended-upgrade timer fired and temporarily reported no next event while
its oneshot service was starting. The preflight exited rather than treating
that transition as stable. A subsequent readback showed the upgrade service
inactive with result `success`. No sustained repeat or controlled incident
started. The archive contains the guest preflight log and was verified at
SHA-256 `1fab5f59e8b12a3aef1fdc65ad4688821f6565d369e04fb3835b56b952678ec0`.

The revised maintenance checker waits through systemd's activating state
and transient timer rescheduling. It requires both maintenance units to be
inactive with successful results, a full clean timer window, and no pending
reboot. The mock regression exercised the exact activation and missing timer
transition. Security timers remained enabled. Both VMs returned to
`TERMINATED` with the capacity VM still configured for automatic STOP after
13,800 seconds.

This attempt ran for 8 minutes 15.811 seconds, adding approximately
US$0.03761 at the compute and IPv4 planning rates. The cumulative planning
total with another 230-minute bounded attempt and the existing reserves
is approximately US$2.88839. A US$2.90 ceiling adds a small Operations
API timing reserve. A further start requires fresh owner authorization and
eligible credit confirmation.
