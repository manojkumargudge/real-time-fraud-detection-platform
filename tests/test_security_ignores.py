"""Accepted-risk lists used by the CI security job (security/check_ignores.py)."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_ignores", ROOT / "security" / "check_ignores.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_accepted_risk_has_an_expiry_and_a_reason() -> None:
    ci = load()
    entries = ci.parse_ignores(
        (ROOT / "security" / "pip-audit-ignore.txt").read_text(encoding="utf-8")
    )
    assert entries and all(reason for _, _, reason in entries)
    trivy = [
        line
        for line in (ROOT / ".trivyignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert trivy and all(" exp:" in line for line in trivy)


def test_expired_entries_fail(tmp_path: Path) -> None:
    ci = load()
    f = tmp_path / "ignore.txt"
    f.write_text("PYSEC-2026-1 | 2026-01-01 | old\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="expired"):
        ci.pip_audit_flags(f, today=date(2026, 10, 23))
    f.write_text("PYSEC-2026-1 | 2027-01-01 | ok\n", encoding="utf-8")
    assert ci.pip_audit_flags(f, today=date(2026, 10, 23)) == ["--ignore-vuln", "PYSEC-2026-1"]


def test_malformed_entries_are_rejected() -> None:
    with pytest.raises(ValueError, match="expected"):
        load().parse_ignores("PYSEC-1 no expiry\n")


def test_gnn_equivalents_strip_local_versions() -> None:
    pins = load().gnn_equivalents(ROOT / "requirements-gnn.txt")
    assert "torch==2.8.0" in pins and "torch_geometric==2.8.1" in pins
    assert not any("+" in p or p.startswith("pyg") for p in pins)


# --- 14-day lead time (S9) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "fails"),
    [
        (date(2027, 1, 16), False),  # expiry 2027-01-31 = today + 15 days
        (date(2027, 1, 17), False),  # = today + 14 days: not < today + 14
        (date(2027, 1, 18), True),  # = today + 13 days
        (date(2027, 1, 31), True),  # expires today
        (date(2027, 2, 1), True),  # expired
    ],
)
def test_ci_fails_14_days_before_an_expiry(tmp_path: Path, today: date, fails: bool) -> None:
    ci = load()
    pa = tmp_path / "pip-audit-ignore.txt"
    pa.write_text("CVE-2026-1 | 2027-01-31 | reason\n", encoding="utf-8")
    tv = tmp_path / ".trivyignore"
    tv.write_text("# comment\nCVE-2026-2 exp:2027-06-30\n", encoding="utf-8")
    if fails:
        with pytest.raises(SystemExit, match=r"CVE-2026-1 \(2027-01-31\)"):
            ci.check_expiry(pa, tv, today=today)
        with pytest.raises(SystemExit, match="within 14 days"):
            ci.pip_audit_flags(pa, today=today)
    else:
        assert "earliest expiry 2027-01-31" in ci.check_expiry(pa, tv, today=today)
        assert ci.pip_audit_flags(pa, today=today) == ["--ignore-vuln", "CVE-2026-1"]


def test_trivyignore_expiry_is_checked_too(tmp_path: Path) -> None:
    ci = load()
    pa = tmp_path / "pip-audit-ignore.txt"
    pa.write_text("CVE-2026-1 | 2030-01-01 | reason\n", encoding="utf-8")
    tv = tmp_path / ".trivyignore"
    tv.write_text("CVE-2026-2 exp:2027-01-20\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"\.trivyignore.*CVE-2026-2"):
        ci.check_expiry(pa, tv, today=date(2027, 1, 10))


def test_trivyignore_entry_without_expiry_is_rejected() -> None:
    with pytest.raises(ValueError, match="exp:"):
        load().parse_trivyignore("CVE-2026-2\n")


def test_empty_lists_fail(tmp_path: Path) -> None:
    ci = load()
    pa, tv = tmp_path / "a.txt", tmp_path / "b.txt"
    pa.write_text("# none\n", encoding="utf-8")
    tv.write_text("CVE-2026-2 exp:2030-01-01\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="empty"):
        ci.check_expiry(pa, tv, today=date(2027, 1, 1))


def test_repo_accepted_risks_do_not_expire_within_14_days() -> None:
    """The real lists, today: the unit job fails too once an acceptance is 14 days from lapsing."""
    ci = load()
    summary = ci.check_expiry(
        ROOT / "security" / "pip-audit-ignore.txt", ROOT / ".trivyignore", today=date.today()
    )
    print(summary)
    assert summary.startswith("accepted risks ok")


def test_check_expiry_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ci = load()
    pa = tmp_path / "pip-audit-ignore.txt"
    pa.write_text("CVE-2026-1 | 2999-01-01 | reason\n", encoding="utf-8")
    tv = tmp_path / ".trivyignore"
    tv.write_text("CVE-2026-2 exp:2999-01-01\n", encoding="utf-8")
    assert ci.main(["check-expiry", str(pa), str(tv)]) == 0
    assert "1 pip-audit + 1 trivy" in capsys.readouterr().out
