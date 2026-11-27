"""Fail when a container can open a connection to a GCE metadata endpoint."""

import socket

for address in ("169.254.169.254", "fd20:ce::254"):
    try:
        connection = socket.create_connection((address, 80), timeout=2)
    except OSError:
        continue
    connection.close()
    raise SystemExit("Metadata TCP connection was available inside a workload namespace")
