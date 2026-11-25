#!/usr/bin/env bash
set -euo pipefail

# Pass the fully specified isolated Compose command, e.g. docker compose -p isolated ...
(( $# > 0 )) || exit 2
while true; do
  date -u +%FT%TZ
  free -b
  vmstat 1 2 | tail -1
  df -PT / /var/lib/docker
  mapfile -t containers < <("$@" ps -q)
  (( ${#containers[@]} > 0 )) || exit 1
  docker stats --no-stream "${containers[@]}"
  "$@" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U fraud -d fraud -At <<'SQL'
SELECT 'wal_bytes=' || wal_bytes FROM pg_stat_wal;
SELECT CASE WHEN to_regclass('pending_feast_updates') IS NULL
  THEN 'SELECT ''pending_feast=unsupported'''
  ELSE 'SELECT ''pending_feast='' || count(*) FROM pending_feast_updates'
END \gexec
SQL
  "$@" exec -T redis redis-cli INFO persistence | grep -E '^aof_(current_size|last_write_status|pending_bio_fsync|delayed_fsync)'
  "$@" exec -T redis redis-cli LATENCY LATEST
  "$@" exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:19092 --describe --group fraud-scorer
  sleep 30
done
