#!/usr/bin/env bash
set -euo pipefail

# Pass the isolated Compose command as arguments. stdout is safe for journald.
(( $# > 0 )) || exit 2
ca=${FRAUD_TELEMETRY_CA:?set path to the approved loopback CA}
endpoint=0 kafka=0 postgres=0 redis=0 disk=0
lag=-1 pending=-1 oldest=-1 disk_pct=-1
curl --silent --fail --max-time 5 --noproxy '*' --cacert "$ca" \
  --resolve api.localhost:18443:127.0.0.1 --output /dev/null \
  https://api.localhost:18443/health || endpoint=1
if group=$(timeout 12 "$@" exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server kafka:19092 --describe --group fraud-scorer 2>/dev/null); then
  # An assigned empty partition has no committed offset before its first event.
  lag=$(awk '$2=="transactions" && $3~/^[0-9]+$/ {
      if ($6~/^[0-9]+$/) {seen[$3]=1; sum+=$6}
      else if ($4=="-" && $5=="0" && $6=="-" && NF>=7 && $7!="-") {seen[$3]=1}
    } END {print length(seen)==3 ? sum+0 : -1}' <<< "$group")
fi
if (( lag < 0 || lag > ${FRAUD_KAFKA_LAG_LIMIT:-100} )); then kafka=1; fi
baseline=${FRAUD_CAPACITY_BASELINE:-false}
if [[ $baseline != true ]] &&
   journal=$(timeout 8 "$@" exec -T postgres psql -XAt -v ON_ERROR_STOP=1 -U fraud -d fraud \
    -c "SELECT count(*), COALESCE(floor(extract(epoch FROM now()-min(s.scored_at)))::bigint,0)
        FROM pending_feast_updates p JOIN scored_transactions s USING (transaction_id)" 2>/dev/null) &&
   [[ $journal =~ ^[0-9]+\|[0-9]+$ ]]; then
  IFS='|' read -r pending oldest <<< "$journal"
fi
if [[ $baseline != true ]] && (( oldest < 0 || oldest > ${FRAUD_FEAST_AGE_LIMIT:-60} )); then
  postgres=1
fi
if ! persistence=$(timeout 8 "$@" exec -T redis redis-cli INFO persistence 2>/dev/null) ||
   ! grep -qx 'aof_enabled:1' <<< "${persistence//$'\r'/}" ||
   ! grep -qx 'aof_last_write_status:ok' <<< "${persistence//$'\r'/}"; then redis=1; fi
if docker_root=$(timeout 8 docker info --format '{{.DockerRootDir}}' 2>/dev/null) &&
   filesystems=$(df -P / "$docker_root"); then
  disk_pct=$(awk 'NR>1 {sub(/%$/,"",$5); if($5>max) max=$5} END {print max+0}' <<< "$filesystems")
fi
if (( disk_pct < 0 || disk_pct > 70 )); then disk=1; fi
fault=$(( endpoint || kafka || postgres || redis || disk ))
printf '{"probe":"fraud_capacity","fault":%s,"endpoint_fault":%s,"kafka_fault":%s,"postgres_fault":%s,"redis_fault":%s,"disk_fault":%s,"kafka_lag":%s,"pending_feast":%s,"oldest_feast_seconds":%s,"disk_pct":%s}\n' \
  "$fault" "$endpoint" "$kafka" "$postgres" "$redis" "$disk" "$lag" "$pending" "$oldest" "$disk_pct"
