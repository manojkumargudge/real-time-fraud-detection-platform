# Compute Engine release checklist

The November 28 final-image rehearsal passed all four sustained repeats,
reconciled 108,000 Kafka scores, and verified live Pub/Sub opening and
closure for one incident. Both rehearsal VMs are stopped. The private
capacity result applies to the archived manifest set. The 13 unresolved
High findings still hold production release. The deployed rate-limit,
public TLS, and live deployment checks remain pending.
[Final rehearsal evidence](evidence/2026-11-28-release-continuation.md)

## Local deployment staging

Use the [environment template](../deploy/production.env.example) and two
copies of the [image-only override](../deploy/release-images.example.yml)
for rollout and rollback. Use literal private registry references ending in
full `@sha256:` digests verified against each signed inventory. Keep the
environment and overrides outside the checkout. Prepare independent
32-byte hex values for the PostgreSQL password, API key and dashboard
cookie, plus separate bcrypt dashboard entries. Use version-pinned Secret
Manager references and render a root-owned `/run/fraud/production.env`
with mode 0600 at startup. Use synthetic values for local validation.

After preparing the files, check both effective configurations:

```bash
export FRAUD_ENV_FILE=/run/fraud/production.env
stack=(bash deploy/compose.sh -p fraud-production --profile registry --profile monitoring)
candidate=("${stack[@]}" -f /etc/fraud/release-images.approved.yml)
rollback=("${stack[@]}" -f /etc/fraud/release-images.previous.yml)
for override in /etc/fraud/release-images.{approved,previous}.yml; do
  "${stack[@]}" -f "$override" config --quiet
  "${stack[@]}" -f "$override" config --images
done
```

Compare the reference list to the approved manifests and confirm only the
proxy publishes TCP 80 and 443. Keep the retraining profile disabled.
After approval, take the coordinated checkpoint described below. Quiesce
producers and ingress, drain consumers and the Feast journal, and capture
volumes, offsets, scores, alerts and the MLflow alias and model digest.
For a compatible rollback:

```bash
"${candidate[@]}" stop proxy consumer api dashboard drift
"${rollback[@]}" up --no-build --wait -d postgres redis kafka mlflow
# Verify checkpoint and restored stores before resuming writers.
"${rollback[@]}" up --no-build --wait -d api consumer dashboard proxy
```

An incompatible state change requires coordinated restore into fresh volumes.
For the public smoke test, run `deploy/verify.sh` against the approved DNS
names. Optionally pass a preapproved, unused synthetic transaction JSON
as the third argument and set `FRAUD_VERIFY_API_KEY_FILE` to a mode-0600
file. This writes one transaction and verifies replay of the same score
record. Produce a separate reserved Kafka fixture, then compare broker
end and committed offsets and verify one PostgreSQL score and its alert
decision, empty pending and dead-letter state, healthy Redis and the
expected model. Authenticate to the dashboard and open its case view.
Confirm matching monitoring incident opening and closure and complete the
rate-limit and recovery gates before sign-off.

**Verdict: NOT READY.** The Compose overlay is a release candidate. PostgreSQL-backed scoring serializes Feast updates per card and journals pending Redis updates with the score. Kafka retries an interrupted update before committing the offset. The late-event policy rejects events whose timestamp precedes the current entity state. Kafka uses its bounded permanent-error retry and dead-letter flow, while HTTP returns 409. Same-timestamp events still use strictly earlier history.

| Release gate | Evidence or remaining work |
| --- | --- |
| Model and dataset baseline | The documented champion is `baseline-9fdad27ea3b0`, validation PR-AUC 0.2992, threshold 0.12806208431720734. The documented dataset has 590,540 labeled transactions. See [serving](serving.md) and [model comparison](../reports/model_v1_vs_v2.md). |
| Existing regression evidence | Main's November 16 run recorded 726 passing unit tests, one skip and 44 passing integration tests. The archived isolated replay matched all 200 scores and alert decisions. See [milestone evidence](evidence/2026-11-16-m2/README.md). |
| API and analyst access | Production mode requires a 32-character key, protects model metadata, disables API docs and uses one token bucket across concurrent requests in the single API worker. Caddy publishes only scoring, health and the analyst dashboard. Dashboard sign-in expires after 30 minutes of inactivity at the next interaction. All configured dashboard users are analysts with the existing review permission. |
| Feast and Kafka | PostgreSQL session locks serialize the Redis read through the post-commit update by card. The recovery journal supports idempotent retries after partial writes. Kafka partitioning stays by card. PostgreSQL/Redis regression coverage includes duplicates, timestamp ties, late arrivals and partial-write recovery. |
| Security scans and dependencies | See [security exceptions](security.md). Keep existing exception expiry gates. Verify the registry and reverse proxy images in addition to serving and dashboard. Feast still requires a compatible patched upgrade or a reviewed mitigation for each applicable finding. |
| Linux deployment | Compose overlay, private backend ports, HTTPS, persistent volumes, least-privilege application containers and a daily drift-only job are in this branch. Validate a clean Linux build and exercise TLS issuance, authenticated dashboard access and transaction-to-dashboard behavior before release. |
| Model registry and retraining | MLflow has private HTTP access and persistent Linux SQLite and proxied artifact storage. Champion import, restart persistence, report lookup and a clean Linux retraining run passed on synthetic fixtures. Rehearse the full authorized dataset before enabling automated retraining. |
| Performance | Compare the same inputs, warmup, client levels, duration and host resources on both commits using isolated stores. Capture p50, p95, p99, throughput and error rate. Existing benchmarks are historical and cannot establish the impact of the new lock and limiter. |
| Recovery | PostgreSQL dump and isolated restore scripts are supplied. Exercise both and capture row counts before and after. Validate coordinated Kafka, PostgreSQL and Redis recovery plus MLflow/model restoration. |
| Observability | Health and redacted scoring logs are available. Verify live Kafka lag, pending Feast updates, error counts, throughput, failure alerting and drift visibility under an isolated load. |
| Kubernetes | The local kind regression rolled out six deployments and reconciled 20 synthetic Kafka scores with all three partitions drained. Its [evidence](evidence/2026-11-27-release-continuation.md) covers readiness and authorization. Production Kubernetes deployment remains outside this release. |

## Local evidence

### Outstanding release gates

| Blocker | Severity | Observed failure or evidence gap | Next change or verification | Acceptance criteria | Status and evidence |
| --- | --- | --- | --- | --- | --- |
| Kafka acknowledgement and ownership | P0 | The original consumer could advance past an offset gap. The archived broker and client logs do not establish what caused the missing deliveries. | Use a single broker position snapshot for production assignment and gap tracking. Check commits against broker watermarks and preserve manual acknowledgements. Exercise actual coordinator outages, reassignment, crash windows and drain. | No acknowledgement across unresolved records and exact score and alert counts after replay. | Assignment and bounds regressions added. See `tests/test_streaming_reliability.py` and the isolated replay evidence below. |
| Redis and PostgreSQL recovery | P0 | Redis snapshots can omit or roll back history while PostgreSQL retains durable scores. Training-only history requires its original seed. | Promote a fully verified Redis namespace and fence inference against PostgreSQL row digests. Rebuild from the pinned seed and durable scores. | Later transactions see the same history as the oracle. Pending entries drain after faults and restart without double application. | Service-backed tests cover stale and missing rows, failed promotion and batch parity. Local startup verified the full training seed. See the checkpoint evidence below. |
| Complete forensic reconciliation | P0 | The first archive covered only the committed offsets. The offline broker disk retained 36,803 more records. | Verify every Kafka batch and replay the full export into isolated stores against independent batch feature and model output. | Exact offset boundaries, unique and duplicate counts, zero unexplained score or alert differences, zero outstanding lag, pending updates and dead letters, and reproducible digests. | Passed for the retained cohort. All 58,468 distinct scores and 2,430 alert rows reconciled. See the full replay evidence below. |
| Sustained capacity | P1 | Four-vCPU rehearsal CPU reached 100%. On the approved eight-vCPU VM, an automatic guest package update interrupted the first candidate setup after main-1 completed. The VM booted a newer kernel at the next verified preflight. | Run two final-image and two matched main repeats in one guest maintenance window after credit approval. | Meet the throughput protocol, preserve all counts and digests, and pass the documented 70% resource thresholds. | Open. The completed main-1 on the previous kernel and the hosted short comparison are useful historical measurements. [Run evidence](evidence/2026-11-27-release-continuation.md) |
| Exact-image security and recovery | P1 | High-severity image findings require disposition. Previous recovery runs used small synthetic cohorts. | Review remaining findings against the exact publishable images and rehearse restore and rollback with the final release candidate. | Required scans and CI pass, all applicable findings resolved or explicitly accepted, and verified restored state agrees across stores. | See [image review](release-image-security.md) and the recovery results below. |
| Deployment and live evidence | P2 | The private rehearsal had no public endpoint. Compute Engine inventory showed the rehearsal VM stopped on November 26. | After the preceding gates and cost approval, validate production ingress, TLS, synthetic workload, recovery and alert delivery. | External authenticated checks and oracle comparison pass. Only Caddy is public, and resource usage and costs are bounded. | Blocked on P0 and P1. The stopped VM retains a 100 GB balanced disk. |

### November 27 continuation

The [latest private evidence](evidence/2026-11-27-release-continuation.md)
records the completed 44-minute main baseline, kernel qualification failure,
guarded shutdown, passing local drift and Kubernetes runs, and current CI.
The [sustained runbook](capacity-rehearsal-approved-run.md) records the next
required credit envelope. The [CVE matrix](capacity-cve-disposition.md)
records open security decisions and the two curl scanner discrepancies.

The [November 28 checkpoint](evidence/2026-11-28-release-continuation.md)
records the production offset reset fix, regression results and remaining
release gates.

- The later private Linux export includes nine image manifests and a trial host snapshot. It contains no completed workload samples, Kafka audit, oracle output or completion marker. The host snapshot's `nproc` value was 1. The archived driver set `OMP_NUM_THREADS=1` before running `nproc`, so that value cannot establish the VM's effective CPU allocation. The exit point and startup failure, if any, remain unverified.
- Local Ubuntu WSL2 and its Docker daemon both reported 12 logical CPUs and 10,428,297,216 bytes of memory. The capacity preflight rejected that configuration. A qualifying dedicated Linux host still needs two complete repeats, including active ingestion, checkpoint and drift, with the 70% CPU, memory and disk gates.
- The read-only frozen-cohort command below verified 16 checksums and independently reproduced 21,665 distinct committed records and 917 oracle alerts. The frozen database had 10,415 missing rows, 3,563 score discrepancies and 37 differing alert decisions. The separate full retained-cohort replay reconciled all 58,468 records and 2,430 alerts with zero unexplained differences. Both results describe different stores. The original evidence remains frozen and the initial delivery-gap root cause remains unproven.

To reproduce the read-only committed-cohort audit from the repository root, run
`python scripts/audit_frozen_cohort.py tmp/kafka-forensics-edd730b --threshold 0.12806208431720734`.
The audit verifies the source manifest, matches Kafka offsets and payload IDs to the SQL export,
and reconstructs the archived SQL digest from the previously generated batch oracle. Its output
contains aggregate counts and SHA-256 values. The archived group positions were 9,145 / 8,760 /
3,760 and the corresponding log ends were 20,201 / 17,527 / 20,740. The 21,665 archived records
had 21,665 distinct IDs. The archived batch output reconstructed
`804a580ce0005d3448c56df11a5a4ecb8e298408bb8fafa473d672fad81e5a61` with 917 alert decisions.

### November 26 candidate verification

- `9385799` passed 737 local unit tests with one configured skip and all 52 local integration
  tests against isolated PostgreSQL, Kafka and Redis. Hosted CI passed 712 unit tests with 11
  environment-dependent skips, all 52 service-backed tests, strict mypy, Ruff and the configured
  security jobs. [Hosted CI](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45733807553)
- The exact-head two-transaction recovery fixture had identical score and alert records, Redis
  digest, champion registry data, and committed Kafka offset 2 in source, restore and rollback.
  Cold backup took 2 seconds, verified restore 31 seconds, and rollback 10 seconds. Zero fixture
  transactions were lost. [Hosted recovery](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45733807576)
- The hosted HTTP throughput gate passed with matching result digests for 4,608 requests.
  See the [matched comparison](throughput-acceptance.md). Full-stack CPU and storage headroom
  still need a dedicated sustained trial.

### Full retained Kafka cohort

The original Docker disk remained offline. A read-only extraction produced copies of the three
Kafka transaction segments. The VHDX reader reported an unreplayed disk log, and the copy's
broker recovery checkpoints were unreadable. The audit decoded the transaction segments
directly, verified every batch CRC-32C and matched every key and payload at the 21,665 previously
archived offsets. It used the frozen group report for original commits and confirmed the
record boundaries through all three archived log ends.

| Partition | Original next commit | Verified log end | Additional records | Replay next commit |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 9,145 | 20,201 | 11,056 | 20,201 |
| 1 | 8,760 | 17,527 | 8,767 | 17,527 |
| 2 | 3,760 | 20,740 | 16,980 | 20,740 |

- The retained segments contained 58,468 records and 58,468 unique transaction IDs. The
  partitioned export SHA-256 was
  `1cd00d52b2a0f24125d3bec696c27bfa8bef92e1e906e29241b8f501e53ec618`.
  Replay preserved keys, values, headers and per-partition offsets in a fresh broker.
- The batch oracle used the offline feature implementation on the same cohort plus the
  original seed history. The champion was `baseline-9fdad27ea3b0` at threshold
  `0.12806208431720734`. Windows and Linux produced identical encoded feature matrix
  SHA-256 `24fccbf46c5f043b9293047026ec52d17d2902a542ee6c91cfe1e4548c24bcb5`
  and raw margin SHA-256 `7a0c18b89df17922ada4fff04f279b385b9a136ea56299bac06dfe0d39fa1b32`.
  The platforms differed in 44 batch probabilities by at most two float32 ULPs
  (`2.98e-8` absolute). Alert decisions agreed for every record.
- All 12,380 Windows and 46,088 Linux persisted scores matched the corresponding
  runtime's batch oracle exactly. This accounted for 35 differences with the Windows
  reference after handoff. There were 2,430 alerts and zero unexplained score or alert
  differences, missing or duplicate scores, unexpected records, pending updates, dead
  letters or residual lag. The sorted SQL score and alert digest was
  `ded8d0f78821004840d6bd9c1ce060a0b770314aa366bbbff3709a97833c3f6b`.
- The replay included abrupt consumer loss and reassignment, a Kafka restart that produced
  `NOT_COORDINATOR` and `COORDINATOR_LOAD_IN_PROGRESS` commit retries, a temporary Redis
  pause and a PostgreSQL restart during active scoring. A replacement consumer recovered
  a pending Feast entry. The final oracle and drain checks passed after these interruptions.
- The cache rebuild read the drained PostgreSQL checkpoint and folded 58,468 durable
  scores into the 472,432-event seed. The patched Redis instance rebuilt all 55,706 keys.
  All 12 history features matched exactly for 494 sampled entities before and after a
  restart, with probe digest
  `133bff428f38d3caa4dfb8d1d1452a35498140e06342553f25cd125e5815bb3e`.
  See the [rebuild runbook](feature_store.md#rebuilding-online-state-after-redis-loss).

The raw export, decoder, oracles and final audit output are retained locally under `tmp/`.
The aggregate results above require no publication of payloads or transaction identifiers.

- Fetched main passed 726 local unit tests with one skip. The candidate passed 728 local unit tests with one skip and all 46 integration tests against isolated PostgreSQL, Kafka and Redis. Ruff lint and formatting and strict mypy passed. The integration suite covered train/serve parity, replay idempotency, late-event handling, drift reporting and promotion safeguards.
- The November 25 integration suite recorded 46 passes and one failure. The failed Kafka round trip uses the in-memory store and received zero scored messages before the test ended. It passed on an isolated rerun. The new PostgreSQL integration test confirmed that a rejected recovery-journal insert rolls back both the score and alert and that the score can be retried after the existing journal entry is cleared.
- The recovery regression injected a Redis failure after the UID write. A replay through a separate PostgreSQL scorer finished the card write, returned the original score, cleared the journal and counted the event once for later scoring. The concurrent same-timestamp and late-event regression passed against the isolated PostgreSQL and Redis containers.
- The first Compose rehearsal verified TLS using Caddy's local CA, an authenticated score and duplicate, HTTP 401 without credentials, and HTTP 404 for API docs and model metadata at ingress. Kafka delivered three synthetic events, including a duplicate, and persisted two scores. The dashboard data layer read the expected model version and one alert.
- The PostgreSQL scripts restored three scored rows from a three-row source into a separate empty project. The private MLflow server imported `baseline-9fdad27ea3b0` as registry version 1 and verified the downloaded model and metadata byte for byte. This rehearsal used fresh Compose volumes.
- The rebuilt stack started with PostgreSQL 16.16, Redis 7.4.12, and Kafka 3.9.2. After the service restart and image changes, Caddy served an authenticated score that saw one prior Redis event. The journal drained to zero rows, Kafka kept offset 3 with zero lag on the exercised partition, and MLflow still pointed `champion` to version 1.
- Linux Feast materialized 2,700 synthetic pre-stream events into 120 UID and 120 card states. Standalone Evidently drift processed 1,100 scored fixture rows, persisted its HTML and database row, and the dashboard resolved the report. The full-dataset rehearsal was stopped under local memory pressure and produced no report.
- The November 25 rehearsal loaded all 590,540 authorized transactions and 144,233 identity rows. Feast materialized all 472,432 pre-validation events into 39,087 UID and 13,792 card states using the production Compose Redis persistence settings. The separate MLflow registry imported the verified champion as version 1. Materialization is the offline baseline for replaying the 118,108 validation events.
- The production Compose images built and the isolated stack reached healthy status. The first 200 real validation events went through Kafka, PostgreSQL and Feast. A separate synthetic score and duplicate passed over Caddy HTTPS with the local CA. Ingress returned 401 without the scoring key and 404 for protected documentation and model paths. PostgreSQL held 201 scores and zero pending Feast updates after the sample.
- A coordinated stop and restart of the scoring and backing services took 244.4 seconds in the local rehearsal. The 201 persisted scores had the same score digest before and after, with 20 alerts, 52,884 Redis keys, no pending journal entries and no lag at committed Kafka offsets 66, 60 and 74. The MLflow champion alias responded successfully on both sides. A PostgreSQL backup restored 201 scores and 20 alerts into an empty Compose project with the same score digest. The separate full-stack backup restore gate remains open.
- The original Kafka rehearsal stopped at next committed offsets 9,145, 8,760 and 3,760. An offline check verified all 16 archived SHA-256 entries and matched all 21,665 archived payload IDs to their exported SQL reconciliation entries. The frozen export contains 11,250 scores, 529 alerts and 10,415 missing scores. The missing ranges cover offsets 1,423–6,806 on partition 0 and 1,410–6,440 on partition 1 and end immediately before leader epoch 16 begins on each partition. The old consumer could commit across a delivery gap after a coordinator retry. The original logs cannot distinguish what the client fetched, buffered or delivered to Python during the transition. Keep the frozen group and stores intact.
- The isolated replay reconciled the entire committed cohort, including all 10,415 IDs missing from the original SQL export. At commits 9,145 / 8,760 / 3,760, the archived audit recorded zero lag, zero duplicates, zero unexpected scores, zero pending Feast updates and an empty dead-letter topic. All 21,665 scores and alert decisions exactly matched the independent batch oracle, and all 917 alert rows were distinct. The replay SQL row SHA-256 was `804a580ce0005d3448c56df11a5a4ecb8e298408bb8fafa473d672fad81e5a61`. The original stores remain frozen for forensics. Its broker snapshot also contains 36,803 records beyond those commits, outside this replay's verified cohort. [Replay report](https://github.com/manojkumargudge/real-time-fraud-detection-platform/pull/49)
- Reconstructing the SQL row digest from the sorted batch oracle and champion threshold independently reproduced the archived replay digest. Comparing the original SQL export to that oracle found 3,563 retained scores and 37 alert decisions that differed, all after the gaps. Partition 0 had 1,829 score differences, partition 1 had 1,734 and partition 2 had zero. Recovery of the affected cohort requires ordered replay into isolated stores and full score, alert and feature-state reconciliation before any state replacement.
- A separate deterministic regression reproduces a failed seek for a buffered record polled during transient retry backoff. The prior loop logged the seek failure and could remain assigned with the record skipped until restart. The consumer now leaves the group on seek failure, and the regression verifies that the next consumer scores both records in order from the unchanged broker position. The archived incident does not establish a seek failure.
- The retraining image built successfully. On a fresh Linux-native Git checkout, the clean-tree guard passed and the Feast and pandas retraining matrices matched exactly across 3,800 rows and 31 columns, with the same SHA-256 on both sides. On the synthetic holdout, challenger PR-AUC was 0.8262 and champion PR-AUC was 0.4004. The job moved `champion` to version 2 within the isolated registry and kept the protected `production` alias at version 1.
- Bandit reported no medium or high findings. The unfiltered local Python environment audit reported 41 advisory entries across eight packages, including local notebook and tooling dependencies. Trivy initially reported 103 high and four critical entries per serving and dashboard image. The entries with a listed fix were the three Feast advisories and one PyArrow advisory. The older embedded Feast restrictions still apply. [Feast 0.67 package metadata](https://pypi.org/project/feast/0.67.0/), [critical Feast advisory](https://github.com/advisories/GHSA-gg2p-37pv-v7fq).
- The official package metadata for patched Feast 0.54, 0.63 and 0.67 requires NumPy 2, while this repository pins NumPy 1.26.4. The release candidate uses embedded Feast with a local registry generated by the application. It exposes neither the Feast feature nor registry server and accepts only trusted local feature inputs. The hardening does not change the current exception list or expiry gates. [Feast registry advisory](https://github.com/advisories/GHSA-q63x-9pfm-mjx4), [Feast Kubernetes compute-engine advisory](https://github.com/advisories/GHSA-34wm-4hw7-qfjv).
- The infrastructure rescans found zero high or critical findings in Redis 7.4.12 and the patched Kafka Alpine OS layer. The original Kafka 3.9.2 OS layer had 44 high and six critical findings. Kafka's Java scan remained incomplete after the database download retry failed with an OCI stream protocol error. PostgreSQL 16.16 had 26 high and one critical finding, with 26 entries attributed to the bundled `gosu` startup helper. The private MLflow image had 101 high and two critical findings with no fix listed by this scan.
- The November 25 Trivy retry downloaded the mirrored Java advisory database, then hit its scan deadline before producing a Kafka report. The local API and dashboard rescan also remained incomplete after cache contention and a scanner exit. The latest hosted security pass applies to `54fd016`. Local Bandit, the serving dependency audit with its six existing ignores, and the exception-expiry check passed. The Feast and container exceptions and release gates remain in force.
- Caddy 2.11.9 passed configuration validation and reduced the scan to two high and zero critical findings, from 99 high and six critical on 2.10. The remaining alerts involve HTML parsing and JWK parsing libraries. The configured HTTP routes only proxy requests and responses, with no HTML templating or JWK authentication handlers. Complete the reachability review before publishing the proxy. [Caddy release](https://github.com/caddyserver/caddy/releases/tag/v2.11.9), [Go HTML advisory](https://pkg.go.dev/vuln/GO-2026-6860), [Go JOSE advisory](https://github.com/advisories/GHSA-mgcc-cww5-fp2q).
- Gitleaks scanned 145 commits across local refs with redaction enabled. All six findings point to three test API-key literals introduced in two commits. The file-only history check found no commits under `data/raw`.
- `kubectl kustomize k8s` rendered 18 objects. The application manifests require a non-root runtime and omit automatic service-account token mounts. The running Minikube stack still needs a deployment regression.

### Local API benchmark

Each run scored all 96 measured synthetic requests after 16 warmups across 16 cards, using the same champion, Windows Python environment, PostgreSQL pool size and isolated Docker PostgreSQL and Redis services. The client called the FastAPI application in-process. Latencies cover the application and its datastore calls. TLS, proxy overhead and production Redis synchronous AOF were outside this benchmark.

| Revision | Clients | p50 ms | p95 ms | p99 ms | Requests/s | Error rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fetched main `6f1a39f` | 1 | 265.85 | 562.24 | 1,750.36 | 3.02 | 0% |
| Recovery candidate | 1 | 674.55 | 4,098.54 | 10,811.49 | 0.79 | 0% |
| Fetched main `6f1a39f` | 8 | 579.58 | 4,217.97 | 5,794.07 | 7.93 | 0% |
| Recovery candidate | 8 | 3,911.32 | 11,837.45 | 16,130.81 | 1.42 | 0% |

The candidate was slower in this run. The journal adds PostgreSQL operations and the host was also building images and running tests under memory pressure. Use a dedicated Linux load test to quantify the overhead and establish the release latency and throughput limits.

### November 25 Linux follow-up

The follow-up used a four-CPU Linux container under WSL2, Uvicorn over loopback HTTP, the same champion, fresh PostgreSQL schemas and Redis namespaces, and the default ten-connection pool. Each row is a separate run of 96 measured requests after 16 warmups against 16 cards. Each run scored all 96 requests without errors, and all runs produced the same digest of scores, decisions and history counts for both warmup and measured events. These runs used the integration images and Redis defaults. They excluded Caddy, production synchronous Redis AOF and full-dataset state, and host contention varied across runs.

| Revision | Clients | p50 ms | p95 ms | p99 ms | Requests/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Main `6f1a39f` | 1 | 75.77 | 123.13 | 132.26 | 12.78 |
| Main `6f1a39f` | 1 | 51.69 | 101.96 | 127.29 | 17.16 |
| PR `54fd016` | 1 | 56.11 | 117.98 | 137.10 | 15.03 |
| PR `54fd016` | 1 | 90.16 | 292.80 | 998.56 | 7.71 |
| Atomic SQL | 1 | 99.61 | 167.08 | 192.06 | 9.33 |
| Atomic SQL | 1 | 143.34 | 228.48 | 282.24 | 6.81 |
| Main `6f1a39f` | 8 | 731.76 | 1190.06 | 1386.26 | 10.10 |
| Main `6f1a39f` | 8 | 797.35 | 1009.96 | 1092.12 | 10.07 |
| PR `54fd016` | 8 | 921.95 | 1323.85 | 1480.50 | 8.44 |
| PR `54fd016` | 8 | 1306.53 | 4863.59 | 5425.93 | 5.06 |
| Atomic SQL | 8 | 1152.26 | 1930.82 | 2034.19 | 6.43 |
| Atomic SQL | 8 | 1794.45 | 2772.47 | 3062.07 | 4.23 |

At eight clients, the original candidate's median scoring transaction took 82–97 ms and the atomic statement took 29–43 ms. Its alert and journal rows still commit with the score, before Feast writes to Redis. Feast online writes, including local DataFrame conversion, were the largest measured stage and took 312–377 ms on the original candidate. Online reads took 14–16 ms. Per-request pool checkout totaled 45 ms and lock entry including checkout took 52 ms. Those timings overlap and include scheduling time. They do not isolate server-side lock wait or Redis command execution. Offline Feast materialization does not run on the API request path. The throughput release gate remains open. Repeat on a dedicated Linux host with production Redis persistence, and compare Redis server timings and same-card lock contention before changing the locking or journal protocol.

Use separate services and credentials when rerunning tests.

```bash
python -m pytest --ignore=tests/test_integration.py -rfEs
ruff check .
ruff format --check .
python -m mypy
docker compose -p fraud-readiness-tests -f docker-compose.test.yml up -d --wait
TEST_DATABASE_URL=postgresql://fraud:test-only@127.0.0.1:25432/fraud \
TEST_KAFKA_BOOTSTRAP=127.0.0.1:29092 TEST_REDIS=127.0.0.1:26379 \
  python -m pytest tests/test_integration.py -rfEs
```

## Linux deployment procedure

Use a Linux x86-64 VM with Docker Engine, Compose 2.24.4 or later, Git, curl, and systemd. Start sizing at `e2-standard-8` (8 vCPU, 32 GiB RAM) and 100 GiB of balanced persistent disk for a private single-node deployment. This is an initial estimate. Size disk from Kafka retention, database growth, artifacts and local backup staging. Measure peak memory during materialization and drift before enabling scheduled jobs. [Compute Engine machine types](https://cloud.google.com/compute/docs/general-purpose-machines).

For a 730-hour month in Iowa (`us-central1`), budget approximately **US$209.32/month** for the on-demand VM ($195.67), 100 GB of balanced persistent disk ($10) and one attached external IPv4 address ($3.65). Budget snapshots, off-VM backup storage, DNS, egress, observability ingestion and taxes separately. Reprice for the selected region and measured retention before approval. [VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose), [disk pricing](https://cloud.google.com/products/block-storage), [IPv4 pricing](https://cloud.google.com/vpc/network-pricing).

For the approved deployment phase, set `PROJECT_ID`, `ZONE`, `VPC`, `SUBNET`, `VM` and `STATIC_IP` to the target project, network and reserved IPv4 address. Confirm that the selected network has no broader SSH rule covering this VM and grant the operator IAP and OS Login access. The create commands provision billable resources. [Compute Engine Linux VM creation](https://cloud.google.com/compute/docs/create-linux-vm-instance), [IAP SSH](https://cloud.google.com/compute/docs/connect/ssh-using-iap).

To reserve a new address after approval, set `REGION` to the VM's region and run `gcloud compute addresses create "${VM}-web" --project "$PROJECT_ID" --region "$REGION"`. Use the address from `gcloud compute addresses describe "${VM}-web" --project "$PROJECT_ID" --region "$REGION" --format='value(address)'` as `STATIC_IP`. [Static external IP addresses](https://cloud.google.com/vpc/docs/reserve-static-external-ip-address).

```bash
gcloud compute firewall-rules create "${VM}-web" --project "$PROJECT_ID" \
  --network "$VPC" --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags "${VM}-web"
gcloud compute firewall-rules create "${VM}-iap" --project "$PROJECT_ID" \
  --network "$VPC" --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags "${VM}-iap"
gcloud compute instances create "$VM" --project "$PROJECT_ID" --zone "$ZONE" \
  --subnet "$SUBNET" --address "$STATIC_IP" --machine-type e2-standard-8 \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 100GB --boot-disk-type pd-balanced --tags "${VM}-web,${VM}-iap" \
  --metadata enable-oslogin=TRUE --shielded-secure-boot --no-service-account --no-scopes
gcloud compute ssh "$VM" --project "$PROJECT_ID" --zone "$ZONE" --tunnel-through-iap
```

On a fresh Ubuntu 24.04 host, install Docker Engine from Docker's package repository. [Docker Engine installation for Ubuntu](https://docs.docker.com/engine/install/ubuntu/).

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl util-linux
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
  "$(dpkg --print-architecture)" "$VERSION_CODENAME" |
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo docker compose version
```

Point two DNS names at the VM and allow inbound TCP 80 and 443. Restrict administration to an approved access path such as IAP. Keep the backend ports off the VM's published port list. Run the following on the eventual VM after the release gates pass.

```bash
sudo install -d -o "$(id -u)" -g "$(id -g)" /srv/fraud
git clone https://github.com/manojkumargudge/real-time-fraud-detection-platform.git /srv/fraud
cd /srv/fraud
git switch --detach RELEASE_COMMIT
sudo install -d -m 0700 /etc/fraud
sudo install -m 0600 deploy/production.env.example /etc/fraud/production.env
sudoedit /etc/fraud/production.env
```

Set `POSTGRES_PASSWORD`, `API_KEY`, `DASHBOARD_COOKIE_SECRET`, `DASHBOARD_USERS`, `API_HOSTNAME`, `DASHBOARD_HOSTNAME` and `ACME_EMAIL`. The template also includes `API_REQUESTS_PER_MINUTE` and `FEATURE_SOURCE`. Generate independent hex secrets with `openssl rand -hex 32`. Use a single-quoted value for the bcrypt entries. Build the dashboard image and run its hash CLI interactively with `sudo bash deploy/compose.sh run --rm --no-deps dashboard python -m src.dashboard.auth hash ANALYST` after setting a temporary nonempty placeholder for the dashboard entry. For Secret Manager, render individual values into the root-owned env file using a VM identity with access to the specific secrets. Keep the env file out of Git and avoid printing rendered Compose configuration.

Stage the trusted champion bundle under `artifacts/baseline/` before starting the API. Compare `sha256sum artifacts/baseline/model.json` with the approved model checksum. Stage the matching seed as `artifacts/feature-seed/seed.json` and set `FEAST_SEED_CUTOFF` in the root-owned environment file. The seed and bundle must be readable by UID 10001 on the read-only mounts. Run the [reproducible training procedure](../README.md) in an authorized environment if a verified bundle is unavailable. Use a new project and new volumes for initial bootstrap.

```bash
sudo install -d -m 0750 -o 10001 -g 10001 reports/drift
stack=(sudo env "FEAST_SEED_SHA256=$VERIFIED_SEED_SHA256" bash deploy/compose.sh \
  -f /etc/fraud/images.approved.yml --profile registry --profile monitoring)
"${stack[@]}" config --quiet
"${stack[@]}" pull
"${stack[@]}" up -d --no-build --wait postgres redis kafka
"${stack[@]}" run --rm --no-deps -T -e FEAST_SEED_SHA256 api \
  sh -ec 'exec python -m src.features.feast_checkpoint promote "$FEAST_SEED_ARTIFACT" \
    --model-dir "$MODEL_DIR" --repo-dir "$FEAST_REPO_DIR" --before "$FEAST_SEED_CUTOFF"'
"${stack[@]}" up -d --no-build --wait api consumer dashboard mlflow
```

Use [the override template](../deploy/release-images.example.yml) to supply
private registry `image: ...@sha256:...` references for
api, consumer, dashboard, drift, MLflow, Kafka, PostgreSQL, Redis and proxy.
API and consumer share the serving digest. Check the pinned references with
`config --images` and compare each registry manifest with the reviewed
[image inventory](release-image-security.md#release-owner-decision-for-the-audited-candidate).
The ephemeral CI registry addresses identify evidence and cannot serve a
deployment. A rebuild needs a new scan if its manifest changes.

The proxy image runs Caddy as UID and GID 10001. The production overlay
drops its Linux capabilities, prevents privilege escalation and allows
unprivileged ports within its network namespace. Bootstrap on fresh
`caddydata` and `caddyconfig` volumes. When using an older volume, verify
that UID 10001 can read and write its contents on an isolated restore
before cutover. Keep the pre-migration snapshot for rollback.

Use a separate project with a synthetic-only model fixture and seed for any
public demonstration. After ingress approval, configure DNS and Caddy HTTPS,
start the proxy explicitly with `"${stack[@]}" up -d --no-build --wait proxy`,
run `bash deploy/verify.sh "$API_HOSTNAME" "$DASHBOARD_HOSTNAME"`, and use the
authenticated score, duplicate submission, and analyst dashboard probes from
the coordinated recovery fixture. Compare the synthetic score and alert
counts with the fixture, and keep scoring credentials server-side. The
[competition rules](https://www.kaggle.com/c/ieee-fraud-detection/rules) restrict
redistribution of competition data. Keep that dataset and all derived seed
state away from the demonstration.

When upgrading an existing stack, pause producers and stop the API and consumers together before switching revisions. Bring them back together so every Feast writer uses the same locking and recovery protocol. A previously stale Redis volume needs a verified rebuild from its original baseline and subsequent events.

## Monitoring and model operations

Monitor consumer lag and outstanding Feast updates from the private network.

```bash
sudo bash deploy/compose.sh exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:19092 --describe --group fraud-scorer
sudo bash deploy/compose.sh exec -T postgres psql -U fraud -d fraud \
  -c 'SELECT count(*) AS pending_feast_updates FROM pending_feast_updates;'
```

Before opening ingress, configure an HTTPS uptime check on the API's `/health` endpoint and route alerts to the operator. Collect disk and memory utilization and the application's redacted service logs. When using the Ops Agent or Secret Manager, replace `--no-service-account --no-scopes` in the VM command with `--service-account "$VM_SERVICE_ACCOUNT" --scopes cloud-platform`. Grant the dedicated identity `roles/logging.logWriter` and `roles/monitoring.metricWriter` for the Ops Agent and `roles/secretmanager.secretAccessor` on each required secret for Secret Manager. Keep the root-owned rendering file at mode `0600`. [Uptime checks](https://cloud.google.com/monitoring/uptime-checks), [Ops Agent authorization](https://cloud.google.com/stackdriver/docs/solutions/agents/ops-agent/authorization), [Secret Manager access control](https://cloud.google.com/secret-manager/docs/access-control).

On a private deployment with authorized raw data, run one drift report manually and check its file and corresponding dashboard row. Enable the timer after that verification. The service and timer use `/srv/fraud` and `/etc/fraud/production.env`. Use the same flock lock for manual drift runs if the timer is enabled.

```bash
sudo flock -n /run/lock/fraud-drift.lock bash deploy/compose.sh \
  --profile monitoring run --rm --no-deps drift
sudo install -m 0644 deploy/fraud-drift.service deploy/fraud-drift.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fraud-drift.timer
sudo journalctl -u fraud-drift.service
```

Freeze the serving bundle and run the one-time import after MLflow becomes healthy. The importer checks the model hash and threshold, records the original run ID from the bundle's registry link, uploads the metadata and feature definition, and compares the downloaded artifacts before assigning the `champion` alias. Keep the original Windows registry and artifact directory as a historical archive. Serving loads the mounted bundle and never resolves a registry alias on each request.

```bash
sudo bash deploy/compose.sh --profile registry run --rm --no-deps \
  -e MLFLOW_TRACKING_URI=http://mlflow:5000 \
  -v "$PWD/artifacts/baseline:/app/artifacts/baseline:ro" \
  -v "$PWD/deploy/import_champion.py:/app/import_champion.py:ro" \
  mlflow python /app/import_champion.py
```

The drift timer runs drift-only checks. The explicit retraining job requires a clean Git checkout on a Linux-native filesystem, authorized labels, and the migrated champion. The Git provenance check timed out over the workstation's Windows bind mount and passed on a native Linux checkout without bypassing the guard. Run retraining with the same host lock so it cannot overlap the timer. Promotion still uses the existing sample gates and out-of-sample comparison. Registry promotion and replacement of the deployed serving bundle are separate operator actions. Analyst feedback remains append-only and is excluded from automatic training. The dashboard reads persisted fraud-ring output from the [batch GraphSAGE workflow](gnn.md).

```bash
sudo bash deploy/compose.sh --profile registry --profile retraining build retrain
sudo flock -n /run/lock/fraud-drift.lock bash deploy/compose.sh \
  --profile registry --profile retraining run --rm retrain
```

## Backup, verification and rollback

```bash
sudo install -d -m 0700 /var/backups/fraud
sudo bash deploy/compose.sh exec -T postgres psql -U fraud -d fraud \
  -c 'SELECT count(*) AS scored_transactions FROM scored_transactions;'
sudo bash deploy/backup-postgres.sh /var/backups/fraud
sudo bash deploy/restore-postgres.sh fraud-restore-rehearsal /var/backups/fraud/BACKUP_FILE.dump
```

Record nonzero scored-transaction counts on both sides of the restore comparison. The restore command targets an empty, separately named Compose project. Store encrypted backups off the VM. For a consistent cold backup, pause producers and ingress, stop the drift timer and stack, and snapshot the disk containing the PostgreSQL, Kafka, Redis and MLflow volumes together with artifacts and reports. Confirm paths and volume locations before taking a snapshot. Rehearse restoring the snapshot onto a separate VM with isolated DNS.

For application rollback, stop ingress and consumers, restore the previous reviewed Git revision and verified model bundle, then rebuild and restart against the existing volumes only when the schema is backward compatible. For a schema or storage incompatibility, restore the coordinated backup into an isolated environment and verify offsets, state and row counts before switching ingress. Avoid volume deletion during ordinary rollback. A VM or disk failure can take every service offline, and the deployment has no broker or database replica.

Keep a written inventory of the deployed commit, model hash, volume locations and backup timestamp. If the boot disk holds Docker's named volumes, preserve it during VM replacement. For the isolated restore, compare nonzero PostgreSQL and Kafka counts, consumer offsets and lag, Redis-backed history on known events, pending update count, MLflow artifact hashes and aliases, and the drift report's database row and file. Replay a known transaction and check that its persisted score is returned without incrementing history. Record observed recovery time and data loss before approving deployment.

## GCP project preflight

The target is `fraud-detection-platform-2026`. The read-only project check returned `ACTIVE`. The inventory returned no Compute Engine instances and no Artifact Registry repositories in `us-central1`. Confirm the region against data residency and DNS requirements before approval.

- **Sizing.** Keep `e2-standard-8` (8 vCPU, 32 GiB) as the initial rehearsal estimate. The earlier four-CPU application-only WSL2 run showed eight-client contention before the production background workloads were added. Test `e2-standard-4` (4 vCPU, 16 GiB) as the lower-cost option only if the full-stack rehearsal meets the [throughput protocol](throughput-acceptance.md) with headroom during Kafka consumption, API load, feature materialization, drift and backups. Measure whole-host peak RSS, CPU, disk queue, fsync latency and storage growth. The workstation benchmark does not establish either VM's production capacity.
- **Storage.** Budget 100 GB of balanced persistent disk initially, for example 20 GB boot and 80 GB data. Mount the data disk before Docker and place Docker's data root or the Compose volume paths there. Disable data-disk auto-delete. Budget capacity for Kafka retention, PostgreSQL data/WAL, Redis AOF rewrite, MLflow artifacts, Caddy state and backup staging. Alert at 70% utilization and confirm retention leaves space for a rewrite and a restore. Preserve the existing named volumes and test the mount across a VM reboot.
- **Images and credentials.** After approval, build `linux/amd64` serving, dashboard, registry and patched Kafka images from the approved commit. Scan every exact release image including the base PostgreSQL, Redis and Caddy images. Push to a same-region private Artifact Registry repository and use an image-only Compose override pinned by digest. Give the VM service account repository-scoped reader access and secret-specific accessor access. Give the build identity writer access. Use the VM identity and `cloud-platform` scope for Secret Manager. Pin secret versions and stage the Compose environment and dashboard credentials into owner-readable files under `/run` at service start. Keep values out of instance metadata, logs and Git. Verify startup after reboot and deliberate rotation. [Artifact Registry access control](https://docs.cloud.google.com/artifact-registry/docs/access-control) [Secret Manager access control](https://docs.cloud.google.com/secret-manager/docs/access-control)
- **Ingress and monitoring.** Publish Caddy on TCP 80 and 443 after pointing the approved DNS names at the VM address. Permit SSH through IAP and OS Login only. Confirm the effective firewall and private backend ports. Use the Ops Agent for host metrics and redacted application logs. Alert on failed external HTTPS checks, disk headroom, CPU and memory, Kafka lag and dead letters, pending journal age and count, PostgreSQL errors, Redis AOF write errors and stale backups. Confirm alert delivery with an isolated fault test. [IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding) [Ops Agent](https://docs.cloud.google.com/monitoring/agent/ops-agent)
- **Backups and rollback.** Schedule nightly encrypted PostgreSQL logical dumps to restricted off-VM storage. Proposed retention is seven daily and four weekly copies. Create a coordinated restore point before upgrades by quiescing ingress and consumers, draining the journal and recording offsets, then capturing PostgreSQL, Kafka, Redis, MLflow and model state from the same recovery window. Rehearse restore to isolated volumes, compare score and alert digests, Kafka positions, Redis state and the champion, and record measured RPO and RTO. Roll back application images to the previous reviewed digests when schemas are compatible. If state requires rollback, restore the coordinated set under halted ingress and reconcile the committed cohort before resuming.
- **Cost.** Iowa on-demand estimate at 730 hours/month is about **$209/month** for `e2-standard-8`, 100 GB balanced disk and one in-use external IPv4, or **$111/month** with `e2-standard-4` if testing qualifies it. An illustrative 100 GB of billed standard snapshot storage adds about $6.50/month. Registry storage, backup objects, logs, egress, DNS, taxes and any billable secret versions are usage-dependent. Before approval, price the chosen region and usage in the calculator. Propose a $250 project-scoped budget for the eight-vCPU option with 50%, 80% and 100% actual-spend alerts and a 100% forecast alert. Budget alerts do not cap spend. [VM pricing](https://cloud.google.com/products/compute/pricing/general-purpose) [Disk and snapshot pricing](https://cloud.google.com/products/block-storage) [IPv4 pricing](https://cloud.google.com/vpc/network-pricing) [Cloud Billing budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets)

Release is gated on the throughput protocol, exact-image scans and Feast exception review, the coordinated restore drill, external TLS and authenticated access verification, and operational alert delivery. Automated retraining requires separate authorized-data verification. Provisioning requires explicit approval of the deployment and estimate.

## Release validation follow-up

The current release inventory includes serving (API and the three consumers share it), dashboard, patched Kafka, patched PostgreSQL, Redis, MLflow, Caddy, drift and the optional retraining image. `deploy/scan-release-image.sh` builds the application images for `linux/amd64`, resolves external images to the architecture-specific manifest, and inventories packages and all vulnerability severities without applying ignores. It uses a loopback-only registry for locally built images. The hosted artifact records the manifest reference and scanner database result for each image. The build uses upstream package repositories, so rebuilding the same commit can produce a different digest. Scan the exact bytes retained for publication and compare the destination digest before using the image. See [the current applicability assessment](release-image-security.md) before considering any exception. Assign an owner, mitigation and expiry only after authorized review.

### Sustained Linux rehearsal

The Nov 27 read-only inventory found an existing stopped eight-vCPU host. Use
the [bounded reuse and operational verification procedure](capacity-rehearsal-approved-run.md)
for the next approved run and the [CVE disposition matrix](capacity-cve-disposition.md)
for release-owner review.

The private e2-standard-4 rehearsal completed. Two alternating trials per revision compared `6f1a39f` with `f31ecff`. Optimized throughput was 22.94, 27.00 and 25.67 requests/s at 1, 4 and 8 clients and 20.77 requests/s at 8 hot-card clients. The 208,037 measured HTTP requests completed without duplicates, errors, or timeouts. Each trial reconciled 27,000 Kafka transactions with exact offsets and matching score/alert digests. Optimized CPU reached 100% (p95 92–96%), peak sampled memory was 39.78%, and root disk usage reached 15%. The throughput comparison passed. Production CPU headroom and the final image set's sustained results are pending. [Full rehearsal evidence](https://github.com/manojkumargudge/real-time-fraud-detection-platform/pull/49)

On that machine, use Ubuntu 24.04, the production Compose overlay, full durable AOF and PostgreSQL settings, representative approved artifacts, and fresh project names and volumes. Give the API one worker and the Kafka consumer three replicas. Keep Caddy on `127.0.0.1:18443`, use `api.localhost` and `dashboard.localhost`, and set the same test API key and limiter of 30,000 requests/minute for both revisions. This keeps the limiter outside the stress measurement. Also run a separate 600/minute configuration check for the deployed ingress. Run the reviewed main baseline `6f1a39f` and release candidate sequentially, two repeats in alternating order, with only the API/consumer image switched for main. Keep the dashboard, Kafka, Redis, Caddy and PostgreSQL images, model, initial state, disk tier, and configuration identical. Main lacks the candidate late-event and recovery behavior, so use monotonically nondecreasing timestamps for the controlled comparison, and run the production safety suite separately. Provision configurations equivalent to four vCPUs/16 GiB and eight vCPUs/32 GiB on the dedicated host. Track the Docker overhead and the difference between local and Compute Engine disk performance.

Use the same Compose arguments for all commands below. Select a fresh rehearsal project, use explicit test-only credentials, use the loopback override from `deploy/rehearse-recovery.sh`, and bring up all services including `--profile registry`. Copy the local CA with `exec -T proxy cat /data/caddy/pki/authorities/local/root.crt`, and set `API_KEY` to the matching synthetic key in the load process. Install `requirements-dev.txt` in the Linux venv. Set `stack` as a Bash array containing the complete Compose command and its overlay paths, for example `stack=(docker compose -p "$REHEARSAL_PROJECT" --env-file "$REHEARSAL_ENV" -f docker-compose.yml -f docker-compose.production.yml -f "$LOOPBACK_OVERLAY" --profile registry)`. Validate loopback binding before load. Use a dedicated evidence directory per repeat.

```bash
bash deploy/check-capacity-host.sh > "$EVIDENCE/host.txt"
"${stack[@]}" config --quiet
"${stack[@]}" up -d --no-build --wait
"${stack[@]}" exec -T proxy cat /data/caddy/pki/authorities/local/root.crt > "$EVIDENCE/ca.crt"
"${stack[@]}" exec -T redis redis-cli CONFIG SET latency-monitor-threshold 1
bash deploy/sample-release-load.sh "${stack[@]}" > "$EVIDENCE/resources.log" 2>&1 &
sampler=$!
trap 'kill "$sampler" 2>/dev/null || true' EXIT
for clients in 1 4 8; do
  python -m benchmarks.sustained --ca "$EVIDENCE/ca.crt" --clients "$clients" \
    --cards 1024 --first-id "$((100000000 + clients * 1000000))" \
    --output "$EVIDENCE/spread-$clients.json"
done
python -m benchmarks.sustained --ca "$EVIDENCE/ca.crt" --clients 8 \
  --cards 1 --first-id 120000000 --output "$EVIDENCE/hot-8.json"
```

For the final 8-vCPU/32 GiB rehearsal, run the preflight with the local Docker
daemon and record the provisioned machine and storage tier. The preflight allows
31 GiB of usable RAM to account for the guest kernel's reservation. For a
historical 4-vCPU comparison, record the host resources separately. Preserve
startup and background job logs even if a trial stops before producing load
results. Check that the sampler remains active throughout each level and that
consumer replicas are running without restarts. Apply the disk limit to both
the root and Docker data filesystems and review PostgreSQL rollback and deadlock
counters, I/O timing, Redis AOF status and Kafka lag alongside throughput.

Repeat all four levels with concurrent Kafka input at a fixed 10 events/s, using the same authorized input and producer rate for both revisions. Use `"${stack[@]}" exec -T consumer python -m src.streaming.producer --raw-dir /app/data/raw --limit 27000 --rate 10` with the same approved raw-data bind mount in both stacks. If the workload is shorter, stop the producer before its input runs out and record the produced count and duration. Use fresh volumes for this repeat and confirm Kafka transaction IDs and entity keys do not collide with the synthetic HTTP fixture. Capture `iostat -xz 30` and the sampler log. Measure one minute warmup and ten minutes at each level, and wait for Kafka to drain at the end. Compare medians using the throughput protocol, and compare stored scores, alerts and online state for a separate fixed-cohort parity run. A 30% resource headroom planning target means peak steady-state memory and disk use below 70% and enough CPU and I/O margin to run the checkpoint, drift and Kafka work. Verify no committed offset gap, durable Redis writes, no unexplained pending journal entries and zero lag after drain. Keep timings and I/O samples for both host sizes. Select the four-vCPU option only after it passes, and repeat checks against the deployment VM before public cutover.

The recovery workflow uses a small synthetic cohort, a private loopback HTTPS proxy and separate source and restored named volumes. It verifies PostgreSQL score/alert digests, Kafka payload IDs and committed positions, binary Redis hashes and MLflow alias/artifacts before and after recovery. It also injects an API container that exits before startup and verifies unhealthy ingress before rolling it back. Record the measured times as rehearsal observations only. Without replication, a lost VM or disk can incur RPO up to the age of the last usable checkpoint. External DNS, public TLS, alert notification delivery, and AOF failure and stale journal alerting require a separate actual-host fault exercise.

### Budget and approval commands

#### Time-limited live verification and portfolio demo

Proposed only. After the capacity, security and release gates pass, obtain
separate approval for **US$3.25 in eligible Welcome credits** and reconfirm
Free Trial coverage through teardown. Use a fresh Iowa e2-standard-8 VM
with 100 GB balanced storage, an ephemeral IPv4 address for outbound
access, an eight-hour automatic STOP limit, and an IAP-only firewall.
Use approved digests and synthetic data. Demo access is private through
IAP and the portfolio artifact is a sanitized recording and evidence bundle.
Reserve any public DNS and external TLS validation for its own approved
window with a confirmed hostname and cost. Private TLS does not satisfy
the public cutover gate.

| Component | Planning basis | USD |
| --- | --- | ---: |
| Compute | 8 hours at $0.26804568/hour | 2.14437 |
| Ephemeral IPv4 | 8 hours at $0.005/hour | 0.04000 |
| Balanced disk | 100 GB, $10/month, retained at most 24 hours of a 730-hour month | 0.32877 |
| Artifact Registry | 10 GiB at $0.10/GiB-month, at most 24 hours, no free allowance assumed | 0.03288 |
| Internet transfer | Up to 1 GiB at the planning rate of $0.12/GiB | 0.12000 |
| Temporary logs, backups, secrets, and operations | Planning reserve | 0.35000 |
| **Estimated total** | Before tax and credits, calculated from unrounded items | **3.01601** |

Verify IAP-only exposure, application health, authenticated scoring and
replay, Kafka and PostgreSQL reconciliation, dashboard sign-in, monitored
incident opening and closure, and rollback. Save sanitized artifacts before
stopping the VM. Confirm `TERMINATED` and release temporary resources within
24 hours. The STOP policy bounds VM runtime. Disks and registry storage
require explicit cleanup. Keep the existing rehearsal disks out of the
temporary inventory and leave the billing plan unchanged.
[Compute pricing](https://cloud.google.com/products/compute/pricing/general-purpose)
[Disk pricing](https://cloud.google.com/compute/disks-image-pricing)
[Network pricing](https://cloud.google.com/vpc/network-pricing)
[Registry pricing](https://cloud.google.com/artifact-registry/pricing)

#### Continuous-hosting reference

Monthly planning estimate for Iowa and 730 powered-on hours, before credits and tax:

| Item | Assumption | 4 vCPU | 8 vCPU |
| --- | --- | ---: | ---: |
| Compute | E2 on demand | $97.84 | $195.67 |
| Disk | 100 GB pd-balanced | $10.00 | $10.00 |
| External IPv4 | One in use | $3.65 | $3.65 |
| Snapshots | 100 GB-month net billed | $6.50 | $6.50 |
| Off-VM dumps | 50 GB-month regional Standard storage including a small operations allowance below | $1.00 | $1.00 |
| Registry | 10 GiB retained, budget 9.5 GiB billed | $0.95 | $0.95 |
| Internet egress | 100 GiB at $0.12/GiB, assume no free quota | $12.00 | $12.00 |
| DNS | One zone and 100k queries | $0.24 | $0.24 |
| Secrets, backup operations and probes | Four active secret versions, low access and a $1 operations allowance | $1.00 | $1.00 |
| Logs and metrics | 20 GiB logs, 30-day retention, standard metrics and low custom volume within allowances | $0.00 | $0.00 |
| **Illustrative total** | Replace estimates with measured usage before approval | **$133.18** | **$231.01** |

Network cost depends on destination and tier. Reserve additional margin for traffic beyond the assumed 100 GiB, log ingestion above the included quota, additional backup/restore operations, and taxes. Eligible Free Trial accounts receive up to $300 in credits for 90 days. At the modeled run rate a full credit would cover about 69 days (four vCPU) or 40 days (eight vCPU), before other project spend. Actual credit balance, account type and expiry must be confirmed by the billing owner. Set budgets against gross usage so credits do not hide the burn rate. Budgets send notifications and are not automatic spending caps. [Free Trial](https://docs.cloud.google.com/free/docs/free-cloud-features) [Compute](https://cloud.google.com/products/compute/pricing/general-purpose) [Block storage](https://cloud.google.com/products/block-storage) [Backups](https://cloud.google.com/storage/pricing) [Registry](https://cloud.google.com/artifact-registry/pricing) [DNS](https://cloud.google.com/dns/pricing) [Network](https://cloud.google.com/vpc/pricing) [Logging and monitoring](https://cloud.google.com/products/observability/pricing)

Run the following only after release and cost approval. Set `BILLING_ACCOUNT` and `ALERT_CHANNEL` to the approved billing ID and resource name. Choose `BUDGET_USD=160` for the qualified four-vCPU VM or `275` for the eight-vCPU VM. Use the VM and firewall commands under [Linux deployment procedure](#linux-deployment-procedure) with the approved machine type. Defer the web tag/firewall rule and DNS publication to the separately approved cutover. Give the infrastructure operator only instance/disk, networking, DNS, scoped budget management and VM service-account attachment permissions for their assigned steps. The deploy operator uses `roles/iap.tunnelResourceAccessor` and OS Login, and uses OS Admin Login only for the required `sudo` setup. Give the publisher repository-scoped writer and the VM repository-scoped reader.

```bash
PROJECT_ID=fraud-detection-platform-2026
REGION=us-central1
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
  --display-name=fraud-production-monthly --budget-amount="${BUDGET_USD}USD" \
  --filter-projects="projects/$PROJECT_NUMBER" --credit-types-treatment=exclude-all-credits \
  --threshold-rule=percent=0.50 --threshold-rule=percent=0.80 --threshold-rule=percent=1.00 \
  --threshold-rule=percent=1.00,basis=forecasted-spend \
  --notifications-rule-monitoring-notification-channels="$ALERT_CHANNEL"
gcloud artifacts repositories create fraud-release --project "$PROJECT_ID" \
  --repository-format=docker --location "$REGION" --immutable-tags
gcloud auth configure-docker "$REGION-docker.pkg.dev"
```

Publish the byte-preserved approved linux/amd64 manifests with `skopeo copy --preserve-digests docker://SOURCE@sha256:DIGEST docker://us-central1-docker.pkg.dev/fraud-detection-platform-2026/fraud-release/IMAGE:RELEASE_TAG` from the authorized release build environment. Compare all destination digests. Use an immutable Compose override that maps both API and consumer to serving, proxy to Caddy and every other service to its approved digest. Apply the overlay with `-f /etc/fraud/release-images.yml`. Use `pull` and `up --no-build`, preserving the named volumes. Provision PostgreSQL password, API key, dashboard cookie secret and bcrypt analyst entries in Secret Manager with individually scoped access from the VM. Keep public hostnames, ACME email and Secret Manager version references in a root-readable deployment manifest. Use the VM identity to render them into `/run/fraud/production.env` with mode `0600` before Compose starts, and keep them out of logs and Git. The VM needs logging and metric writer grants only if using the Ops Agent, and a bucket-scoped backup writer grant. Keep backup-reading permission on the isolated restore identity. Record build/image digests, verified artifact hash, backup checksum and previous-known-good override before cutover.

After cutover approval, apply the firewall web tag/rule and DNS A records, wait for Caddy's public certificate, run `bash deploy/verify.sh "$API_HOSTNAME" "$DASHBOARD_HOSTNAME"`, and verify an authorized synthetic score and duplicate plus analyst sign-in. Confirm external alert delivery for a controlled health failure and verify the recovery notification. If the startup or reconciliation gate fails, close ingress and stop writers, reapply the previous image override by digest and reuse data volumes only if schema compatibility is confirmed. For state incompatibility, restore the coordinated checkpoint into separate volumes, reconcile all digests/offsets and only then switch ingress.

### Initial image audit and subsequent fixes

A hosted Linux amd64 build and Trivy inventory completed for all nine image roles. The table uses digest prefixes for readability. Full manifest refs, package inventories and complete finding lists are in the workflow artifacts. The PostgreSQL rebuild with the Alpine package fix requires a fresh scan and restore validation. [Initial digest inventory](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45565323432)

| Image role | SHA-256 prefix | High | Critical |
| --- | --- | ---: | ---: |
| Serving (API and consumer) | `aa0448406f67` | 101 | 4 |
| Dashboard | `c88bd821f759` | 101 | 4 |
| Kafka | `d2672d869647` | 24 | 1 |
| PostgreSQL upstream base | `9d9e487c1bd6` | 26 | 1 |
| Redis | `e4ee3e03ce8f` | 0 | 0 |
| MLflow | `ee13af170797` | 99 | 2 |
| Caddy | `fc9cbc100585` | 2 | 0 |
| Drift | `e82a91e70f12` | 101 | 4 |
| Retraining | `3e29cb5f5b61` | 120 | 4 |

- **Kafka:** The Java scan completed. It identified 25 high/critical entries with fixes listed, including critical Netty `CVE-2026-75595` and Jackson, compression, ZooKeeper and Jetty dependencies. The private KRaft broker does not exercise ZooKeeper or Connect in this Compose topology, but exposure of the other bundled server paths still requires review. Evaluate a supported upstream Kafka release on isolated volumes, then verify read/write clients, group offsets, KRaft upgrade and downgrade prerequisites, and cold restore. The initial production audit used Kafka 3.9.2. The compatibility and security results for its replacement are recorded below. [Kafka releases](https://kafka.apache.org/downloads/) [Kafka advisories](https://kafka.apache.org/community/cve-list/)
- **PostgreSQL:** Trivy identified the bundled Go `gosu` startup helper and Alpine `libxml2` `CVE-2026-86144`. The same-major image rebuild requires `libxml2>=2.13.9-r3`. The `gosu` findings remain open pending reachability review and a patched privilege-drop helper verified against init and recovery. [PostgreSQL 16.16 release notes](https://www.postgresql.org/docs/release/16.16/)
- **Feast and PyArrow:** The four listed Python fixes require moving beyond Feast 0.47's package constraints (`numpy<2`, `pyarrow<18.1.0`). Existing entries expire **2027-01-31**. Embedded Feast has a locally generated trusted registry, with no feature or registry server published. The offline materializer is outside production Compose, and PyArrow inputs must remain generated locally and trusted. Revalidate those conditions for the exact release, or migrate Feast, NumPy and PyArrow together and rerun the byte-level Redis, recovery, and model parity tests. These scopes do not extend to other findings. [Existing entries](security.md) [Feast releases](https://github.com/feast-dev/feast/releases)
- **Python image OS layers:** The scan still reports critical SQLite and zlib findings without a listed Bookworm fix in this run, across serving, dashboard, drift, retraining and MLflow. Confirm each Debian advisory disposition, affected binary and call path. MLflow actively uses SQLite, so do not apply a blanket unused-component exception. Compare a supported newer base on isolated runtime, MLflow and recovery tests before changing production.
- **Caddy:** The two high findings are `golang.org/x/net/html` and Go JOSE. The current proxy routes do not configure an HTML templating or JWK authentication module. Keep reachability unresolved until both findings are reviewed against the exact binary and config, and use an upstream or reproducibly patched build if either applies. [Go HTML advisory](https://pkg.go.dev/vuln/GO-2026-6860) [Go JOSE advisory](https://github.com/advisories/GHSA-mgcc-cww5-fp2q)

The security gate remains blocked. The scan workflow succeeds when it inventories the package and findings; release disposition is a separate gate. No new risk exceptions were granted.

### Kafka security upgrade

The Alpine-patched Kafka 4.3.2 candidate scan recorded zero high and critical findings, including the absence of CVE-2026-75595. The exact tested candidate manifest was `sha256:e1b6116e8e5348e9a2338344e720e933ae46e324d283434d0e51c85950609a9a`. Eight Kafka and Feast integration tests passed. A cold 3.9.2 to 4.3.2 upgrade recovered two original payloads and committed offset 2, then stored a third payload. A cold rollback to 3.9.2 recovered all three payloads and committed offset 3. The test left the KRaft metadata feature level unchanged. Keep that rollback constraint for a staged migration. Production Compose uses the pinned 4.3.2 upstream manifest, applies Alpine updates at build time and disables the JDK Base64 intrinsic per Apache guidance. Re-scan the exact publishable digest because Alpine package resolution can change between builds. [Candidate scan and compatibility evidence](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45591965673) [Apache JDK mitigation](https://kafka.apache.org/blog/2026/11/19/mitigating-cve-2026-114938-java4shell-in-apache-kafka-deployments/)
### Private Compute Engine validation

The approved synthetic rehearsal ran one e2-standard-4 in us-central1-a with a 100 GB balanced disk and an eight-hour STOP schedule. The custom VPC admits only IAP SSH. Backend containers publish no ports, and Caddy listens on 127.0.0.1:18443. An observed local HTTP 502 from the failed-upgrade drill was forwarded to Cloud Logging. Cloud Monitoring delivered the matching incident to the private Pub/Sub subscription, which acknowledged it. Default Compute Engine CPU metrics appeared in Cloud Monitoring. The isolated cold checkpoint restored exact PostgreSQL scores and alerts, Kafka committed offsets, Redis bytes, and the MLflow champion. Backup was 5 seconds, verified restore 47 seconds, and rollback 17 seconds, with zero lost committed fixture transactions. The base comparison stays on f31ecff so release image changes cannot affect a later trial. The ₹450 project-only alert budget measures gross usage. The VM stop schedule is a separate control. Public DNS and production deployment remain outside this rehearsal.

### Runtime security changes after the initial inventory

The Debian 13 Python rebuilds removed the initial Bookworm MiniZip and SQLite critical findings from the checked serving, dashboard and MLflow images. Feast 0.67.0, NumPy 2.2.6 and PyArrow 23.0.1 passed the focused compatibility run. The patched Compose recovery also reproduced the earlier synthetic Redis byte digest exactly. The prior Feast, PyArrow and image exceptions have been retired. Remaining reported findings require disposition on the exact release manifests. [Feast compatibility](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45593303730) [Cross-version Redis snapshot and restore](https://github.com/manojkumargudge/real-time-fraud-detection-platform/actions/runs/45594638604)

The Caddy build uses the exact upstream commit from PR 8368, which removes Go JOSE v3 and pins an updated x/net. The PostgreSQL image packages Alpine's privilege-drop helper at the official entrypoint's gosu path. The recovery check verifies that PostgreSQL PID 1 runs with the non-root PostgreSQL UID. Verify both images in the exact-digest inventory before release. [Caddy upstream security change](https://github.com/caddyserver/caddy/pull/8368)

### Release checkpoint and next capacity check

The November 26 zero-cost preflight verified 12 logical processors and about
16 GiB physical RAM on Windows. Ubuntu WSL2 and its separate Docker daemon
reported approximately 9.7 GiB usable memory under the 10 GiB configuration
limit, plus 4 GiB swap. Docker Desktop was stopped or unavailable to the CLI.
The dedicated 8-vCPU, 32 GiB target capacity cannot be reproduced there.
Neither sustained trial was started. The hosted HTTP benchmark remains a
functional and comparative performance result. CPU headroom, durable AOF
and disk behavior, and concurrent drift and checkpoint capacity still require
the target-host rehearsal. The smaller 4-vCPU cloud rehearsal saturated CPU,
so it has no release capacity certification.

The checkpoint candidate pins the seed to the model and verifies full Redis row
digests at startup and on the scoring path. The service-backed regression exercises
stale-but-present rows, a partial restore, damaged and missing seed files, failed
promotion and independent batch feature/prediction parity. Use the
[checkpoint bootstrap](feature_store.md#rebuilding-online-state-after-redis-loss)
before starting the production scorer. The earlier full retained-cohort evidence
remains the incident audit. The checkpoint path requires a new capacity measurement.
Local promotion of the full training seed verified 39,087 user rows and 13,792
card rows in 98.3 seconds. Opening the scorer and rechecking those rows took
53.3 seconds on the isolated Windows/WSL stack. A subsequent 256-event run with
the checkpoint enabled matched the independent batch oracle exactly for all 12
history features, probabilities and alert decisions.

On November 26 billing was linked, the private four-vCPU rehearsal VM was stopped,
and Iowa showed 24 E2 CPUs available, eight unused address slots and 100 GB of
500 GB balanced/SSD disk quota in use. Trial balance was unavailable via the CLI.
Cloud spending and public ingress each require separate approval.

Production stays blocked. See [exact manifest digests and the per-CVE review](release-image-security.md). Full hosted CI, byte-level Feast parity, Kafka upgrade and rollback, and coordinated recovery are recorded there. A scan that completes can still report unresolved findings. Start the next deployment validation only with a reviewed manifest set and fresh approval for the host, cost and eventual public ingress.

1. Use a separate e2-standard-8 (8 vCPU, 32 GiB) in Iowa, Ubuntu 24.04, with 100 GB pd-balanced in the existing private rehearsal subnet and IAP-only SSH. Keep the old VM stopped. Set an eight-hour platform STOP limit, use loopback Caddy and synthetic data, and save results off the host before stopping it. Budget about **$2.29 for eight hours** before credits, variable network/log usage and tax. This assumes $0.26805/h compute, $0.005/h ephemeral IPv4 and $10/month disk prorated. The existing stopped VM's 100 GB disk is a separate ~$10/month while retained. At the read-only November 25 checkpoint, the old VM was `TERMINATED`, its disk was `READY`, and inventory showed no reserved IPv4 or snapshots. [Compute](https://cloud.google.com/products/compute/pricing/general-purpose) [Disk](https://cloud.google.com/compute/disks-image-pricing) [IPv4](https://cloud.google.com/vpc/network-pricing)
2. Run two sequential repeats of the final candidate under the [sustained protocol](#sustained-linux-rehearsal) with fresh Compose projects, the verified release digests, the same warmed model and 10 synthetic Kafka events/s during every client level. The preserved synthetic workload is `deploy/capacity-stream.py`. Use it only with a fresh, isolated Compose project. Start it with `"${stack[@]}" exec -T -e FRAUD_CAPACITY_FIXTURE=1 api python - produce 27000 < deploy/capacity-stream.py` in the background before the first warmup, wait for its successful exit, and run the equivalent `audit 27000` command after Kafka drains. Use the completed cloud samples as historical context. If a new relative gate is needed, collect two matched main runs on the new host so a cross-machine comparison is never used as the acceptance test. Record raw JSON, Kafka audit, journal and iostat evidence. Require exact integrity, zero errors, sampled steady-state p95 CPU at most 70% and peak steady-state memory and disk usage at most 70%. Exercise checkpoint and drift while ingestion is active and check lag recovery and p99 latency. Recheck the deployed rate limit separately.
3. Carry out [private deployment and rollback preparation](#linux-deployment-procedure), starting with the digest override and scoped secrets from the [GCP commands](#budget-and-approval-commands). Verify the automated guest telemetry path for disk, Kafka lag, stale Feast journal, Redis AOF failures and endpoint health. Deliver and acknowledge a controlled alert and its recovery notification on the intended production channel. The earlier operator-forwarded 502 verified only the Cloud Monitoring-to-Pub/Sub delivery path.
4. After separate production ingress approval, target only Caddy with public TCP 80/443, publish approved DNS A records and verify public CA trust and hostnames from outside GCP. Use `deploy/verify.sh`, then check authenticated scoring, duplicate detection and dashboard analyst login, and confirm backend ports remain unreachable. On failure close ingress, quiesce writers and follow the [coordinated rollback](#backup-verification-and-rollback) with the known-good manifests and validated checkpoint.
