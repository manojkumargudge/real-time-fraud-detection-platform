# Project: Real-Time Financial Fraud Detection Platform

## Objective
Build a tested, reproducible fraud detection platform with transaction scoring,
graph-based fraud analysis, streaming ingestion, analyst review, monitoring,
and containerized deployment.

## Required stack
- Python, Pandas, XGBoost, PyTorch Geometric
- Apache Kafka, Feast, MLflow
- FastAPI, PostgreSQL
- Evidently AI, React
- Docker Compose, Kubernetes/Minikube
- pytest

## Engineering rules
- Inspect the repository and Git status before making changes.
- Implement one small, verifiable milestone at a time.
- Preserve working code and avoid unrelated rewrites.
- Use type hints, input validation, structured logging, and clear interfaces.
- Never hardcode or commit credentials, API keys, or personal data.
- Do not commit raw datasets, local environments, or generated model artifacts.
- Prevent target leakage and use appropriate validation for transaction data.
- Keep training and serving feature definitions consistent.
- Add meaningful tests for each feature and run them before reporting completion.
- Never claim tests, benchmarks, or deployments succeeded without verification.
- Ask before destructive operations or changes that may discard data.
- Avoid unnecessary dependencies, repeated explanations, and large file dumps.

## Workflow
Inspect -> plan briefly -> implement -> test -> report.

At each milestone, summarize changed files, actual test results, remaining risks,
and the next smallest task.

## Quality standard
Aim for production-style engineering, but document operational, security,
scalability, and compliance limitations honestly. Do not claim financial-sector
production readiness without appropriate evidence.
