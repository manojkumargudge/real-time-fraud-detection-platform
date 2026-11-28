#!/usr/bin/env bash
set -euo pipefail

image=${FRAUD_METADATA_PROBE_IMAGE:?set the cached serving image digest}
ids=$("$@" ps -q)
[[ -n $ids ]]
while read -r id; do
  docker inspect --format '{{.Name}}' "$id"
  docker run -i --rm --pull=never --network "container:$id" --cap-drop ALL \
    --security-opt no-new-privileges:true "$image" python - \
    < "$(dirname "$0")/check-metadata-sockets.py"
done <<< "$ids"
