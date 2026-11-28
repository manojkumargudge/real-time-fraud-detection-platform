#!/usr/bin/env bash
set -euo pipefail

wait_seconds=${1:-0}
[[ $wait_seconds =~ ^[0-9]+$ && $wait_seconds -le 660 ]]
deadline=$((SECONDS + wait_seconds))
while true; do
  busy=false
  now=$(date +%s)
  for timer in apt-daily.timer apt-daily-upgrade.timer; do
    next=$(systemctl show "$timer" --property=NextElapseUSecRealtime --value)
    if [[ -z $next || $next == n/a ]]; then
      echo "Waiting for $timer to report its next scheduled run"
      busy=true
      continue
    fi
    until_next=$(( $(date -d "$next" +%s) - now ))
    if (( until_next < 11040 )); then
      echo "Upcoming $timer at $next"
      (( until_next <= deadline - SECONDS )) || {
        echo 'Maintenance is outside the allowed wait' >&2
        exit 1
      }
      busy=true
    fi
  done
  for unit in apt-daily.service apt-daily-upgrade.service; do
    state=$(systemctl show "$unit" --property=ActiveState --value)
    case "$state" in
      inactive)
        [[ $(systemctl show "$unit" --property=Result --value) == success ]] || {
          echo "Maintenance failed: $unit" >&2
          exit 1
        }
        ;;
      active|activating|deactivating|reloading) busy=true ;;
      *) echo "Unsafe maintenance state: $unit ($state)" >&2; exit 1 ;;
    esac
  done
  if ! $busy; then break; fi
  (( SECONDS < deadline )) || { echo 'Maintenance did not finish inside the allowed wait' >&2; exit 1; }
  sleep 15
done

[[ ! -e /var/run/reboot-required ]] || { echo 'A security update requires a reboot' >&2; exit 1; }
uname -r
dpkg-query -W linux-image-gcp
systemctl list-timers --all apt-daily.timer apt-daily-upgrade.timer
