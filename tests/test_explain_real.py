"""Why-flagged parity on real alerts of the demo database (opt-in, read-only, host only).

Skipped unless ``EXPLAIN_DEMO_DATABASE_URL`` points at the demo database and the feature file
(``python -m src.dashboard.explain build``) and the served bundle exist. It never runs in CI and
only reads: the connection is opened with ``default_transaction_read_only=on``. Run with::

    EXPLAIN_DEMO_DATABASE_URL=postgresql://fraud:...@localhost:15432/fraud \
        python -m pytest tests/test_explain_real.py -s
"""

from __future__ import annotations

import os

import pytest

from src.dashboard import data, explain

ALERTS = (1502, 3131, 4705, 298)
DSN = os.environ.get("EXPLAIN_DEMO_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN
    or not explain.FEATURES_FILE.is_file()
    or not (explain.MODEL_DIR / "model.json").is_file(),
    reason="needs EXPLAIN_DEMO_DATABASE_URL, the feature file and the served bundle",
)


def test_parity_holds_on_real_alerts() -> None:
    import psycopg

    assert DSN is not None
    explainer = explain.Explainer.load(explain.MODEL_DIR, explain.FEATURES_FILE)
    with psycopg.connect(DSN, options="-c default_transaction_read_only=on") as conn:
        cases = {a: data.get_case(conn, a) for a in ALERTS}
        conn.rollback()
    found = {a: c for a, c in cases.items() if c is not None}
    rows = len(explainer.features)
    print(f"rows: alerts asked {len(ALERTS)}, found {len(found)}; feature rows {rows}")
    assert len(found) == len(ALERTS) and len(explainer.features) > 0
    for alert_id, case in found.items():
        e = explainer.explain(int(case["transaction_id"]), float(case["score"]))
        assert e is not None, alert_id
        print(
            f"alert {alert_id}: |score diff| {e.score_diff:.2e}, "
            f"|sum+bias-margin| {e.margin_diff:.2e}, top {e.raising[0].label if e.raising else '-'}"
        )
        assert e.parity_ok, (alert_id, e.score_diff, e.margin_diff)
