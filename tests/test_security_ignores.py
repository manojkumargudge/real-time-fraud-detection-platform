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
