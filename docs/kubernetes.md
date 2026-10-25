# Kubernetes smoke test (Minikube)

A smoke test, not the production deployment: it proves that the platform runs on Kubernetes and
scores events end to end. Everything lives in the namespace `fraud-k8s`. It is fully separate
from the two Compose stacks; the Minikube node is its own container on the Docker VM.

**Why Minikube:** the brief allows Minikube or an equivalent. Docker Desktop's built-in
Kubernetes was tried first, but it can only be switched on in the Docker Desktop UI (no CLI or
settings key on Docker Desktop 4.55). Minikube on the Docker driver runs next to the Compose
stacks on the same Docker Desktop.

## What is deployed (`k8s/`, `kubectl apply -k k8s`)

| Component | Kind | Image | Storage | Probes | Requests / limits |
|---|---|---|---|---|---|
| PostgreSQL 16.4 | Deployment + Service `postgres:5432` | `postgres:16.4-alpine` | PVC 1 Gi | `pg_isready` (ready + live) | 100m / 192 Mi, 500m / 384 Mi |
| Kafka 3.8.1, single-node KRaft | Deployment + Service `kafka:9092` | `apache/kafka:3.8.1` | PVC 1 Gi | TCP 9092 (ready + live) | 200m / 512 Mi, 1 / 768 Mi (heap 256-384 MB) |
| Redis 7.4.1 | Deployment + Service `redis:6379` | `redis:7.4.1-alpine` | PVC 512 Mi (AOF) | `redis-cli ping` (ready + live) | 50m / 64 Mi, 200m / 128 Mi |
| API (FastAPI) | Deployment + Service `api:8000` | `fraud-serving:k8s-local` | - | HTTP `/health` (ready; live after 60 s of failures) | 100m / 256 Mi, 1 / 512 Mi |
| Consumer x3 | Deployment (one per partition) | `fraud-serving:k8s-local` | - | none (no HTTP endpoint) | 100m / 256 Mi, 1 / 512 Mi each |
| Producer (one-off) | Job `k8s/jobs/produce-events.yaml` | `fraud-serving:k8s-local` | ConfigMap with events | - | 50m / 128 Mi, 500m / 256 Mi |

Notes:
- **Settings:** ConfigMap `fraud-app-config` holds them, with `FEATURE_SOURCE=store`. This
  smoke test reads history from PostgreSQL and does no Feast materialization; Redis runs but
  is not read.
- **Password:** Secret `fraud-postgres`, created by a command at deploy time and never written
  to a file. `DATABASE_URL` is assembled from it in the pod spec (`$(POSTGRES_PASSWORD)`).
- **`enableServiceLinks: false`** on every pod. Otherwise Kubernetes injects variables such as
  `KAFKA_PORT=tcp://...` for the `kafka` Service, and the apache/kafka image turns every
  `KAFKA_*` variable into a broker setting.
- **Init containers:**
  - Kafka chowns its data directory to uid 1000 first, because Minikube's hostPath volumes
    ignore `fsGroup`.
  - The API and the consumers wait for PostgreSQL (and Kafka) before starting, instead of
    crash-looping.
- **Image `fraud-serving:k8s-local`:** the serving image (`Dockerfile`) plus the model bundle
  from `artifacts/baseline` (`Dockerfile.k8s`; `Dockerfile.k8s.dockerignore` admits only the
  two bundle files).
  - Built and loaded into Minikube by `k8s/build-image.sh`; `imagePullPolicy: IfNotPresent`.
  - Never pushed. The bundle stays gitignored.
- **Requests and limits:** requests total about 2.0 Gi of memory, limits about 3.5 Gi, which
  fits the 4 GB Minikube node.

## Prerequisites

- Docker Desktop running (with WSL 2), Minikube 1.39 or later (`winget install -e --id
  Kubernetes.minikube`).
- `minikube start --driver=docker --cpus=4 --memory=4g` gives Kubernetes 1.37 on one node,
  with the `standard` storage class. The Docker VM had 10 GB (`.wslconfig`: `memory=10GB`).
- The model bundle in `artifacts/baseline` (`python -m src.models.train`), and the IEEE-CIS
  CSVs for the test events (data/README.md).

## Deploy

```bash
k8s/build-image.sh                     # build fraud-serving:k8s-local, minikube image load
# optional, avoids pulls from Docker Hub inside Minikube:
for i in postgres:16.4-alpine apache/kafka:3.8.1 redis:7.4.1-alpine; do minikube image load $i; done
kubectl apply -f k8s/namespace.yaml
kubectl -n fraud-k8s create secret generic fraud-postgres --from-literal=password="$(openssl rand -hex 24)"
kubectl apply -k k8s
for d in postgres redis kafka api consumer; do kubectl -n fraud-k8s rollout status deploy/$d; done
```

## Verify

```bash
kubectl -n fraud-k8s get pods                                    # all Running, 1/1, restarts
kubectl -n fraud-k8s port-forward svc/api 18080:8000 &
curl http://localhost:18080/health                               # status ok, store_ok true
curl -X POST http://localhost:18080/score -H 'Content-Type: application/json' \
  -d '{"TransactionID": 999000001, "TransactionDT": 15800000, "TransactionAmt": 117.0, "ProductCD": "W", "card1": 9500, "card2": 321.0, "card4": "visa", "card6": "debit", "addr1": 315.0, "has_identity": false}'

# 20 validation events through Kafka (the same events as producer --skip 0 --limit 20):
python -m scripts.k8s_events --limit 20                          # -> k8s/local/events.json (gitignored)
kubectl -n fraud-k8s create configmap smoke-events --from-file=events.json=k8s/local/events.json
kubectl apply -f k8s/jobs/produce-events.yaml
kubectl -n fraud-k8s wait --for=condition=complete job/produce-events

PG=$(kubectl -n fraud-k8s get pod -l app=postgres -o name)
kubectl -n fraud-k8s exec $PG -- psql -U fraud -d fraud -c \
  "select count(*), count(distinct transaction_id) from scored_transactions"
KF=$(kubectl -n fraud-k8s get pod -l app=kafka -o name)          # Git Bash: MSYS_NO_PATHCONV=1
kubectl -n fraud-k8s exec -c kafka $KF -- /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --group fraud-scorer
kubectl -n fraud-k8s exec -c kafka $KF -- /opt/kafka/bin/kafka-get-offsets.sh \
  --bootstrap-server localhost:9092 --topic transactions.dlq

kubectl -n fraud-k8s delete pod <one consumer pod>               # replaced by the Deployment
```

## Delete

```bash
kubectl delete namespace fraud-k8s     # everything, PVCs included (throwaway)
minikube stop                          # keeps the cluster; `minikube delete` removes it
```

## Results (2026-10-25, Minikube 1.39.0, Kubernetes 1.37.0, branch `k8s-smoke`)

**Rollout:**
- All 7 pods (PostgreSQL, Kafka, Redis, API, 3 consumers) were Running and Ready within 47 s
  of `kubectl apply -k`, with 0 restarts.
- All 3 PVCs were bound.
- The Minikube node used 1.96 GiB of its 4 GiB.

**API, through `port-forward`:**
- `/health`: HTTP 200, `status: ok`, `store_ok: true`, model `baseline-9fdad27ea3b0`.
- `POST /score` with TransactionID 999000001: HTTP 200, score 0.0199, not an alert
  (threshold 0.1281), 54 ms.

**Kafka path:** the Job published the first 20 validation events (TransactionID
3459432-3459451) in 9 s.
- All 20 were stored within about 5 s.
- `scored_transactions` held 21 rows (20 + the API event), all transaction IDs distinct.
- Lag 0 on all 3 partitions (7 + 5 + 8 messages); DLQ `transactions.dlq` empty.

**Consumer kill:**
- `kubectl delete pod` on one consumer: the Deployment created a replacement in the same
  second and was back to 3/3 available 2 s after the delete.
- The new pod was assigned partition 2, 8 s after the delete.
- 10 more events (TransactionID 3459452-3459461), sent after the kill, were all stored: 31
  rows in total, all distinct.
- Lag 0 (11 + 9 + 10 messages), DLQ empty, 0 restarts on every pod; the new pod scored 2 of
  them.

**Not covered:**
- Scores are not compared with the demo: the store path starts with an empty history here.
- No load or throughput figures.
- No node failure (one node).

## What the full deployment still needs

- **Feast materialize Job:** `python -m src.features.feast_store materialize --before validation`
  into the in-cluster Redis, then `FEATURE_SOURCE=feast` with `FEAST_REDIS=redis:6379` (the
  default, and the path without train/serve skew).
- **Drift CronJob:** `src.monitoring.run_drift --no-retrain`, with the reference data and
  `reports/drift` on a volume.
- **MLflow server for retraining:** a tracking server with a shared artifact store. Today the
  registry is a local SQLite file whose artifact paths are Windows `file:///C:/...` paths, so
  retraining runs on the host only.
- **Ingress (or a LoadBalancer) for the API,** with TLS. This smoke test uses `port-forward`.
- **Real secrets management** (an external secret store / sealed secrets, rotation) instead of a
  Secret created by hand.
- **Production-grade infrastructure:** Kafka with replication (StatefulSets, RF >= 3, auth/TLS)
  or a managed broker, PostgreSQL with backups (operator or managed), Redis persistence policy.
- **Images from a registry** (the CI image at `ghcr.io/...`) with the model bundle delivered
  from the model registry, not baked into a local image.
- **Kubernetes manifests in CI:** validation (`kubectl apply --dry-run=server` / kubeconform) and
  a deploy step. Nothing in CI deploys yet.
- **Operations:** consumer health and lag-based autoscaling (KEDA or a lag exporter), metrics
  and alerting, PodDisruptionBudgets.
