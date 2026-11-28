#!/usr/bin/env bash
set -euo pipefail

work=${1:?report directory required}
mkdir -p "$work"
work=$(cd "$work" && pwd)
mkdir "$work/checkpoint" "$work/evidence"
mkdir "$work/checkpoint/feature-seed"
evidence="$work/evidence"
scope="fraud-recovery-$(date +%s)-$$"
source_project="$scope-source"
restore_project="$scope-restored"
repo=$(pwd)
export POSTGRES_PASSWORD=synthetic-recovery-only
export API_KEY=synthetic-recovery-api-key-not-for-deployment
export DASHBOARD_COOKIE_SECRET=synthetic-recovery-cookie-not-for-deployment
export DASHBOARD_USERS="$(python -c 'import bcrypt; print("analyst:" + bcrypt.hashpw(b"fixture-password", bcrypt.gensalt()).decode())')"
export API_HOSTNAME=api.localhost DASHBOARD_HOSTNAME=dashboard.localhost ACME_EMAIL=rehearsal@example.invalid
export FEAST_SEED_CUTOFF=0
python - "$work/checkpoint/feature-seed/seed.json" <<'PY'
import json
import sys
from pathlib import Path
from src.features.feast_checkpoint import seed_metadata
from src.serving.model import ModelBundle

Path(sys.argv[1]).write_text(json.dumps({
    "metadata": seed_metadata(ModelBundle.load("artifacts/baseline"), 0),
    "events": [],
}))
PY
export FEAST_SEED_SHA256="$(sha256sum "$work/checkpoint/feature-seed/seed.json" | cut -d ' ' -f1)"
cat > "$work/rehearsal.yml" <<YAML
services:
  api:
    image: $scope-api
    volumes:
      - $work/checkpoint/feature-seed:/app/artifacts/feature-seed:ro
  consumer:
    image: $scope-api
    volumes:
      - $work/checkpoint/feature-seed:/app/artifacts/feature-seed:ro
  dashboard:
    image: $scope-dashboard
  kafka:
    image: $scope-kafka
  mlflow:
    image: $scope-mlflow
    volumes:
      - $repo/artifacts/baseline:/app/artifacts/baseline:ro
  proxy:
    ports: !override
      - 127.0.0.1:18080:80
      - 127.0.0.1:18443:443
YAML

release_override=()
backup_image=postgres:16.16-alpine3.24
if [[ -n ${FRAUD_RECOVERY_MANIFEST:-} ]]; then
  cmp "$FRAUD_RECOVERY_MANIFEST" deploy/capacity-manifests.txt
  while read -r role image; do
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image" |
      grep -Fx "$image"
    export "${role^^}_IMAGE=$image"
  done < "$FRAUD_RECOVERY_MANIFEST"
  release_override=(-f deploy/release-images.example.yml)
  backup_image=$POSTGRES_IMAGE
  cp "$FRAUD_RECOVERY_MANIFEST" "$evidence/release-manifests.txt"
fi

stack() {
  local project=$1
  shift
  docker compose -p "$project" --profile registry --env-file /dev/null \
    -f docker-compose.yml -f docker-compose.production.yml -f "$work/rehearsal.yml" \
    "${release_override[@]}" "$@"
}

capture() {
  local project=$1 label=$2 table
  stack "$project" exec -T api sh -ec '
    test "$(id -u)" -ne 0
    grep -Eq "^CapBnd:[[:space:]]+0+$" /proc/1/status
    grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/1/status
  '
  stack "$project" exec -T postgres sh -ec \
    'test "$(awk '\''/^Uid:/ {print $2}'\'' /proc/1/status)" = "$(id -u postgres)"'
  for table in scored_transactions alerts; do
    stack "$project" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U fraud -d fraud -q \
      -c "COPY (SELECT * FROM $table ORDER BY transaction_id) TO STDOUT" |
      sha256sum | cut -d ' ' -f 1 > "$evidence/$label-$table.sha256"
  done
  stack "$project" exec -T api python - snapshot < deploy/recovery-evidence.py > "$evidence/$label-stream.json"
  stack "$project" exec -T mlflow python - registry-check < deploy/recovery-evidence.py > "$evidence/$label-registry.json"
}

matches_checkpoint() {
  local label=$1 item
  for item in scored_transactions.sha256 alerts.sha256 stream.json registry.json; do
    cmp "$evidence/source-$item" "$evidence/$label-$item"
  done
}

api() {
  curl -sS --max-time 15 --cacert "$work/local-ca.crt" \
    --resolve "api.localhost:18443:127.0.0.1" "$@"
}

attest_ingress() {
  local project=$1 service id path status
  stack "$project" exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  stack "$project" exec -T proxy cat /data/caddy/pki/authorities/local/root.crt > "$work/local-ca.crt"
  api --fail https://api.localhost:18443/health
  curl -fsS --max-time 15 --cacert "$work/local-ca.crt" \
    --resolve dashboard.localhost:18443:127.0.0.1 https://dashboard.localhost:18443/_stcore/health
  for path in /docs /openapi.json /redoc /model; do
    status=$(api -o /dev/null -w '%{http_code}' "https://api.localhost:18443$path")
    [[ $status == 404 ]]
  done
  [[ $(api -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{}' https://api.localhost:18443/score) == 401 ]]
  for service in api consumer postgres kafka redis dashboard mlflow; do
    while read -r id; do
      [[ -n $id && -z $(docker port "$id") ]]
    done < <(stack "$project" ps -q "$service")
  done
  stack "$project" exec -T api python -c '
import socket
try:
    connection = socket.create_connection(("mlflow", 5000), timeout=2)
except OSError:
    pass
else:
    connection.close()
    raise SystemExit("Application network reached MLflow")
'
  api --fail -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
    -d '{"TransactionID":910000001,"TransactionDT":101,"TransactionAmt":50,"ProductCD":"W","card1":3999,"card4":"visa","card6":"debit","addr1":315}' \
    https://api.localhost:18443/score | jq -e '.transaction_id == 910000001 and .duplicate and .is_alert' >/dev/null
}

attest_proxy_restart() {
  local project=$1 redirect
  stack "$project" exec -T proxy sh -ec '
    awk "
      /^(Uid|Gid):/ { if (\$2 == 10001 && \$3 == 10001 && \$4 == 10001 && \$5 == 10001) identities++ }
      /^Cap(Eff|Bnd):/ { if (\$2 ~ /^0+\$/) capabilities++ }
      /^NoNewPrivs:/ { hardened = \$2 == 1 }
      END { exit !(identities == 2 && capabilities == 2 && hardened) }
    " /proc/1/status
    grep -E "^(Uid|Gid|CapEff|CapBnd|NoNewPrivs):" /proc/1/status
  ' > "$evidence/proxy-runtime.txt"
  redirect=$(curl -sS --max-time 15 --resolve api.localhost:18080:127.0.0.1 \
    -H 'Host: api.localhost' -o /dev/null -w '%{http_code} %{redirect_url}' \
    http://api.localhost:18080/health)
  [[ $redirect == '308 https://api.localhost/health' ]]
  printf '%s\n' "$redirect" > "$evidence/proxy-http.txt"
  stack "$project" restart proxy
  api --fail --retry 5 --retry-all-errors https://api.localhost:18443/health
  stack "$project" exec -T proxy cat /data/caddy/pki/authorities/local/root.crt |
    cmp "$work/local-ca.crt" -
  sha256sum < "$work/local-ca.crt" > "$evidence/proxy-persisted-ca.sha256"
}

if (( ${#release_override[@]} == 0 )); then
  stack "$source_project" build api kafka postgres redis dashboard mlflow proxy
fi
stack "$source_project" up -d --no-build --wait postgres redis
stack "$source_project" run --rm --no-deps -T -e FEAST_SEED_SHA256 api \
  python -m src.features.feast_checkpoint promote /app/artifacts/feature-seed/seed.json \
  --model-dir /app/artifacts/baseline --repo-dir /tmp/feast --before 0
stack "$source_project" up -d --no-build --wait --wait-timeout 200
stack "$source_project" exec -T mlflow python - registry-seed < deploy/recovery-evidence.py
stack "$source_project" exec -T api python - seed < deploy/recovery-evidence.py
ready=false
for _ in {1..35}; do
  if stack "$source_project" exec -T api python - snapshot < deploy/recovery-evidence.py > "$work/cohort.json" 2> "$evidence/cohort-last-error.log"; then ready=true; break; fi
  sleep 2
done
[[ $ready == true ]]
attest_ingress "$source_project"
attest_proxy_restart "$source_project"
stack "$source_project" stop proxy consumer api dashboard
# Only the stateless API is brought up for the read-only audit after quiescing writers.
stack "$source_project" up -d --no-deps --no-build --wait api
capture "$source_project" source
stack "$source_project" stop api kafka postgres redis mlflow
backup_start=$(date +%s)
volumes=(pgdata kafkadata redisdata mlflowdata caddydata caddyconfig)
for volume in "${volumes[@]}"; do
  docker volume inspect "${source_project}_$volume" >/dev/null
  docker run --rm --network none --entrypoint sh \
    -v "${source_project}_$volume:/state:ro" -v "$work/checkpoint:/backup" "$backup_image" \
    -ec 'tar -C /state -czf "/backup/$1.tgz" .' sh "$volume"
done
(cd "$work/checkpoint" && sha256sum ./*.tgz > "$evidence/checkpoint.sha256")
cp -a artifacts/baseline "$work/checkpoint/model-bundle"
(cd "$work/checkpoint/model-bundle" && sha256sum model.json metadata.json > "$evidence/bundle.sha256")
printf 'cold_backup_seconds=%s\n' "$(( $(date +%s) - backup_start ))" > "$evidence/timings.txt"

restore_start=$(date +%s)
for volume in "${volumes[@]}"; do
  target="${restore_project}_$volume"
  if docker volume inspect "$target" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing restore volume $target" >&2
    exit 1
  fi
  docker volume create --label "com.docker.compose.project=$restore_project" \
    --label "com.docker.compose.volume=$volume" "$target" >/dev/null
  printf 'Restoring %s\n' "$target"
  docker run --rm --network none --entrypoint sh \
    --mount "type=volume,source=$target,target=/state,volume-nocopy" \
    -v "$work/checkpoint:/backup:ro" "$backup_image" \
    -ec 'test -z "$(ls -A /state)" || { echo "Restore volume is not empty" >&2; exit 1; }; tar -C /state -xzf "/backup/$1.tgz"' sh "$volume"
done
# Rebind the restored service to the copied model rather than the source fixture directory.
sed -i "s|$repo/artifacts/baseline|$work/checkpoint/model-bundle|" "$work/rehearsal.yml"
cat > "$work/restored-model.yml" <<YAML
services:
  api:
    volumes:
      - $work/checkpoint/model-bundle:/app/artifacts/baseline:ro
  consumer:
    volumes:
      - $work/checkpoint/model-bundle:/app/artifacts/baseline:ro
YAML
stack "$restore_project" -f "$work/restored-model.yml" up -d --no-build --wait --wait-timeout 200
attest_ingress "$restore_project"
capture "$restore_project" restored
matches_checkpoint restored
printf 'restore_rto_seconds=%s\nquiesced_fixture_rpo_committed_transactions=0\n' "$(( $(date +%s) - restore_start ))" >> "$evidence/timings.txt"

rollback_start=$(date +%s)
cat > "$work/failing.yml" <<'YAML'
services:
  api:
    restart: 'no'
    command: [python, -c, "raise SystemExit(42)"]
YAML
stack "$restore_project" -f "$work/restored-model.yml" -f "$work/failing.yml" up -d --no-deps --no-build --force-recreate api
bad=$(stack "$restore_project" ps -aq api)
[[ $(docker wait "$bad") == 42 ]]
status=$(api -o /dev/null -w '%{http_code}' https://api.localhost:18443/health)
[[ $status == 502 || $status == 503 ]]
printf 'local_health_watch_detected_upgrade_failure=%s\n' "$status" >> "$evidence/timings.txt"
stack "$restore_project" -f "$work/restored-model.yml" up -d --no-deps --no-build --force-recreate --wait api
attest_ingress "$restore_project"
capture "$restore_project" rolled-back
matches_checkpoint rolled-back
printf 'rollback_rto_seconds=%s\n' "$(( $(date +%s) - rollback_start ))" >> "$evidence/timings.txt"
cat "$evidence/timings.txt"
