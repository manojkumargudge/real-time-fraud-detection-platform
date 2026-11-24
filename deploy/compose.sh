#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec docker compose --env-file "${FRAUD_ENV_FILE:-/etc/fraud/production.env}" \
  -f docker-compose.yml -f docker-compose.production.yml "$@"
