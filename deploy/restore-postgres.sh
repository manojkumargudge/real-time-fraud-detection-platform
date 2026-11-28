#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ! $1 =~ ^fraud-restore-[a-z0-9-]+$ || ! -s $2 ]]; then
  echo "Usage: bash deploy/restore-postgres.sh fraud-restore-NAME BACKUP_FILE" >&2
  exit 2
fi
deploy_dir=$(dirname "${BASH_SOURCE[0]}")
compose=(bash "$deploy_dir/compose.sh" -p "$1")
"${compose[@]}" up -d --wait postgres
existing=$("${compose[@]}" exec -T postgres psql -U fraud -d fraud -Atc \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')")
if [[ $existing != 0 ]]; then
  echo "Restore requires an empty target database." >&2
  exit 1
fi
"${compose[@]}" exec -T postgres pg_restore --exit-on-error --single-transaction \
  --no-owner --no-privileges -U fraud -d fraud < "$2"
"${compose[@]}" exec -T postgres psql -U fraud -d fraud -v ON_ERROR_STOP=1 \
  -c 'SELECT count(*) AS scored_transactions FROM scored_transactions;'
