#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 && $# != 3 ]]; then
  echo "Usage: bash deploy/verify.sh API_HOSTNAME DASHBOARD_HOSTNAME [APPROVED_SYNTHETIC_EVENT_JSON]" >&2
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

if [[ $# == 3 ]]; then
  key_file=${FRAUD_VERIFY_API_KEY_FILE:?set the path to the owner-readable API key file}
  key=$(<"$key_file")
  [[ $key =~ ^[0-9a-f]{64}$ ]] || { echo 'Expected a 32-byte hex API key' >&2; exit 1; }
  event_id=$(jq -er '.TransactionID | select(type == "number" and . > 0 and . == floor)' "$3")
  original=''
  for duplicate in false true; do
    score=$(printf 'X-API-Key: %s\n' "$key" |
      curl --fail --silent --show-error --max-time 15 -H @- -H 'Content-Type: application/json' \
        --data-binary "@$3" "$api/score" |
      jq -ce --argjson id "$event_id" --argjson duplicate "$duplicate" '
        select(.transaction_id == $id and .duplicate == $duplicate and
          (.score | type == "number" and . >= 0 and . <= 1) and
          (.model_version | type == "string" and length > 0) and
          (.is_alert | type == "boolean")) | {score, model_version, is_alert}
      ')
    if [[ $duplicate == false ]]; then
      original=$score
    else
      [[ $score == "$original" ]] || { echo 'Replay differed from the original score' >&2; exit 1; }
    fi
  done
  echo 'Authenticated score and idempotent replay verified'
fi
