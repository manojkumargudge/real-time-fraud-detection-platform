# Kubernetes deployment (Minikube)

The platform on one local Minikube node: streaming scoring with Feast features, the API, and a
scheduled drift job. Everything lives in the namespace `fraud-k8s`. It is fully separate from
the two Compose stacks; the Minikube node is its own container on the Docker VM.

This is a working local deployment, **not a production one**. See "Not production" at the end.

**Why Minikube:** the brief allows Minikube or an equivalent. Docker Desktop's built-in
Kubernetes was tried first, but it can only be switched on in the Docker Desktop UI (no CLI or
settings key on Docker Desktop 4.55). Minikube on the Docker driver runs next to the Compose
stacks on the same Docker Desktop.

## Architecture

```
                 kubectl port-forward (no Ingress)
                            |
   producer Job ---> Kafka ---> consumer x3 ---> PostgreSQL <--- API (/health, /score)
   (one-off)       (KRaft)         |   ^          (scored_transactions, alerts, drift_reports)
                                   v   |                ^
                             Redis (Feast online state) |
                                   ^                    |
   feast-materialize Job ----------+       drift CronJob (every 6 h, --no-retrain)
          \_____________ read-only hostPath /data/fraud-raw (IEEE-CIS CSVs) _____/
```

| Component | Kind | Image | Storage | Probes | Requests / limits |
|---|---|---|---|---|---|
| PostgreSQL 16.4 | Deployment + Service `postgres:5432` | `postgres:16.4-alpine` | PVC 1 Gi | `pg_isready` (ready + live) | 100m / 192 Mi, 500m / 384 Mi |
| Kafka 3.8.1, single-node KRaft | Deployment + Service `kafka:9092` | `apache/kafka:3.8.1` | PVC 1 Gi | TCP 9092 (ready + live) | 200m / 512 Mi, 1 / 768 Mi (heap 256-384 MB) |
| Redis 7.4.1 (Feast online store) | Deployment + Service `redis:6379` | `redis:7.4.1-alpine` | PVC 512 Mi (AOF) | `redis-cli ping` (ready + live) | 50m / 64 Mi, 200m / 128 Mi |
| API (FastAPI) | Deployment + Service `api:8000` | `fraud-serving:k8s-local` | - | HTTP `/health` (ready; live after 60 s of failures) | 100m / 256 Mi, 1 / 512 Mi |
| Consumer x3 | Deployment (one per partition) | `fraud-serving:k8s-local` | - | startup: heartbeat file exists; live: heartbeat < 60 s old | 100m / 256 Mi, 1 / 512 Mi each |
| Feast materialize | Job `k8s/jobs/feast-materialize.yaml` | `fraud-serving:k8s-local` | raw data, read-only | - | 500m / 1 Gi, 2 / 1.5 Gi |
| Drift | CronJob `drift`, `0 */6 * * *` (UTC) | `fraud-monitoring:k8s-local` | raw data, read-only | - | 500m / 1 Gi, 2 / 2 Gi |
| Producer (one-off) | Job `k8s/jobs/produce-events.yaml` | `fraud-serving:k8s-local` | ConfigMap with events | - | 50m / 128 Mi, 500m / 256 Mi |

**Settings:** ConfigMap `fraud-app-config`, the same values as the demo stack:
`FEATURE_SOURCE=feast`, `FEAST_REDIS=redis:6379`, `FEAST_REPO_DIR=/tmp/feast`. Each pod
registers the Feast objects in its own local registry at startup; the online keys in Redis
(project `fraud`) are shared.

**Images** (`k8s/build-image.sh`): `Dockerfile.k8s` adds the model bundle from
`artifacts/baseline` to the serving image (`fraud-serving`) and to the monitoring image
(`fraud-monitoring` = serving + Evidently, `REQUIREMENTS=requirements-monitoring.txt`).
`Dockerfile.k8s.dockerignore` admits only the two bundle files. The images are loaded with
`minikube image load`, use `imagePullPolicy: IfNotPresent`, and are never pushed; the bundle
stays gitignored.

**Secret:** `fraud-postgres`, created by a command at deploy time with a random password,
never written to a file. `DATABASE_URL` is assembled from it in the pod spec
(`$(POSTGRES_PASSWORD)`).

**Details that matter:**
- **`enableServiceLinks: false`** on every pod. Otherwise Kubernetes injects variables such
  as `KAFKA_PORT=tcp://...` for the `kafka` Service, and the apache/kafka image turns every
  `KAFKA_*` variable into a broker setting.
- **Init containers:**
  - Kafka chowns its data directory to uid 1000 first, because Minikube's hostPath volumes
    ignore `fsGroup`.
  - The API, the consumers and the materialize Job wait for PostgreSQL, Kafka and Redis
    before starting, instead of crash-looping.
- **Exec probes have `timeoutSeconds: 5`.** The 1 s default timed out once under CPU load on
  the 4 GB node; one failure, no restart.
- **Requests and limits:** requests total about 2 Gi of memory for the long-running pods,
  limits about 3.5 Gi (4 GB node). Run the data Jobs when the node has room: the materialize
  Job peaks at about 0.9 GiB.

## How the Feast state is loaded

The online state must start from the batch history before the validation cut-off, exactly as
the demo stack was started. Otherwise the features are skewed (docs/feature_store.md). The
materialize CLI reads the raw IEEE-CIS files, which must never go into an image or into git.

**Choice: copy the two CSVs once into the Minikube node and mount them read-only.**
- `minikube cp` puts them into `/data/fraud-raw`; Minikube keeps `/data` across restarts. The
  files are made read-only (`chmod 0444`).
- Both the materialize Job and the drift CronJob mount that directory as a read-only `hostPath`
  (`type: Directory`, so a missing copy fails the pod instead of creating an empty directory).

**Why:**
- The drift CronJob needs the same data, inside the cluster, every 6 hours. One copy serves
  both.
- `minikube mount` would need a 9p server process on the host to stay running, and a CronJob
  would silently depend on it.
- Running the materialize CLI on the host through `kubectl port-forward` works too, as in the
  throughput benchmark, but does not help the CronJob.
- The data stays on this machine (the node's disk), never in an image or git.
- Remove it with `minikube ssh -- sudo rm -rf /data/fraud-raw`.

The Job runs `python -m src.features.feast_store materialize --before validation --raw-dir
/data/raw` before any event is produced.

## Consumer liveness (heartbeat)

- With `CONSUMER_HEARTBEAT_FILE=/tmp/heartbeat` (set only in this manifest), the consumer
  touches the file on every poll-loop iteration, also when idle and during retry waits
  (`src/streaming/consumer.py`, `Heartbeat`).
- The file goes stale only when the loop itself stops (a hung or frozen process), not while
  the consumer is waiting out a database or DLQ outage.
- A failing touch is logged once and never stops consumption.
- Without the variable nothing changes (the Compose stacks and tests run as before; unit
  tests cover both).
- **Probes:**
  - Startup: the file exists (up to 3 min for model load and Feast registration).
  - Liveness: `test $(( $(date +%s) - $(stat -c %Y /tmp/heartbeat) )) -lt 60`, every 10 s,
    3 failures, so a frozen consumer is restarted about 60-90 s after it stops.

## Drift CronJob

- **What it runs:** `python -m src.monitoring.run_drift --no-retrain` every 6 hours (UTC),
  `concurrencyPolicy: Forbid`.
  - Feature drift, prediction drift and delayed-label PR-AUC over the in-cluster
    `scored_transactions`; one row per run in the in-cluster `drift_reports`.
  - The Evidently HTML report stays pod-local (`/tmp/drift-reports`); the row is the result.
- **Retraining stays on the host.** The MLflow registry is a local SQLite file
  (`mlflow.db` + `mlruns/`) whose artifact paths are Windows `file:///C:/...` paths. With
  `--no-retrain` the row records `promotion_decision = "not run: retraining disabled"`.
  - In-cluster retraining needs an MLflow tracking server with a shared artifact store
    (object storage) first.

## What the cluster's data shows

- **The cluster database is a 20-event replay**, not the demo database: the current deployment
  (redeployed 2026-10-27 with the dashboard, PR #6) holds 20 `scored_transactions`, 1 alert
  and 0 `case_reviews` (checked read-only on 2026-10-28).
- **The drift CronJob checks only** (`--no-retrain`); retraining stays on the host.
- **Evidence of an in-cluster drift run:** 2026-10-25, "Results" → "Full deployment" below: a
  run created from the CronJob (`drift-manual`) wrote `drift_reports` row 1 (30 rows, share
  0.893, `triggered = true`, retraining not run). That deployment's namespace was deleted
  afterwards. The current deployment has no `drift_reports` table yet (the job creates it on
  its first run; checked 2026-10-28), so the cluster's Drift page shows "No drift reports in
  this database yet".
- **The Drift page with real reports is shown from the Docker Compose demo database**
  (`drift_reports` ids 1-9; screenshot [08-drift.png](screenshots/08-drift.png)).

## Prerequisites

- Docker Desktop running (with WSL 2; the Docker VM had 10 GB via `%UserProfile%\.wslconfig`
  `memory=10GB`).
- Minikube 1.39 or later (`winget install -e --id Kubernetes.minikube`), started with
  `minikube start --driver=docker --cpus=4 --memory=4g`. That gives Kubernetes 1.37 on one
  node, with the `standard` storage class.
- The model bundle in `artifacts/baseline` (`python -m src.models.train`), and the IEEE-CIS
  CSVs in `data/raw/ieee-cis` (data/README.md).

## Deploy

```bash
k8s/build-image.sh                         # fraud-serving + fraud-monitoring :k8s-local, loaded
# optional, avoids pulls from Docker Hub inside Minikube:
for i in postgres:16.4-alpine apache/kafka:3.8.1 redis:7.4.1-alpine; do minikube image load $i; done
# raw data for the Feast Job and the drift CronJob (once; read-only on the node):
minikube ssh -- sudo mkdir -p /data/fraud-raw
for f in train_identity.csv train_transaction.csv; do minikube cp data/raw/ieee-cis/$f minikube:/data/fraud-raw/$f; done
minikube ssh -- "sudo chmod 0555 /data/fraud-raw && sudo chmod 0444 /data/fraud-raw/*.csv"

kubectl apply -f k8s/namespace.yaml
kubectl -n fraud-k8s create secret generic fraud-postgres --from-literal=password="$(openssl rand -hex 24)"
kubectl apply -n fraud-k8s -f k8s/app-config.yaml -f k8s/postgres.yaml -f k8s/kafka.yaml -f k8s/redis.yaml
kubectl apply -f k8s/jobs/feast-materialize.yaml
kubectl -n fraud-k8s wait --for=condition=complete job/feast-materialize --timeout=15m
kubectl apply -k k8s                       # API, consumers, drift CronJob (and the rest, unchanged)
for d in postgres redis kafka api consumer; do kubectl -n fraud-k8s rollout status deploy/$d; done
```

## Verify

```bash
kubectl -n fraud-k8s get pods                                    # Running, 1/1, restarts
kubectl -n fraud-k8s port-forward svc/api 18080:8000 &
curl http://localhost:18080/health                               # status ok, store_ok true

# first 20 validation events (the same events as producer --skip 0 --limit 20):
python -m scripts.k8s_events --limit 20                          # -> k8s/local/events.json (gitignored)
kubectl -n fraud-k8s create configmap smoke-events --from-file=events.json=k8s/local/events.json
kubectl apply -f k8s/jobs/produce-events.yaml
PG=$(kubectl -n fraud-k8s get pod -l app=postgres -o name)
kubectl -n fraud-k8s exec $PG -- psql -U fraud -d fraud -c "select transaction_id, score from scored_transactions"
#   compare with the demo database: same ids, same scores (both from the same Feast start state)
KF=$(kubectl -n fraud-k8s get pod -l app=kafka -o name)          # Git Bash: MSYS_NO_PATHCONV=1
kubectl -n fraud-k8s exec -c kafka $KF -- /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --group fraud-scorer

kubectl -n fraud-k8s create job drift-manual --from=cronjob/drift   # one drift run now
kubectl -n fraud-k8s exec $PG -- psql -U fraud -d fraud -x -c "select * from drift_reports"

# liveness: freeze one consumer from the node (SIGSTOP to PID 1 inside the container is ignored)
CID=$(kubectl -n fraud-k8s get pod <consumer> -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's|containerd://||')
PID=$(minikube ssh -- sudo crictl inspect --output go-template --template '{{.info.pid}}' $CID)
minikube ssh -- sudo kill -STOP $PID                             # restarted after ~60-90 s
```

## Delete

```bash
kubectl delete namespace fraud-k8s     # everything, PVCs included
minikube stop                          # keeps the cluster; `minikube delete` removes it
minikube ssh -- sudo rm -rf /data/fraud-raw   # (cluster running) remove the raw data copy
```

## Results

### Full deployment (2026-10-25, branch `k8s-full`, Minikube 1.39.0, Kubernetes 1.37.0)

**Feast state:** the materialize Job finished in 72 s inside the cluster: before=12,192,900,
472,432 events, 39,087 uids, 13,792 cards, **52,879 Redis keys**. That equals the demo stack's
recorded cold start.

**Pods:**
- PostgreSQL, Kafka, Redis, the API and 3 consumers were all Running and Ready, with 0
  restarts except the one deliberate liveness restart below.
- After the probe-timeout change rolled out, all 7 long-running pods had 0 restarts.
- Node memory: 2.8 GiB of 4 GiB.

**Feast score comparison:** the first 20 validation events (TransactionID 3459432-3459451)
replayed through Kafka, all scored with `features=feast`.
- **All 20 scores are bit-identical to the demo database's** stored scores: max difference
  0.0, 0 alert mismatches.
- They are also identical to the host batch (training) features and scores (max 0.0).
- The demo database stores no features. The match with batch-feature scores shows that the
  features matched too, and every event had real history (`uid_prior_txn_count` from 1 to
  8,081).

**Liveness probe:**
- `kill -STOP` on one consumer's process (from the node) at 14:20:54. The kubelet logged
  "Liveness probe failed" and "Container consumer failed liveness probe, will be restarted";
  the restart count went 0 -> 1, 115 s after the stop.
- The restarted container came back Ready with a fresh heartbeat, got partition 1, and scored
  4 of the 10 events sent afterwards.
- 30 rows in total, 30 distinct, lag 0 on all partitions, DLQ empty.

**Drift CronJob:** `kubectl create job drift-manual --from=cronjob/drift` completed in
2 min 38 s and wrote `drift_reports` row 1:
- current_rows 30, reference_rows 88,846;
- 25 of 28 features drifted (share 0.893), prediction-drift PSI 0.292;
- `triggered = true`, `promotion_decision = "not run: retraining disabled (--no-retrain;
  host-only step)"`;
- delayed-label rows 0 (with a 30-day label delay no scored event is labelled yet).

The trigger is meaningless here: the "current" window holds only the 30 replayed events
(minutes of traffic) against a 30-day reference. It shows that the job runs end to end, not
real drift.

**Interruptions:** the laptop went into Modern Standby three times (Windows Kernel-Power log):
- 14:17:21-14:17:53;
- 14:22:58-14:51:57;
- 14:57:00-15:24:52.

The cluster resumed without restarts. The second producer Job's runtime (29 min) is that
suspend. The liveness test (14:20:54-14:22:49) ran before it.

### Smoke test (2026-10-25, branch `k8s-smoke`, `FEATURE_SOURCE=store`)

- 7 pods Ready in 47 s, 0 restarts.
- `/health` ok; `/score` returned score 0.0199.
- 20 events through Kafka: 20 rows, no duplicates, lag 0, DLQ empty.
- A deleted consumer pod was replaced in 2 s, and 10 more events were scored.

## Not production

- **Single node, single replicas, no replication:** one Kafka broker (RF=1), one PostgreSQL
  without backups or failover, one Redis. A node or pod loss means downtime, and a lost volume
  means lost data.
- **Storage:** Minikube hostPath volumes, local to the node.
- **No Ingress / TLS:** the API is reached with `kubectl port-forward`. No authentication on
  Kafka, PostgreSQL or Redis inside the cluster.
- **No secrets manager:** the PostgreSQL password is a Kubernetes Secret created by hand (base64
  in etcd, not encrypted at rest here, no rotation). Production needs an external secret store
  or sealed secrets.
- **Retraining on the host:** the drift job only checks drift. Retraining and champion
  promotion need the MLflow registry, which is a local SQLite file on the host. In-cluster
  retraining needs an MLflow server with a shared artifact store.
- **Local images:** built on this machine with the model bundle baked in. Production would pull
  versioned images from a registry (the CI image at `ghcr.io/...`) and load the bundle from the
  model registry.
- **Raw data copy:** the reference data is a manual copy on the node. Production would read it
  from object storage or a feature-store offline source.
- **Operations:**
  - The Feast materialize Job has to be run by hand before traffic.
  - No PodDisruptionBudgets, no lag-based autoscaling, no metrics or alerting.
  - No manifest validation or deploy step in CI.
- **Feast concurrency:** the same known limitations as in Compose (docs/feature_store.md): a
  read-modify-write per entity, and a failed push is only logged.
