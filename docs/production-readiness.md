# Compute Engine release checklist

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
| Kubernetes | The existing [Minikube deployment](kubernetes.md) remains a local demonstration. Its manifests and service behavior require a separate regression run. |

## Local evidence

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
- The larger Kafka replay stopped at next committed offsets 9,145, 8,760 and 3,760. A read-only audit found 21,665 unique source IDs, 11,250 matching scores, 529 corresponding alerts, zero pending Feast entries and an empty dead-letter topic. The missing scores were at offsets 1,423–6,806 on partition 0 and 1,410–6,440 on partition 1. The broker's checkpoint places the start of leader epoch 16 at offsets 6,807 and 6,441, the first scored records after the gaps. The old consumer could commit across a delivery gap after a coordinator retry. A deterministic test exercises that commit boundary. The original logs cannot distinguish what the client fetched, buffered or delivered to Python during the epoch transition. Keep the frozen group and stores intact. Reconcile an independent replay before releasing.
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
  --metadata enable-oslogin=TRUE --shielded-secure-boot --no-service-account
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

Stage the trusted champion bundle under `artifacts/baseline/` before starting the API. Compare `sha256sum artifacts/baseline/model.json` with `9fdad27ea3b07d5c29159bae75acc3ecaa7bf671943ab543bf9385145b89ba88`. Run the [reproducible training procedure](../README.md) in an authorized environment if a verified bundle is unavailable. Use a new project and new volumes for initial materialization.

```bash
sudo install -d -m 0750 -o 10001 -g 10001 reports/drift
sudo bash deploy/compose.sh --profile monitoring --profile registry config --quiet
sudo bash deploy/compose.sh --profile monitoring --profile registry build
sudo bash deploy/compose.sh up -d --wait postgres redis kafka
sudo bash deploy/compose.sh run --rm --no-deps -v "$PWD/data/raw:/app/data/raw:ro" \
  consumer python -m src.features.feast_store materialize --repo-dir /tmp/feast --before validation
sudo bash deploy/compose.sh --profile registry up -d --wait
sudo bash deploy/verify.sh API_HOSTNAME DASHBOARD_HOSTNAME
```

Use the materialization command only for the private IEEE-CIS validation replay, with authorized local files and a compatible stream cutoff. Use a separate empty deployment for synthetic demonstration transactions. The [competition rules](https://www.kaggle.com/c/ieee-fraud-detection/rules) restrict redistribution of competition data. Keep raw data, identifiers and transaction-derived analyst views inaccessible to public visitors. The API initializes the application schema under a PostgreSQL advisory transaction lock. A model artifact change requires restarting the API and consumers with the verified bundle.

When upgrading an existing stack, pause producers and stop the API and consumers together before switching revisions. Bring them back together so every Feast writer uses the same locking and recovery protocol. A previously stale Redis volume needs a verified rebuild from its original baseline and subsequent events.

## Monitoring and model operations

Monitor consumer lag and outstanding Feast updates from the private network.

```bash
sudo bash deploy/compose.sh exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka:19092 --describe --group fraud-scorer
sudo bash deploy/compose.sh exec -T postgres psql -U fraud -d fraud \
  -c 'SELECT count(*) AS pending_feast_updates FROM pending_feast_updates;'
```

Before opening ingress, configure an HTTPS uptime check on the API's `/health` endpoint and route alerts to the operator. Collect disk and memory utilization and the application's redacted service logs. When using the Ops Agent or Secret Manager, replace `--no-service-account` in the VM command with `--service-account "$VM_SERVICE_ACCOUNT" --scopes cloud-platform`. Grant the dedicated identity `roles/logging.logWriter` and `roles/monitoring.metricWriter` for the Ops Agent and `roles/secretmanager.secretAccessor` on each required secret for Secret Manager. Keep the root-owned rendering file at mode `0600`. [Uptime checks](https://cloud.google.com/monitoring/uptime-checks), [Ops Agent authorization](https://cloud.google.com/stackdriver/docs/solutions/agents/ops-agent/authorization), [Secret Manager access control](https://cloud.google.com/secret-manager/docs/access-control).

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
