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
import hashlib
import os
import pathlib
import sqlite3
import stat
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
loaded = set()
for path in proc.glob("[0-9]*/comm"):
    if path.parent.name == str(os.getpid()):
        continue
    try:
        processes.add(path.read_text().strip())
        for entry in (path.parent / "maps").read_text().splitlines():
            name = entry.rsplit("/", 1)[-1]
            if name.startswith(("libsqlite3.so", "libssl.so", "libcrypto.so", "libacl.so")):
                loaded.add(name)
    except FileNotFoundError:
        continue
    except PermissionError:
        loaded.add("maps-not-readable")
result = {"role": role, "identity": identity, "listeners": listeners,
          "processes": sorted(processes), "loaded_libraries": sorted(loaded)}
if role == "mlflow":
    path = pathlib.Path("/app/state/mlflow.db")
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == 10001
    with sqlite3.connect("file:/app/state/mlflow.db?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        ddl = db.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        virtual = [name for _, name, kind, *_ in db.execute("PRAGMA main.table_list")
                   if kind in {"virtual", "shadow"}]
        options = {row[0] for row in db.execute("PRAGMA compile_options")}
    assert ddl
    result.update(schema_objects=len(ddl), virtual_tables=sorted(virtual),
                  schema_sha256=hashlib.sha256(json.dumps(ddl).encode()).hexdigest(),
                  sqlite_version=sqlite3.sqlite_version,
                  sqlite_features=sorted(options & {"ENABLE_FTS5", "ENABLE_SESSION", "ENABLE_LOAD_EXTENSION"}),
                  database_owner=info.st_uid, database_mode=oct(stat.S_IMODE(info.st_mode)))
    assert not virtual
print(json.dumps(result, sort_keys=True))
PY
  done <<< "$ids"
done
