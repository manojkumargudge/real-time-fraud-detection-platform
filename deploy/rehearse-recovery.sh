#!/usr/bin/env bash
set -euo pipefail

work=${1:?report directory required}
mkdir -p "$work"
work=$(cd "$work" && pwd)
mkdir "$work/checkpoint" "$work/evidence"
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
cat > "$work/rehearsal.yml" <<YAML
services:
  api:
    image: $scope-api
  consumer:
    image: $scope-api
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
      - 127.0.0.1:18443:443
YAML

stack() {
  local project=$1
  shift
  docker compose -p "$project" --profile registry --env-file /dev/null \
    -f docker-compose.yml -f docker-compose.production.yml -f "$work/rehearsal.yml" "$@"
}

capture() {
  local project=$1 label=$2 table
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
  api --fail -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
    -d '{"TransactionID":910000001,"TransactionDT":101,"TransactionAmt":50,"ProductCD":"W","card1":3999,"card4":"visa","card6":"debit","addr1":315}' \
    https://api.localhost:18443/score | jq -e '.transaction_id == 910000001 and .duplicate and .is_alert' >/dev/null
}

stack "$source_project" build api kafka postgres dashboard mlflow
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
    -v "${source_project}_$volume:/state:ro" -v "$work/checkpoint:/backup" postgres:16.16-alpine3.24 \
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
    -v "$work/checkpoint:/backup:ro" postgres:16.16-alpine3.24 \
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
