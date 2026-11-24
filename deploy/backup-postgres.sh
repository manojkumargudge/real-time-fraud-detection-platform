#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 1 || ! -d $1 ]]; then
  echo "Usage: bash deploy/backup-postgres.sh BACKUP_DIRECTORY" >&2
  exit 2
fi
deploy_dir=$(dirname "${BASH_SOURCE[0]}")
dump=$(mktemp "$1/fraud-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.dump")
trap 'rm -f -- "$dump"' EXIT
bash "$deploy_dir/compose.sh" exec -T postgres pg_dump -U fraud -d fraud -Fc > "$dump"
bash "$deploy_dir/compose.sh" exec -T postgres pg_restore --list < "$dump" >/dev/null
trap - EXIT
printf '%s\n' "$dump"
