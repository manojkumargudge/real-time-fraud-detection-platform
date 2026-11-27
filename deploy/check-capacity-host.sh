#!/usr/bin/env bash
set -euo pipefail

# Run on the dedicated Linux Docker host before preparing either trial.
# MemTotal excludes memory reserved by the guest kernel.
minimum_memory_bytes=$((31 * 1024 * 1024 * 1024))
[[ $(uname -sm) == 'Linux x86_64' ]] || {
  printf 'Capacity rehearsal requires an amd64 Linux host.\n' >&2
  exit 1
}
host_cpus=$(env -u OMP_NUM_THREADS -u OMP_THREAD_LIMIT nproc)
host_memory_bytes=$(awk '/^MemTotal:/ { printf "%.0f\n", $2 * 1024 }' /proc/meminfo)
read -r docker_os docker_arch docker_cpus docker_memory_bytes < <(
  docker info --format '{{.OSType}} {{.Architecture}} {{.NCPU}} {{.MemTotal}}'
)
printf 'host_cpus=%s host_memory_bytes=%s docker_os=%s docker_arch=%s docker_cpus=%s docker_memory_bytes=%s\n' \
  "$host_cpus" "$host_memory_bytes" "$docker_os" "$docker_arch" "$docker_cpus" "$docker_memory_bytes"
if [[ $docker_os != linux || $docker_arch != x86_64 ]] ||
   (( host_cpus < 8 || docker_cpus < 8 ||
      host_memory_bytes < minimum_memory_bytes || docker_memory_bytes < minimum_memory_bytes )); then
  printf 'Capacity rehearsal requires at least 8 visible vCPUs and a 32 GiB host (31 GiB usable minimum).\n' >&2
  exit 1
fi
docker_root=$(docker info --format '{{.DockerRootDir}}')
df -PT / "$docker_root"
