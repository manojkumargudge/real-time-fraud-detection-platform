"""Accepted-risk lists for the security scans (stdlib only; used by CI).

python security/check_ignores.py pip-audit-flags security/pip-audit-ignore.txt
    -> prints ``--ignore-vuln ID ...``; exits 1 if any entry has expired or is malformed
python security/check_ignores.py gnn-equivalents requirements-gnn.txt
    -> prints PyPI-equivalent pins for the ``+cpu`` torch wheels (pip-audit cannot resolve
       local versions), e.g. ``torch==2.8.0``; pyg-lib is not on PyPI and is skipped
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ENTRY = re.compile(
    r"^(?P<id>[A-Z]+-[\w-]+)\s*\|\s*(?P<expires>\d{4}-\d{2}-\d{2})\s*\|\s*(?P<reason>.+)$"
)


def parse_ignores(text: str) -> list[tuple[str, date, str]]:
    entries = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ENTRY.match(line)
        if not m:
            raise ValueError(f"line {n}: expected 'ID | YYYY-MM-DD | reason', got {line!r}")
        entries.append((m["id"], date.fromisoformat(m["expires"]), m["reason"].strip()))
    return entries


def pip_audit_flags(path: Path, today: date | None = None) -> list[str]:
    today = today or date.today()
    entries = parse_ignores(path.read_text(encoding="utf-8"))
    expired = [(i, e) for i, e, _ in entries if e < today]
    if expired:
        raise SystemExit(f"expired accepted risks (re-review them): {expired}")
    return [arg for i, _, _ in entries for arg in ("--ignore-vuln", i)]


def gnn_equivalents(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(torch[\w-]*)==([\d.]+)(\+\w+)?\s*$", line.strip())
        if m:
            out.append(f"{m[1]}=={m[2]}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("pip-audit-flags", "gnn-equivalents"):
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    if argv[0] == "pip-audit-flags":
        print(" ".join(pip_audit_flags(path)))
    else:
        print("\n".join(gnn_equivalents(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
