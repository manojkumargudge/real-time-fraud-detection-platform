#!/usr/bin/env bash
# Build fraud-serving:k8s-local (serving image + baked-in model bundle) and load it into Minikube.
# Usage (repo root, Git Bash): k8s/build-image.sh        Never pushes the image.
set -euo pipefail
cd "$(dirname "$0")/.."
MINIKUBE="${MINIKUBE:-minikube}"
test -f artifacts/baseline/model.json || { echo "missing artifacts/baseline (python -m src.models.train)"; exit 1; }
docker build -t fraud-serving:base-local -f Dockerfile .
docker build -t fraud-serving:k8s-local -f Dockerfile.k8s --build-arg BASE_IMAGE=fraud-serving:base-local .
"$MINIKUBE" image load fraud-serving:k8s-local
"$MINIKUBE" image ls | grep -F "fraud-serving:k8s-local"
