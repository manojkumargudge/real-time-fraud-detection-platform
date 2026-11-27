#!/usr/bin/env bash
set -euo pipefail

# Use only on the isolated stack. Pass the fully specified Compose command.
(( $# > 0 )) || exit 2
for role in api consumer dashboard mlflow; do
  ids=$("$@" ps -q "$role")
  [[ -n $ids ]] || exit 1
  while read -r id; do
    docker inspect --format '{{.Name}} {{.Image}} ports={{json .HostConfig.PortBindings}}' "$id"
    docker exec -i "$id" python - "$role" <<'PY'
import json
import pathlib
import sqlite3
import sys

role = sys.argv[1]
proc = pathlib.Path("/proc")
identity = {}
for line in (proc / "1/status").read_text().splitlines():
    key, _, value = line.partition(":")
    if key in {"Uid", "CapEff", "NoNewPrivs"}:
        identity[key] = value.split()
assert identity["Uid"] == ["10001"] * 4
assert int(identity["CapEff"][0], 16) == 0 and identity["NoNewPrivs"] == ["1"]
listeners = {}
for protocol in ("tcp", "tcp6", "udp", "udp6"):
    rows = (proc / "net" / protocol).read_text().splitlines()[1:]
    # Ignore Docker's loopback DNS resolver, which is injected into the network namespace.
    listeners[protocol] = [
        row.split()[1] for row in rows
        if (protocol.startswith("udp") or row.split()[3] == "0A")
        and not row.split()[1].startswith("0B00007F:")
    ]
processes = set()
for path in proc.glob("[0-9]*/comm"):
    try:
        processes.add(path.read_text().strip())
    except FileNotFoundError:
        continue
result = {"role": role, "identity": identity, "listeners": listeners, "processes": sorted(processes)}
if role == "mlflow":
    with sqlite3.connect("file:/app/state/mlflow.db?mode=ro", uri=True) as db:
        ddl = db.execute("SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL").fetchall()
    assert ddl
    virtual = [name for _, name, sql in ddl if "VIRTUAL TABLE" in sql.upper()]
    result.update(schema_objects=len(ddl), virtual_tables=virtual)
    assert not virtual
print(json.dumps(result, sort_keys=True))
PY
  done <<< "$ids"
done
