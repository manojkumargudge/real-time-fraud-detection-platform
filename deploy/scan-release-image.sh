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
  kafka|postgres) ;;
  kafka-next) source_ref=apache/kafka:4.3.2 ;;
  redis) source_ref=redis:7.4.12-alpine3.22 ;;
  caddy) source_ref=caddy:2.11.9-alpine ;;
  *) exit 2 ;;
esac

if [[ -n $source_ref ]]; then
  docker buildx imagetools inspect --raw "$source_ref" > "$out/source-manifest.json"
  digest=$(jq -er '[.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest] | if length == 1 then .[0] else error("expected exactly one amd64 manifest") end' "$out/source-manifest.json")
  ref="${source_ref}@${digest}"
  docker pull --platform linux/amd64 "$ref"
else
  docker run -d --name release-inventory-registry -p 127.0.0.1:5500:5000 registry:2 >/dev/null
  if [[ $kind == kafka || $kind == postgres ]]; then
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
