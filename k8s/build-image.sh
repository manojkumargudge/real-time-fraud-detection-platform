#!/usr/bin/env bash
# Build the local Kubernetes images (model bundle baked in) and load them into Minikube:
#   fraud-serving:k8s-local     API, consumers, producer and Feast materialize Jobs
#   fraud-monitoring:k8s-local  drift CronJob (serving runtime + Evidently)
# Usage (repo root, Git Bash): k8s/build-image.sh        Never pushes the images.
set -euo pipefail
cd "$(dirname "$0")/.."
MINIKUBE="${MINIKUBE:-minikube}"
test -f artifacts/baseline/model.json || { echo "missing artifacts/baseline (python -m src.models.train)"; exit 1; }
docker build -t fraud-serving:base-local -f Dockerfile .
docker build -t fraud-monitoring:base-local -f Dockerfile --build-arg REQUIREMENTS=requirements-monitoring.txt .
for name in serving monitoring; do
  docker build -t "fraud-$name:k8s-local" -f Dockerfile.k8s --build-arg "BASE_IMAGE=fraud-$name:base-local" .
  "$MINIKUBE" image load "fraud-$name:k8s-local"
done
"$MINIKUBE" image ls | grep -E "fraud-(serving|monitoring):k8s-local"
