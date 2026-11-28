#!/usr/bin/env bash
set -euo pipefail

# Install via Docker's ExecStartPre on the dedicated rehearsal guest only.
# DOCKER-USER applies to forwarded container traffic and leaves host OUTPUT alone.
for entry in 'iptables 169.254.169.254/32' 'ip6tables fd20:ce::254/128'; do
  read -r firewall address <<< "$entry"
  if ! "$firewall" -w -S DOCKER-USER >/dev/null 2>&1; then
    "$firewall" -w -N DOCKER-USER
  fi
  if ! "$firewall" -w -C DOCKER-USER -d "$address" -j REJECT >/dev/null 2>&1; then
    "$firewall" -w -I DOCKER-USER 1 -d "$address" -j REJECT
  fi
done
