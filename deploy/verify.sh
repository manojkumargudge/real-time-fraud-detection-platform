#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: bash deploy/verify.sh API_HOSTNAME DASHBOARD_HOSTNAME" >&2
  exit 2
fi
api="https://$1"
dashboard="https://$2"
curl --fail --silent --show-error --max-time 15 "$api/health"
curl --fail --silent --show-error --max-time 15 "$dashboard/_stcore/health"
for path in /docs /redoc /openapi.json /model; do
  status=$(curl --silent --show-error --max-time 15 --output /dev/null \
    --write-out '%{http_code}' "$api$path")
  [[ $status == 404 ]] || { echo "$path returned $status, expected 404" >&2; exit 1; }
done
status=$(curl --silent --show-error --max-time 15 --output /dev/null --write-out '%{http_code}' \
  -H 'Content-Type: application/json' --data '{}' "$api/score")
[[ $status == 401 ]] || { echo "/score returned $status, expected 401" >&2; exit 1; }
