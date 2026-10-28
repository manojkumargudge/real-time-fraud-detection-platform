# Architecture

```mermaid
flowchart LR
  subgraph HOST["Host only (Windows laptop)"]
    DATA[("IEEE-CIS CSVs<br/>(Kaggle download, data/raw)")]
    FEAT["Point-in-time features<br/>src/features"]
    OFFLINE[("Feast offline store<br/>state snapshots (parquet)")]
    TRAIN["Training<br/>python -m src.models.train<br/>(--feature-source feast)"]
    MLFLOW[("MLflow registry<br/>local SQLite mlflow.db<br/>champion = production = v2")]
    GNN["GNN + fraud rings<br/>batch job src/graph"]
    RETRAIN["Retraining + promotion gate<br/>(when the drift job fires)"]
  end

  subgraph BOTH["Docker Compose demo and Minikube"]
    PRODUCER["Kafka producer<br/>(validation events, time order)"]
    KAFKA[["Kafka topic transactions"]]
    CONSUMERS["Consumers x3"]
    API["FastAPI POST /score"]
    SCORER["Scorer.score_event<br/>Feast read + XGBoost v2"]
    REDIS[("Feast online store<br/>Redis: per-entity state")]
    PG[("PostgreSQL<br/>scores, alerts, case_reviews,<br/>fraud_rings, drift_reports")]
    DASH["Streamlit dashboard<br/>alert queue, case reviews"]
    DRIFT["Evidently drift job<br/>(checks only in containers)"]
  end

  DATA --> FEAT --> OFFLINE --> TRAIN --> MLFLOW
  OFFLINE -- "materialize (cold start)" --> REDIS
  MLFLOW -- "v2 bundle (artifacts/baseline)" --> SCORER
  PRODUCER --> KAFKA --> CONSUMERS --> SCORER
  API --> SCORER
  SCORER <-- "read state / push update" --> REDIS
  SCORER --> PG
  PG <--> DASH
  DASH -- "case reviews -> labels" --> PG
  DATA --> GNN -- "fraud_rings table" --> PG
  PG --> DRIFT -- "rule fires (host run)" --> RETRAIN -- "challenger alias" --> MLFLOW
```

**Legend**

- Boxes are processes; cylinders are stores; the double-bordered box is the Kafka topic.
- **Host only:** MLflow (local SQLite file, no server), training, retraining and promotion, the
  GNN, the fraud-ring job, EDA and reports. Retraining stays on the host because the registry's
  artifact paths are Windows paths that a container cannot open.
- **Docker Compose** (`docker-compose.yml`, the full demo: 118,108 streamed events): Kafka,
  PostgreSQL, Redis, API, 3 consumers, dashboard, and a one-shot drift service (checks only).
  The producer and the Feast materialize step run from the host.
- **Minikube** (`k8s/`, namespace `fraud-k8s`, a 20-event replay): the same services, plus a
  materialize Job, a producer Job and a drift CronJob every 6 h (`--no-retrain`, checks only).
- Features: Feast stores per-entity state; training (offline store) and serving (Redis) compute
  the features from it with the same function, `entity_state.history_features`
  ([feature_store.md](feature_store.md)).
- Labels: analyst reviews are stored append-only in `case_reviews`; they are not yet used for
  retraining.
- Details: [serving.md](serving.md), [feature_store.md](feature_store.md),
  [gnn.md](gnn.md), [monitoring.md](monitoring.md), [dashboard.md](dashboard.md),
  [kubernetes.md](kubernetes.md).
