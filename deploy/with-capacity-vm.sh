#!/usr/bin/env bash
set -euo pipefail

project=fraud-detection-platform-2026
zone=us-central1-a
vm=fraud-release-capacity-1126
expected_id=938503370005181237
target=("$vm" --project "$project" --zone "$zone")
describe() {
  gcloud compute instances describe "${target[@]}" --format="$1"
}

(( $# > 0 )) || { echo "Usage: $0 APPROVED_OPERATOR_COMMAND [ARG ...]" >&2; exit 2; }
[[ ${FRAUD_CAPACITY_START_APPROVED:-} == "$expected_id" ]] || {
  echo 'Set FRAUD_CAPACITY_START_APPROVED to the approved instance ID after spending approval.' >&2
  exit 2
}
exec 9>"${XDG_RUNTIME_DIR:-$HOME}/.fraud-capacity-$expected_id.lock"
flock -n 9 || { echo 'A capacity operator session is already active.' >&2; exit 1; }
[[ $(describe 'value(id)') == "$expected_id" ]]
[[ $(describe 'value(status)') == TERMINATED ]]
metadata_keys=$(describe 'value(metadata.items[].key)')
[[ ";$metadata_keys;" != *";startup-script;"* ]] || {
  echo 'Remove the legacy startup script before starting.' >&2
  exit 1
}
[[ $(describe 'value(scheduling.instanceTerminationAction)') == STOP ]]
seconds=$(describe 'value(scheduling.maxRunDuration.seconds)')
[[ $seconds =~ ^[0-9]+$ ]] && (( seconds > 0 && seconds <= 14400 )) || {
  echo 'Configure the four-hour Compute Engine STOP limit before starting.' >&2
  exit 1
}

stop_vm() {
  result=$?
  trap - EXIT INT TERM HUP
  if ! timeout 180 gcloud compute instances stop "${target[@]}" --quiet ||
     [[ $(timeout 30 gcloud compute instances describe "${target[@]}" --format='value(status)') != TERMINATED ]]; then
    echo 'Direct STOP could not be confirmed. Check the VM immediately. Platform STOP remains configured.' >&2
    (( result != 0 )) || result=1
  fi
  exit "$result"
}
trap stop_vm EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
timeout 300 gcloud compute instances start "${target[@]}" --quiet
# Reserve time for evidence export inside the operator command and the final STOP.
timeout --foreground --kill-after=30s 210m "$@" 9>&-
