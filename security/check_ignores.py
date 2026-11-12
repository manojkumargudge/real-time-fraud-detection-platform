"""Accepted-risk lists for the security scans (stdlib only; used by CI).

python security/check_ignores.py pip-audit-flags security/pip-audit-ignore.txt
    -> prints ``--ignore-vuln ID ...``; exits 1 if any entry expires within LEAD_DAYS (14) days
       (or has expired) or is malformed
python security/check_ignores.py check-expiry security/pip-audit-ignore.txt .trivyignore
    -> exits 1 if any accepted risk in either file expires within LEAD_DAYS days, so CI turns
       red two weeks before an acceptance lapses instead of on the day
python security/check_ignores.py gnn-equivalents requirements-gnn.txt
    -> prints PyPI-equivalent pins for the ``+cpu`` torch wheels (pip-audit cannot resolve
       local versions), e.g. ``torch==2.8.0``; pyg-lib is not on PyPI and is skipped
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

# CI fails this many days before an accepted risk expires: expiry < today + LEAD_DAYS.
LEAD_DAYS = 14

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


TRIVY_ENTRY = re.compile(r"^(?P<id>[A-Z]+-[\w-]+)\s+exp:(?P<expires>\d{4}-\d{2}-\d{2})\s*$")


def parse_trivyignore(text: str) -> list[tuple[str, date]]:
    """``.trivyignore`` entries; every entry must carry an ``exp:`` date."""
    entries = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = TRIVY_ENTRY.match(line)
        if not m:
            raise ValueError(f"line {n}: expected 'ID exp:YYYY-MM-DD', got {line!r}")
        entries.append((m["id"], date.fromisoformat(m["expires"])))
    return entries


def expiring(
    entries: list[tuple[str, date]], today: date | None = None, lead_days: int = LEAD_DAYS
) -> list[tuple[str, date]]:
    """Entries whose expiry is before ``today + lead_days`` (expired ones included)."""
    limit = (today or date.today()) + timedelta(days=lead_days)
    return [(i, e) for i, e in entries if e < limit]


def _fail_if_expiring(source: str, entries: list[tuple[str, date]], today: date | None) -> None:
    soon = expiring(entries, today)
    if soon:
        listed = ", ".join(f"{i} ({e.isoformat()})" for i, e in soon)
        raise SystemExit(
            f"{source}: accepted risks expire within {LEAD_DAYS} days or have expired; "
            f"re-review them and update the expiry: {listed}"
        )


def pip_audit_flags(path: Path, today: date | None = None) -> list[str]:
    entries = parse_ignores(path.read_text(encoding="utf-8"))
    _fail_if_expiring(str(path), [(i, e) for i, e, _ in entries], today)
    return [arg for i, _, _ in entries for arg in ("--ignore-vuln", i)]


def check_expiry(pip_audit: Path, trivy: Path, today: date | None = None) -> str:
    """Fail (SystemExit) if any entry of either list expires within LEAD_DAYS; else a summary."""
    pa = [(i, e) for i, e, _ in parse_ignores(pip_audit.read_text(encoding="utf-8"))]
    tv = parse_trivyignore(trivy.read_text(encoding="utf-8"))
    if not pa or not tv:
        raise SystemExit(f"empty accepted-risk list: {pip_audit} {len(pa)}, {trivy} {len(tv)}")
    _fail_if_expiring(str(pip_audit), pa, today)
    _fail_if_expiring(str(trivy), tv, today)
    first = min(e for _, e in pa + tv)
    fails_from = first - timedelta(days=LEAD_DAYS - 1)  # first day with expiry < today + LEAD
    return (
        f"accepted risks ok: {len(pa)} pip-audit + {len(tv)} trivy entries; "
        f"earliest expiry {first.isoformat()} (CI fails from {fails_from.isoformat()})"
    )


def gnn_equivalents(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(torch[\w-]*)==([\d.]+)(\+\w+)?\s*$", line.strip())
        if m:
            out.append(f"{m[1]}=={m[2]}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "check-expiry":
        print(check_expiry(Path(argv[1]), Path(argv[2])))
        return 0
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
