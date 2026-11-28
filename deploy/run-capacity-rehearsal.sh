#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/srv/fraud-capacity
session=${1:?supply a unique rehearsal ID}
[[ $session =~ ^[a-z0-9][a-z0-9-]{0,24}$ ]]
repo=${FRAUD_CAPACITY_REPO:-"$root/repo"}
baseline=${FRAUD_CAPACITY_BASELINE_IMAGE_ID:?stage the pinned main serving image before starting}
baseline_ref=fraud-capacity-main:6f1a39f
[[ $baseline =~ ^sha256:[a-f0-9]{64}$ ]]
[[ $(docker image inspect --format '{{.Id}}' "$baseline_ref") == "$baseline" ]]
[[ $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$baseline_ref") == 6f1a39f018e4c8f5bb11f9c4b051ba73d6467094 ]]
[[ $(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$baseline_ref") == linux/amd64 ]]
out="$root/evidence/trials/$session"
mkdir -p "$root/evidence/trials"
mkdir "$out"
exec > >(tee -a "$out/driver.log") 2>&1
bash "$repo/deploy/check-capacity-host.sh" > "$out/host.txt"
uname -r > "$out/kernel.txt"
cd "$repo"
printf '%s %s\n' "$baseline_ref" "$baseline" > "$out/baseline-image.txt"
test -f "$root/bootstrap.complete"
inventory_dir=${FRAUD_CAPACITY_IMAGE_INVENTORY:-"$root/evidence/images"}
if [[ -n ${FRAUD_CAPACITY_IMAGE_INVENTORY:-} ]]; then
  test -f "$inventory_dir/images.complete"
else
  test -f "$root/images.complete"
fi
test -f artifacts/feature-seed/seed.json
test "$(sha256sum artifacts/baseline/model.json | cut -d ' ' -f1)" = 9fdad27ea3b07d5c29159bae75acc3ecaa7bf671943ab543bf9385145b89ba88
test "$(sha256sum artifacts/feature-seed/seed.json | cut -d ' ' -f1)" = c044e3a5d1fedeb7cd173d4c165af676dfa7e1e6f895ae3247f5bb34e1a35f8e
export FEAST_SEED_SHA256=c044e3a5d1fedeb7cd173d4c165af676dfa7e1e6f895ae3247f5bb34e1a35f8e
export PATH="$root/venv/bin:$PATH" PYTHONPATH="$repo"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export POSTGRES_PASSWORD="$(openssl rand -hex 32)" API_KEY="$(openssl rand -hex 32)"
export DASHBOARD_COOKIE_SECRET="$(openssl rand -hex 32)"
export DASHBOARD_USERS="$(python -c 'import bcrypt; print("analyst:" + bcrypt.hashpw(b"isolated-fixture", bcrypt.gensalt()).decode())')"
export API_HOSTNAME=api.localhost DASHBOARD_HOSTNAME=dashboard.localhost ACME_EMAIL=rehearsal@example.invalid
export API_REQUESTS_PER_MINUTE=30000 FEAST_SEED_CUTOFF=12192900
manifest="$inventory_dir/release-manifests.txt"
cmp "$manifest" deploy/capacity-manifests.txt
cp "$manifest" "$out/release-manifests.txt"
while read -r role image; do
  docker pull --platform linux/amd64 "$image"
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image" | grep -Fx "$image"
  export "${role^^}_IMAGE=$image"
done < "$manifest"
sed -e 's/"TransactionDT": 200_000/"TransactionDT": 13200001/' -e 's/"card1": 10_000/"card1": 30_000/' benchmarks/sustained.py > "$out/sustained-full-seed.py"
sed -e 's/"TransactionDT": 199_000/"TransactionDT": 13200000/' deploy/capacity-stream.py > "$out/kafka-full-seed.py"
cat > "$out/loopback.yml" <<'YAML'
services:
  proxy:
    ports: !override
      - 127.0.0.1:18443:443
YAML
python - "$out/sustained-full-seed.py" <<'PY'
import runpy
import sys
from pathlib import Path
import pandas as pd
from tests.test_models import synthetic_split

raw = synthetic_split(Path("data/raw/ieee-cis"), n=12000)
path = raw / "train_transaction.csv"
ref = pd.read_csv(path)
ref["TransactionDT"] = (ref["TransactionDT"] * 2.35).astype("int64")
cursor = runpy.run_path(sys.argv[1])["SyntheticCursor"](101000000, 1024)
current = pd.DataFrame([cursor.take() for _ in range(1000)])
# Provide earlier history for the scored entities without changing the load stream.
prior = current.assign(
    TransactionID=current["TransactionID"] - 1_500_000,
    TransactionDT=current["TransactionDT"] - 3600,
)
fixture = pd.concat([prior, current], ignore_index=True)
identity_fields = ["TransactionID", "DeviceType", "DeviceInfo"]
identity_path = raw / "train_identity.csv"
identity = pd.read_csv(identity_path)
pd.concat([identity, fixture[identity_fields]], ignore_index=True).to_csv(identity_path, index=False)
fixture = fixture.drop(columns=[*identity_fields[1:], "has_identity"]).assign(isFraud=0)
pd.concat([ref, fixture], ignore_index=True).to_csv(path, index=False)
PY
install -d -m 0750 -o 10001 -g 10001 reports/drift
chmod a+rx data data/raw data/raw/ieee-cis
chmod a+r data/raw/ieee-cis/*.csv
candidate=$SERVING_IMAGE
trials=(main-1 release-1 release-2 main-2)
if [[ -n ${FRAUD_CAPACITY_RESUME_MAIN1:-} ]]; then
  prior=$(realpath "$FRAUD_CAPACITY_RESUME_MAIN1")
  [[ $prior == "$root/evidence/trials/"* && $prior != "$out" ]]
  if [[ $(awk 'NR==1 {print $2}' "$prior/main-1/iostat.log") != "$(<"$out/kernel.txt")" ]]; then
    echo 'Prior baseline used a different Linux kernel.' >&2
    exit 1
  fi
  cmp "$prior/baseline-image.txt" "$out/baseline-image.txt"
  cmp "$prior/sustained-full-seed.py" "$out/sustained-full-seed.py"
  cmp "$prior/kafka-full-seed.py" "$out/kafka-full-seed.py"
  python - "$prior/main-1" "$prior/host.txt" "$out/host.txt" <<'PY'
import json, runpy, sys
from pathlib import Path

trial = Path(sys.argv[1])
previous, current = (
    dict(field.split("=", 1) for field in Path(path).read_text().splitlines()[0].split())
    for path in sys.argv[2:]
)
for key in ("host_memory_bytes", "docker_memory_bytes"):
    # MemTotal excludes a small amount reserved by the kernel on each boot.
    if abs(int(previous.pop(key)) - int(current.pop(key))) > 1024 * 1024:
        raise ValueError(f"Prior baseline has different memory capacity: {key}")
if previous != current:
    raise ValueError("Prior baseline has different CPU or Docker geometry")
review = runpy.run_path("deploy/compare-capacity-trials.py")
levels = [review["read_level"](trial, level) for level in review["LEVELS"]]
oracle = json.loads((trial / "oracle.json").read_text())
kafka = json.loads((trial / "kafka-audit.json").read_text())
assert oracle["http"] == sum(row["warmup_requests"] + row["requests"] for row in levels)
assert oracle["score_differences"] == kafka["dlq"] == 0
assert oracle["kafka"] == kafka["kafka_scores"] == sum(kafka["ends"]) == 27000
assert oracle["rows"] == oracle["http"] + oracle["kafka"]
assert kafka["ends"] == kafka["commits"]
PY
  cp -a --reflink=auto "$prior/main-1" "$out/main-1"
  printf '%s\n' "$prior/main-1" > "$out/resumed-main-1.txt"
  trials=(release-1 release-2 main-2)
fi
for trial in "${trials[@]}"; do
  run="$out/$trial"
  mkdir "$run"
  is_baseline=false
  if [[ $trial == main-* ]]; then is_baseline=true; fi
  export SERVING_IMAGE="$candidate"
  audit=audit
  stack=(docker compose -p "fraud-capacity-$session-$trial" --profile registry --profile monitoring --env-file /dev/null -f docker-compose.yml -f docker-compose.production.yml -f deploy/release-images.example.yml -f "$out/loopback.yml")
  sampler= io= publisher= background= telemetry=
  cleanup() {
    for pid in "$sampler" "$io" "$publisher" "$background" "$telemetry"; do
      if [[ -n $pid ]]; then kill "$pid" 2>/dev/null || true; fi
    done
    "${stack[@]}" stop >/dev/null 2>&1 || true
  }
  trap cleanup EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  "${stack[@]}" config --quiet
  "${stack[@]}" config --format json | python -c '
import json, sys
services = json.load(sys.stdin)["services"]
assert all(not config.get("ports") for name, config in services.items() if name != "proxy")
assert all(config.get("network_mode") != "host" for config in services.values())
port, = services["proxy"]["ports"]
assert port["host_ip"] == "127.0.0.1" and str(port["published"]) == "18443"
assert port["target"] == 443'
  "${stack[@]}" up -d --no-build --wait postgres redis kafka
  if [[ $trial == main-1 ]]; then
    "${stack[@]}" run --rm --no-deps -T api python - 0 preflight \
      < deploy/capacity-oracle.py > "$out/oracle-preflight.txt"
  fi
  if $is_baseline; then
    "${stack[@]}" run --rm --no-deps -T -e FEAST_SEED_SHA256 api python - \
      < deploy/seed-capacity-baseline.py > "$run/generation.txt"
    export SERVING_IMAGE="$baseline_ref"
    audit=audit-main
  else
    "${stack[@]}" run --rm --no-deps -T -e FEAST_SEED_SHA256 api sh -ec 'exec python -m src.features.feast_checkpoint promote "$FEAST_SEED_ARTIFACT" --model-dir "$MODEL_DIR" --repo-dir "$FEAST_REPO_DIR" --before "$FEAST_SEED_CUTOFF"' > "$run/generation.txt"
  fi
  "${stack[@]}" up -d --no-build --wait --wait-timeout 260 api consumer dashboard mlflow proxy
  if [[ ${FRAUD_VERIFY_METADATA_ISOLATION:-0} == 1 ]]; then
    FRAUD_METADATA_PROBE_IMAGE="$candidate" bash deploy/check-container-metadata.sh \
      "${stack[@]}" > "$run/metadata-isolation.txt"
  fi
  "${stack[@]}" exec -T proxy cat /data/caddy/pki/authorities/local/root.crt > "$run/ca.crt"
  bash deploy/check-python-runtime.sh "${stack[@]}" > "$run/python-runtime.jsonl"
  "${stack[@]}" exec -T redis redis-cli CONFIG SET latency-monitor-threshold 1
  (
    export FRAUD_TELEMETRY_CA="$run/ca.crt"
    export FRAUD_CAPACITY_BASELINE="$is_baseline"
    while true; do
      bash deploy/capacity-telemetry.sh "${stack[@]}"
      sleep 30
    done
  ) > "$run/telemetry.jsonl" 2> "$run/telemetry.err" & telemetry=$!
  if [[ $trial == release-1 && ${FRAUD_TEST_NOTIFICATION:-0} == 1 ]]; then
    sleep 60
    date -u +%FT%TZ > "$run/controlled-incident-start.txt"
    "${stack[@]}" stop api
    sleep 150
    "${stack[@]}" up -d --no-build --wait --wait-timeout 260 api
    date -u +%FT%TZ > "$run/controlled-incident-recovered.txt"
    sleep 120
  fi
  bash deploy/sample-release-load.sh "${stack[@]}" > "$run/resources.log" 2>&1 & sampler=$!
  iostat -xz 30 > "$run/iostat.log" & io=$!
  "${stack[@]}" exec -T -e FRAUD_CAPACITY_FIXTURE=1 api python - produce 27000 < "$out/kafka-full-seed.py" > "$run/kafka-produced.json" 2> "$run/kafka-producer.log" & publisher=$!
  (
    sleep 300
    "${stack[@]}" exec -T postgres pg_dump -U fraud -d fraud -Fc > "$run/online-postgres.dump"
    sha256sum "$run/online-postgres.dump"
    "${stack[@]}" exec -T redis redis-cli BGREWRITEAOF
    if [[ ${FRAUD_VERIFY_METADATA_ISOLATION:-0} == 1 ]]; then
      "${stack[@]}" run --rm --no-deps -T --entrypoint sh \
        -v "$repo/deploy/check-metadata-sockets.py:/tmp/check-metadata-sockets.py:ro" drift -ec '
          python /tmp/check-metadata-sockets.py
          echo "Drift metadata isolation verified"
          exec python -m src.monitoring.run_drift --no-retrain --window-start-day 152 --window-end-day 154
        '
    else
      "${stack[@]}" run --rm --no-deps -T drift --window-start-day 152 --window-end-day 154
    fi
  ) > "$run/background.log" 2>&1 & background=$!
  for clients in 1 4 8; do
    python "$out/sustained-full-seed.py" --ca "$run/ca.crt" --clients "$clients" --first-id "$((100000000 + clients * 1000000))" --output "$run/spread-$clients.json"
  done
  python "$out/sustained-full-seed.py" --ca "$run/ca.crt" --clients 8 --cards 1 --first-id 120000000 --output "$run/hot-8.json"
  kill -0 "$sampler" "$io" "$telemetry"
  wait "$publisher"; publisher=
  wait "$background"; background=
  drained=false
  for _ in {1..18}; do
    if "${stack[@]}" exec -T -e FRAUD_CAPACITY_FIXTURE=1 api python - "$audit" 27000 < "$out/kafka-full-seed.py" > "$run/kafka-audit.json" 2> "$run/kafka-audit.log"; then
      drained=true
      break
    fi
    sleep 5
  done
  [[ $drained == true ]]
  "${stack[@]}" logs --no-color --tail 500 consumer > "$run/consumer.log" 2>&1
  expected=$(jq -s 'map(.[0] | .warmup_requests + .requests) | add' "$run"/spread-*.json "$run"/hot-8.json)
  if $is_baseline; then
    SERVING_IMAGE="$candidate" "${stack[@]}" run --rm --no-deps -T api python - "$expected" baseline \
      < deploy/capacity-oracle.py > "$run/oracle.json"
  else
    "${stack[@]}" exec -T api python - "$expected" < deploy/capacity-oracle.py > "$run/oracle.json"
  fi
  python - "$run" <<'PY'
import json, re, sys
from datetime import datetime
from pathlib import Path
from statistics import quantiles
run = Path(sys.argv[1])
log = (run / "resources.log").read_text()
levels = [json.loads(path.read_text())[0] for path in [*run.glob("spread-*.json"), run / "hot-8.json"]]
assert len(levels) == 4
intervals = [(r["measured_start_unix"], r["measured_end_unix"]) for r in levels]
cpu, memory, disk = [], [], []
measured = False
for line in log.splitlines():
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line):
        stamp = datetime.fromisoformat(line.replace("Z", "+00:00")).timestamp()
        measured = any(start <= stamp < end for start, end in intervals)
    if not measured:
        continue
    parts = line.split()
    if len(parts) >= 17 and all(x.isdigit() for x in parts):
        cpu.append(100 - int(parts[14]))
    if line.startswith("Mem:"):
        memory.append(100 * (1 - int(parts[-1]) / int(parts[1])))
    usage = re.fullmatch(r"\S+\s+\S+\s+\d+\s+\d+\s+\d+\s+(\d+)%\s+/.*", line)
    if usage:
        disk.append(int(usage[1]))
consumers = [line for line in log.splitlines() if "-consumer-" in line and " restarts=" in line]
assert len({line.split()[0] for line in consumers}) == 3
assert all(line.endswith("status=running restarts=0") for line in consumers)
assert len(cpu) >= 70 and memory and disk
summary = {"cpu_p95": quantiles(cpu, n=100, method="inclusive")[94],
           "memory_peak_pct": max(memory), "disk_peak_pct": max(disk), "samples": len(cpu)}
(run / "capacity.json").write_text(json.dumps(summary))
if run.name.startswith("release-"):
    assert summary["cpu_p95"] <= 70 and summary["memory_peak_pct"] <= 70 and summary["disk_peak_pct"] <= 70
PY
  cleanup
  trap - EXIT
  touch "$run/complete"
done
python deploy/compare-capacity-trials.py "$out" > "$out/comparison.json"
touch "$out/complete"
