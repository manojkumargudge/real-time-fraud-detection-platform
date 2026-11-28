# AIML-01 requirements and release evidence

This matrix uses the repository’s [brief-to-evidence audit](brief-evidence.md)
and the current implementation. The original assignment document is not in
this checkout. Implemented or tested items can have separate release gates.

| Requirement | Status | Evidence and limitation |
| --- | --- | --- |
| IEEE-CIS preprocessing, validation, class imbalance and EDA | Tested | [Dataset workflow](../data/README.md), [data quality](../reports/data_quality.md), and [EDA](../reports/eda_summary.md). The observed fraud rate is 3.499%. |
| Chronological splits and leakage prevention | Tested | Historical features exclude future and same-timestamp events. Encoders fit on training rows. [Robust evaluation](../reports/robust_evaluation.md) exercises more chronological windows. The champion headline is a validation metric. Kaggle’s blind test labels are unavailable. |
| XGBoost, precision, recall, PR-AUC, ROC-AUC and threshold | Tested | [Three-seed comparison](../reports/imbalance_comparison.md) measured class weighting. Champion validation PR-AUC 0.2992 and ROC-AUC 0.8464. At threshold 0.12806208431720734, precision is 0.3136, recall is 0.3930 and the alert rate is 4.31%. |
| Probability calibration and business metrics | Tested on validation | [Model results](../reports/model_v1_vs_v2.md) report alert budgets. The [probability audit](../reports/champion_calibration.md) measured Brier score 0.02814 and ten-bin expected calibration error 0.00608. Independent production calibration remains unverified. |
| Temporal PyG, graph construction and fraud rings | Tested | [Graph evaluation](gnn.md) and [stacking](../reports/gnn_stacking.md). GraphSAGE PR-AUC 0.2021 versus champion 0.2992. The strict ring-ranking check showed no lift. The GNN remains a challenger. |
| Reproducible artifacts and experiments | Tested | The champion bundle reproduced byte-for-byte in an archived fresh clone. [MLflow experiment tracking](../README.md#experiment-tracking) documents the run configuration and retained artifacts. |
| Kafka schema, partitioning, ordering, retries, DLQ and offsets | Tested | [Streaming and data contracts](data.md) and the hosted reliability suite cover ownership, offset commits, replay and partial failures. Sustained target-host integrity is open. |
| Idempotency and exact reconciliation | Tested | The archived replay reconciled 58,468 scores and 2,430 alerts. The original frozen store preserves the delivery discrepancies. The historical cause remains unproven. [Forensics](production-readiness.md#full-retained-kafka-cohort) |
| Feast parity, Redis recovery, concurrent writes and point-in-time state | Tested | [Feature-store tests and architecture](feature_store.md) cover versioned checkpoints, per-card locks, journal recovery and Redis read-back. |
| Feature freshness and operational monitoring | Partial | The guest probe tracks pending Feast update age and count. Production delivery, aggregate HTTP metrics, sustained lag and long-run freshness still need live coverage. |
| Persistent MLflow registry, champion and rollback | Tested in hosted recovery | Coordinated recovery verifies artifact, alias and model digests. The registry uses a dedicated internal network. Writer authorization and exact deployment-image recovery remain open. |
| Drift, report persistence and controlled retraining | Tested locally | Evidently writes an HTML report and PostgreSQL row. An isolated scheduled challenger exercised the promotion gate and preserved the champion. Automatic retraining and promotion remain disabled for release. [Monitoring runbook](production-readiness.md#monitoring-and-model-operations) |
| FastAPI endpoint security, validation, limits and readiness | Tested | The [API suite](api.md) and hosted recovery cover authentication, duplicates, hidden routes, readiness and loopback TLS. Public HTTPS and the deployed ingress limit remain open. |
| Dashboard, case review, feedback and explanations | Tested | [Analyst workflow](dashboard.md), archived Minikube sign-in and service tests cover protected case review and model contributions. Production access smoke remains open. |
| Dashboard framework | Approved | The project owner approved Streamlit for the dashboard on November 28, 2026. The [brief audit](brief-evidence.md) records the Streamlit implementation and workflow evidence. |
| Docker, Compose, network isolation, storage and secrets | Tested in isolated environments | Linux image inventories and recovery. The [runbook](production-readiness.md) covers exact digests, private ports, persistent storage and secret handling. Registry promotion and residual security approval remain open. |
| Kubernetes resources, probes, storage and jobs | Tested as a demo | [Archived Minikube results](kubernetes.md) cover readiness, 401 responses, dashboard and 20-event replay. Current manifests render with Kustomize. A production Kubernetes deployment requires separate hardening and validation. |
| GCP sizing, telemetry and incident delivery | Partial | The approved eight-vCPU host and firewall passed preflight. Dedicated telemetry has two writer roles. Complete sustained capacity and both incident states are required for release. |
| Coordinated backup, restore, failure and rollback | Tested in hosted recovery | The [recovery workflow](../.github/workflows/release-recovery.yml) compares Kafka, PostgreSQL, Redis, Feast, registry and model evidence between isolated volumes. The intended deployable digests still require matching rollout and rollback. |
| README, architecture, setup, demo and presentation | Implemented | [README](../README.md), [architecture](architecture.md), [demo](demo.md), and [interview walkthrough and reflection](interview-walkthrough.md). |

See the [release checklist](production-readiness.md) for release decisions.
