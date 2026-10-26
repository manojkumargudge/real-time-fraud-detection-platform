"""Analyst dashboard: workflow rules, report lookup and page smoke tests (no database).

The pages run in Streamlit's AppTest with an in-memory repository injected through
``st.session_state["repo"]`` (the same methods as ``ui.Repository``). SQL is covered by the
integration tests (tests/test_integration.py, "Dashboard data access").
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from src.dashboard import data, workflow  # noqa: E402
from src.serving.store import ALERT_STATUSES  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "src" / "dashboard" / "app.py")

ALLOWED = {
    ("open", "investigating"),
    ("open", "confirmed_fraud"),
    ("open", "false_positive"),
    ("investigating", "confirmed_fraud"),
    ("investigating", "false_positive"),
    ("confirmed_fraud", "false_positive"),
    ("confirmed_fraud", "closed"),
    ("false_positive", "confirmed_fraud"),
    ("false_positive", "closed"),
}
PAIRS = list(itertools.product(ALERT_STATUSES, repeat=2))


# --------------------------------------------------------------------------- workflow


def test_all_25_status_pairs_are_covered() -> None:
    assert len(PAIRS) == 25 and set(PAIRS) > ALLOWED


@pytest.mark.parametrize(("src", "dst"), sorted(ALLOWED))
def test_allowed_transition_returns_the_label(src: str, dst: str) -> None:
    expected = {"confirmed_fraud": "fraud", "false_positive": "not_fraud"}.get(dst)
    assert workflow.validate_transition(src, dst) == expected


@pytest.mark.parametrize(("src", "dst"), sorted(set(PAIRS) - ALLOWED))
def test_rejected_transition_raises(src: str, dst: str) -> None:
    with pytest.raises(workflow.InvalidTransitionError, match=f"from {src} to {dst}"):
        workflow.validate_transition(src, dst)


def test_next_statuses_follow_the_table_and_closed_is_terminal() -> None:
    for status in ALERT_STATUSES:
        assert set(workflow.next_statuses(status)) == {d for s, d in ALLOWED if s == status}
    assert workflow.next_statuses("closed") == ()


@pytest.mark.parametrize("bad", ["", "new", "OPEN", "confirmed"])
def test_unknown_statuses_are_rejected(bad: str) -> None:
    with pytest.raises(workflow.InvalidTransitionError, match="unknown alert status"):
        workflow.validate_transition("open", bad)
    with pytest.raises(workflow.InvalidTransitionError, match="unknown alert status"):
        workflow.validate_transition(bad, "closed")


def test_labels_follow_the_target_status_only() -> None:
    assert {s: workflow.label_for(s) for s in ALERT_STATUSES} == {
        "open": None,
        "investigating": None,
        "confirmed_fraud": "fraud",
        "false_positive": "not_fraud",
        "closed": None,
    }


# --------------------------------------------------------------------------- drift report lookup


def test_report_file_resolves_by_file_name_only(tmp_path: Path) -> None:
    (tmp_path / "drift_20261024T093720Z.html").write_text("<html></html>", encoding="utf-8")
    win = (
        r"C:\Users\x\Real-Time Financial Fraud Detection Platform\reports\drift"
        r"\drift_20261024T093720Z.html"
    )
    posix = "/app/reports/drift/drift_20261024T093720Z.html"
    assert data.report_file(win, tmp_path) == tmp_path / "drift_20261024T093720Z.html"
    assert data.report_file(posix, tmp_path) == tmp_path / "drift_20261024T093720Z.html"
    for missing in (None, "", r"C:\elsewhere\other.html", "..", r"C:\x\.."):
        assert data.report_file(missing, tmp_path) is None


# --------------------------------------------------------------------------- pages (AppTest)


NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def make_case(alert_id: int, score: float, uid: str) -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "transaction_id": 3_000_000 + alert_id,
        "score": score,
        "status": "open",
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "transaction_dt": 12_200_000 + alert_id,
        "transaction_day": 141,
        "card_id": uid.rsplit("|", 1)[0],
        "uid": uid,
        "event": {"TransactionID": 3_000_000 + alert_id, "TransactionAmt": 117.0, "ProductCD": "W"},
        "threshold": 0.1281,
        "model_version": "baseline-9fdad27ea3b0",
        "scored_at": NOW,
    }


class FakeRepository:
    """In-memory stand-in for ui.Repository, with the same rules for status changes."""

    def __init__(self, reports_dir: Path) -> None:
        self.cache_key = f"fake-{uuid.uuid4().hex}"  # never share the page cache between tests
        self.reports_dir = reports_dir
        self.missing: list[str] = []
        ring_uid = "1111|321|150|visa|226|debit|315"
        self.cases = {
            1: make_case(1, 0.97, ring_uid),
            2: make_case(2, 0.80, "2222|<NA>|150|visa|226|debit|204"),
        }
        self.reviews: dict[int, list[dict[str, Any]]] = {1: [], 2: []}
        self.ring: dict[str, Any] = {
            "rank": 1,
            "ring_id": 7,
            "uid_count": 2,
            "card_count": 2,
            "tx_count": 9,
            "total_amount": 900.0,
            "shared_devices": ["SM-N950F Build/NMF26X"],
            "mean_risk": 0.4,
            "scored_tx": 9,
            "fraud_rate": 0.25,
            "first_dt": 100,
            "last_dt": 200,
            "uids": [ring_uid, "9999|<NA>|<NA>|<NA>|<NA>|<NA>|<NA>"],
        }

    def missing_schema(self) -> list[str]:
        return self.missing

    def overview(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for c in self.cases.values():
            by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        return {
            "by_status": by_status,
            "total": len(self.cases),
            "per_day": [(141, len(self.cases))],
            "reviewed": 0,
            "confirmed_fraud": 0,
            "precision": None,
            "model_version": "baseline-9fdad27ea3b0",
            "model_last_scored_at": NOW,
        }

    def list_alerts(self, filt: data.AlertFilter, page: int, page_size: int) -> data.Page:
        rows = [
            c | {"amount": 117.0, "product": "W"}
            for c in self.cases.values()
            if (not filt.statuses or c["status"] in filt.statuses)
            and filt.min_score <= c["score"] <= filt.max_score
        ]
        rows.sort(key=lambda r: (-r["score"], r["alert_id"]))
        return data.Page(
            rows[(page - 1) * page_size : page * page_size], len(rows), page, page_size
        )

    def get_case(self, alert_id: int) -> dict[str, Any] | None:
        return dict(self.cases[alert_id]) if alert_id in self.cases else None

    def review_history(self, alert_id: int) -> list[dict[str, Any]]:
        return list(self.reviews.get(alert_id, []))

    def ring_for_uid(self, uid: str | None) -> dict[str, Any] | None:
        return self.ring if uid in self.ring["uids"] else None

    def change_status(
        self, alert_id: int, expected_version: int, to_status: str, analyst: str, notes: str
    ) -> int:
        case = self.cases[alert_id]
        if case["version"] != expected_version:
            raise data.StaleAlertError(
                alert_id, expected_version, case["status"], case["version"], "bob", NOW
            )
        label = workflow.validate_transition(case["status"], to_status)
        self.reviews[alert_id].append(
            {
                "id": len(self.reviews[alert_id]) + 1,
                "from_status": case["status"],
                "to_status": to_status,
                "label": label,
                "analyst": analyst,
                "notes": notes or None,
                "alert_version": case["version"] + 1,
                "created_at": NOW,
            }
        )
        case["status"], case["version"] = to_status, case["version"] + 1
        return int(case["version"])

    def list_rings(self) -> list[dict[str, Any]]:
        return [self.ring]

    def get_ring(self, rank: int) -> dict[str, Any] | None:
        return self.ring if rank == 1 else None

    def ring_alerts(self, uids: list[str]) -> list[dict[str, Any]]:
        return [
            {k: c[k] for k in ("alert_id", "transaction_id", "score", "status", "uid")}
            for c in self.cases.values()
            if c["uid"] in uids
        ]

    def list_drift_reports(self) -> list[dict[str, Any]]:
        return [
            {
                "id": 9,
                "run_at": NOW,
                "synthetic": False,
                "forced": True,
                "window_start_day": 134,
                "window_end_day": 141,
                "current_rows": 7000,
                "drifted_feature_share": 0.29,
                "top_drifted_features": [{"feature": "card3", "score": 0.1, "method": "w"}],
                "drifted_top_gain_features": ["card3"],
                "prediction_drift_score": 0.003,
                "prediction_drift_detected": False,
                "delayed_pr_auc": 0.32,
                "triggered": True,
                "trigger_reasons": ["top-feature drift: card3"],
                "promotion_decision": "kept champion",
                "challenger_version": "7",
                "report_path": r"C:\host\reports\drift\drift_test.html",
            }
        ]

    def report_file(self, report_path: str | None) -> Path | None:
        return data.report_file(report_path, self.reports_dir)


@pytest.fixture
def fake(tmp_path: Path) -> FakeRepository:
    (tmp_path / "drift_test.html").write_text("<html><body>evidently</body></html>", "utf-8")
    return FakeRepository(tmp_path)


def app(fake: FakeRepository, page: str | None = None, **state: Any) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["repo"] = fake
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    if page is not None:
        at.switch_page(page)
        at.run()
    return at


def texts(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [str(e.value) for e in at.error] + [str(s.value) for s in at.success]
    parts += [t.value for t in at.title] + [str(i.value) for i in at.info]
    return "\n".join(str(p) for p in parts)


@pytest.mark.parametrize(
    ("page", "title"),
    [
        (None, "Overview"),
        ("views/alerts.py", "Alert queue"),
        ("views/case.py", "Case detail"),
        ("views/rings.py", "Fraud rings"),
        ("views/drift.py", "Drift reports"),
    ],
)
def test_every_page_renders_without_exceptions(
    fake: FakeRepository, page: str | None, title: str
) -> None:
    at = app(fake, page, alert_id=1)
    assert not at.exception, at.exception
    assert at.title[0].value == title


def test_overview_shows_counts_and_model(fake: FakeRepository) -> None:
    at = app(fake)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Alerts"] == "2" and metrics["Precision of reviewed alerts"] == "n/a"
    assert metrics["Production model (newest scored row)"] == "baseline-9fdad27ea3b0"


def test_case_detail_shows_model_and_linked_ring(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Model version"] == "baseline-9fdad27ea3b0" and metrics["Status"] == "open"
    assert "Ring #1" in texts(at)
    other = app(fake, "views/case.py", alert_id=2)  # uid not a ring member: no ring section
    assert "Linked fraud ring" not in [s.value for s in other.subheader]


def test_rings_page_explains_training_period_data(fake: FakeRepository) -> None:
    at = app(fake, "views/rings.py")
    assert "training-period" in texts(at)


def test_drift_page_finds_the_report_by_file_name(fake: FakeRepository) -> None:
    at = app(fake, "views/drift.py")
    assert not at.exception
    assert any("drift_test.html" in e.label for e in at.expander)


def test_writes_need_an_analyst_name(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1)
    assert "Enter your analyst name" in texts(at)
    submit = next(b for b in at.button if b.label == "Save")
    assert submit.disabled


def test_case_status_change_is_saved_and_listed(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    at.selectbox[0].set_value("confirmed_fraud")
    at.text_area[0].input("card testing pattern")
    next(b for b in at.button if b.label == "Save").click()
    at.run()
    assert not at.exception, at.exception
    assert fake.cases[1]["status"] == "confirmed_fraud" and fake.cases[1]["version"] == 2
    (review,) = fake.reviews[1]
    assert (review["label"], review["analyst"], review["notes"]) == (
        "fraud",
        "alice",
        "card testing pattern",
    )
    assert "Saved: open -> confirmed_fraud (version 2)." in texts(at)


def test_optimistic_lock_conflict_is_rejected_with_a_clear_message(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")  # alice sees version 1
    fake.change_status(1, 1, "investigating", "bob", "taking this")  # bob saves first
    at.selectbox[0].set_value("false_positive")
    next(b for b in at.button if b.label == "Save").click()
    at.run()
    errors = [str(e.value) for e in at.error]
    assert any("was changed by bob" in e and "nothing was saved" in e for e in errors), errors
    assert fake.cases[1]["status"] == "investigating" and len(fake.reviews[1]) == 1


def test_conflict_between_two_ui_sessions_saves_nothing(fake: FakeRepository) -> None:
    """Regression (found on the live demo data): bob saves through the UI, which clears the
    shared read cache; alice's form, rendered at version 1, must still be checked against
    version 1 and must never save a status she did not choose."""
    alice = app(fake, "views/case.py", alert_id=1, analyst="alice")
    bob = app(fake, "views/case.py", alert_id=1, analyst="bob")
    bob.selectbox[0].set_value("investigating")
    next(b for b in bob.button if b.label == "Save").click()
    bob.run()
    assert fake.cases[1]["status"] == "investigating"
    alice.selectbox[0].set_value("false_positive")
    next(b for b in alice.button if b.label == "Save").click()
    alice.run()
    errors = [str(e.value) for e in alice.error]
    assert any("was changed by bob" in e and "you had version 1" in e for e in errors), errors
    assert fake.cases[1]["status"] == "investigating" and fake.cases[1]["version"] == 2
    assert [r["analyst"] for r in fake.reviews[1]] == ["bob"]
    assert {m.label: m.value for m in alice.metric}["Status"] == "investigating"  # reloaded


def test_missing_schema_stops_every_page_with_a_clear_error(fake: FakeRepository) -> None:
    fake.missing = ["alerts.version", "case_reviews.id"]
    for page in (None, "views/alerts.py", "views/case.py", "views/rings.py", "views/drift.py"):
        at = app(fake, page, alert_id=1)
        assert not at.exception
        assert any("schema is not initialised" in str(e.value) for e in at.error), page
