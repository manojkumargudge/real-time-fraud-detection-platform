#!/usr/bin/env bash
set -euo pipefail

kind=${1:?image role required}
out=${2:?report directory required}
mkdir -p "$out"
out=$(cd "$out" && pwd)
local_ref="127.0.0.1:5500/fraud-release/$kind:$(git rev-parse --short HEAD)"
source_ref=
requirements=
extra=()
case "$kind" in
  serving) requirements=requirements-serving.txt ;;
  dashboard) requirements=requirements-dashboard.txt ;;
  mlflow) requirements=requirements-registry.txt ;;
  drift) requirements=requirements-monitoring.txt ;;
  retrain) requirements=requirements-retrain.txt; extra=(--build-arg EXTRA_APT_PACKAGES=git) ;;
  kafka|postgres|caddy) ;;
  kafka-next)
    extra=(--build-arg KAFKA_BASE=apache/kafka:4.3.2@sha256:8f325fc00143fed78eabe00b033a86747078bb06edc21345d06651f77ae44f30)
    ;;
  redis) source_ref=redis:7.4.12-alpine3.22 ;;
  *) exit 2 ;;
esac

if [[ -n $source_ref ]]; then
  docker buildx imagetools inspect --raw "$source_ref" > "$out/source-manifest.json"
  digest=$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest] | if length == 1 then .[0] else error("expected exactly one amd64 manifest") end' "$out/source-manifest.json")
  ref="${source_ref}@${digest}"
  docker pull --platform linux/amd64 "$ref"
else
  docker run -d --name release-inventory-registry -p 127.0.0.1:5500:5000 registry:2 >/dev/null
  if [[ $kind == kafka-next ]]; then
    docker build --platform linux/amd64 --pull -f deploy/Dockerfile.kafka "${extra[@]}" -t "$local_ref" .
  elif [[ $kind == kafka || $kind == postgres || $kind == caddy ]]; then
    docker build --platform linux/amd64 --pull -f "deploy/Dockerfile.$kind" -t "$local_ref" .
  else
    docker build --platform linux/amd64 --pull --build-arg "REQUIREMENTS=$requirements" "${extra[@]}" -t "$local_ref" .
  fi
  docker push "$local_ref"
  ref=$(docker image inspect --format '{{index .RepoDigests 0}}' "$local_ref")
fi

[[ $ref =~ @sha256:[a-f0-9]{64}$ ]]
[[ $(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$ref") == linux/amd64 ]]
printf '%s\n' "$kind $ref" > "$out/release-reference.txt"
docker image inspect "$ref" > "$out/image-inspect.json"

if [[ -n $requirements ]]; then
  docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges:true \
    --entrypoint sh "$ref" -s > "$out/python-runtime.txt" <<'SH'
set -eu
id
for binary in mount nsenter infocmp systemd-homed perl git curl; do
  printf '%s: ' "$binary"
  command -v "$binary" || printf 'not in PATH\n'
done
if test -e /usr/lib/systemd/systemd-homed; then
  dpkg-query --search /usr/lib/systemd/systemd-homed
fi
perl -MArchive::Tar -e 'print "Archive::Tar $Archive::Tar::VERSION\n"' 2>/dev/null \
  || printf 'Archive::Tar import unavailable\n'
python - <<'PY'
import pyexpat
import sqlite3
import ssl
import subprocess
import _sqlite3
import _ssl

print(ssl.OPENSSL_VERSION)
print(pyexpat.EXPAT_VERSION)
print("SQLite", sqlite3.sqlite_version)
with sqlite3.connect(":memory:") as db:
    print("SQLite compile options", ", ".join(row[0] for row in db.execute("PRAGMA compile_options")))
with open("/proc/self/status", encoding="utf-8") as status:
    for line in status:
        if line.startswith(("CapEff:", "NoNewPrivs:")):
            print(line.strip())
for module in (_sqlite3, _ssl, pyexpat):
    print(module.__file__, flush=True)
    subprocess.run(["ldd", module.__file__], check=True)
PY
SH
fi

# Preserve all high and critical findings, including unfixed and locally excepted entries.
# A scanner failure fails this job. A successful inventory still needs human disposition.
for attempt in 1 2; do
  if docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
      -v "$out:/out" -v release-inventory-trivy:/root/.cache/trivy \
      aquasec/trivy:0.75.0 image --image-src docker --platform linux/amd64 \
      --scanners vuln --list-all-pkgs --ignorefile /dev/null --timeout 16m \
      --format json --output /out/trivy.json "$ref"; then
    jq -e '.Results | length > 0' "$out/trivy.json" >/dev/null
    jq -r '[.Results[] | (.Vulnerabilities // [])[] | select(.Severity == "HIGH" or .Severity == "CRITICAL")] | sort_by(.Severity, .PkgName, .VulnerabilityID) | .[] | [.Severity, .PkgName, .InstalledVersion, .FixedVersion, .VulnerabilityID] | @tsv' "$out/trivy.json" > "$out/high-critical.tsv"
    exit 0
  fi
  if [[ $attempt == 1 ]]; then sleep 10; fi
done
exit 1
