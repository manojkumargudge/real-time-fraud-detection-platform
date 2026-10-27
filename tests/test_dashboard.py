"""Analyst dashboard: workflow rules, report lookup and page smoke tests (no database).

The pages run in Streamlit's AppTest with an in-memory repository injected through
``st.session_state["repo"]`` (the same methods as ``ui.Repository``). SQL is covered by the
integration tests (tests/test_integration.py, "Dashboard data access").
"""

from __future__ import annotations

import itertools
import logging
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
        self.absent: set[str] = set()  # optional tables (data.OPTIONAL_COLUMNS) that do not exist
        self.calls = 0  # change_status calls, including rejected ones
        self.reviewed: tuple[int, int] = (0, 0)  # (reviewed, confirmed fraud) for the overview
        ring_uid = "1111|321|150|visa|226|debit|315"
        self.cases = {
            1: make_case(1, 0.97, ring_uid),
            2: make_case(2, 0.80, "2222|<NA>|150|visa|226|debit|204"),
        }
        self.reviews: dict[int, list[dict[str, Any]]] = {1: [], 2: []}
        self.tokens: set[str] = set()  # case_reviews.submit_token is UNIQUE in the database
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

    def schema_state(self) -> data.SchemaState:
        return data.SchemaState(missing=tuple(self.missing), absent=frozenset(self.absent))

    def overview(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for c in self.cases.values():
            by_status[c["status"]] = by_status.get(c["status"], 0) + 1
        return {
            "by_status": by_status,
            "total": len(self.cases),
            "per_day": [(141, len(self.cases))],
            "reviewed": self.reviewed[0],
            "confirmed_fraud": self.reviewed[1],
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

    def customer_alerts(
        self, uid: str | None, exclude_alert_id: int
    ) -> tuple[list[dict[str, Any]], int]:
        rows = sorted(
            (
                {k: c[k] for k in ("alert_id", "score", "status", "transaction_day")}
                for c in self.cases.values()
                if uid is not None and c["uid"] == uid and c["alert_id"] != exclude_alert_id
            ),
            key=lambda r: (-r["score"], r["alert_id"]),
        )
        return rows[: data.MAX_CUSTOMER_ALERTS], len(rows)

    def ring_for_uid(self, uid: str | None) -> dict[str, Any] | None:
        return self.ring if uid in self.ring["uids"] else None

    def change_status(
        self,
        alert_id: int,
        expected_version: int,
        to_status: str,
        analyst: str,
        notes: str,
        *,
        submit_token: str | None = None,
    ) -> int:
        self.calls += 1
        case = self.cases[alert_id]
        if submit_token is not None and submit_token in self.tokens:
            raise data.DuplicateSubmitError(alert_id, submit_token)
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
        if submit_token is not None:
            self.tokens.add(submit_token)
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


def test_overview_shows_a_tile_per_status_and_the_model(fake: FakeRepository) -> None:
    at = app(fake)
    metrics = {m.label: m.value for m in at.metric}
    assert [m.label for m in at.metric][:5] == [
        "Open",
        "Investigating",
        "Confirmed fraud",
        "False positive",
        "Closed",
    ]
    assert (metrics["Open"], metrics["Closed"]) == ("2", "0")
    assert metrics["Precision of reviewed alerts"] == "Too few reviews (n=0)"
    assert metrics["Production model"] == "9fdad27ea3b0"
    assert "model_version baseline-9fdad27ea3b0" in texts(at)
    assert "days are relative" in texts(at)
    assert at.dataframe == [] or all("Status" not in d.value.columns for d in at.dataframe)


@pytest.mark.parametrize(
    ("reviewed", "fraud", "shown"),
    [
        (29, 10, "Too few reviews (n=29)"),
        (30, 12, "40.0% (12 of 30)"),
        (1_250, 400, "32.0% (400 of 1,250)"),
    ],
)
def test_precision_needs_enough_reviews(
    fake: FakeRepository, reviewed: int, fraud: int, shown: str
) -> None:
    from src.dashboard.ui import MIN_REVIEWS_FOR_PRECISION

    assert MIN_REVIEWS_FOR_PRECISION == 30
    fake.reviewed = (reviewed, fraud)
    at = app(fake)
    assert {m.label: m.value for m in at.metric}["Precision of reviewed alerts"] == shown


def test_tables_use_plain_column_labels(fake: FakeRepository) -> None:
    queue = app(fake, "views/alerts.py")
    assert list(queue.dataframe[0].value.columns)[:4] == ["Alert", "Score", "Status", "Day"]
    case = app(fake, "views/case.py", alert_id=1)
    assert list(case.dataframe[0].value.columns) == ["Field", "Value"]
    rings = app(fake, "views/rings.py")
    assert "Fraud rate" in rings.dataframe[0].value.columns
    drift = app(fake, "views/drift.py")
    assert "Prediction PSI" in drift.dataframe[0].value.columns
    for at in (queue, case, rings, drift):
        for frame in at.dataframe:
            assert not any("_" in str(c) for c in frame.value.columns), list(frame.value.columns)


def test_case_detail_shows_model_and_linked_ring(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Model version"] == "baseline-9fdad27ea3b0" and metrics["Status"] == "Open"
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


def test_analyst_name_survives_opening_a_case(fake: FakeRepository) -> None:
    """The real path: type the name into the sidebar, then open a case from the queue.

    open_case() uses st.switch_page, which (Streamlit >= 1.46) deletes widget keys at the start
    of the new page's run; without persist_state the name was gone on every opened case.
    """
    at = app(fake)
    at.sidebar.text_input(key="analyst").input("alice").run()
    at.switch_page("views/alerts.py")
    at.run()
    next(n for n in at.number_input if n.label == "Open alert id").set_value(1)
    next(b for b in at.button if b.label == "Open case").click().run()
    assert not at.exception, at.exception
    assert at.session_state["alert_id"] == 1
    assert at.session_state["analyst"] == "alice"
    assert "Enter your analyst name" not in texts(at)
    assert "Choose a new status" in texts(at)  # Save waits only for the status now


def test_case_status_change_is_saved_and_listed(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    at.selectbox[0].set_value("confirmed_fraud")
    at.text_area[0].input("card testing pattern")
    at.run()  # choosing a status reruns the page and enables Save
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
    at.selectbox[0].set_value("false_positive").run()
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
    bob.selectbox[0].set_value("investigating").run()
    next(b for b in bob.button if b.label == "Save").click()
    bob.run()
    assert fake.cases[1]["status"] == "investigating"
    alice.selectbox[0].set_value("false_positive").run()
    next(b for b in alice.button if b.label == "Save").click()
    alice.run()
    errors = [str(e.value) for e in alice.error]
    assert any("was changed by bob" in e and "you had version 1" in e for e in errors), errors
    assert fake.cases[1]["status"] == "investigating" and fake.cases[1]["version"] == 2
    assert [r["analyst"] for r in fake.reviews[1]] == ["bob"]
    assert {m.label: m.value for m in alice.metric}["Status"] == "Investigating"  # reloaded


def test_missing_schema_stops_every_page_with_a_clear_error(fake: FakeRepository) -> None:
    fake.missing = ["alerts.version", "case_reviews.id"]
    for page in (None, "views/alerts.py", "views/case.py", "views/rings.py", "views/drift.py"):
        at = app(fake, page, alert_id=1)
        assert not at.exception
        assert any("schema is not initialised" in str(e.value) for e in at.error), page


# --------------------------------------------------------------------------- unintended saves
# docs/incidents/2026-10-26-unintended-reviews.md: reviews the analyst never chose were saved.
# Review lists are compared as snapshots: both sides must be non-empty, and counts are printed.


def all_reviews(fake: FakeRepository) -> list[tuple[int, int, str, str]]:
    return [
        (alert, r["id"], r["to_status"], r["analyst"])
        for alert, rows in sorted(fake.reviews.items())
        for r in rows
    ]


def assert_same_reviews(before: list[Any], after: list[Any]) -> None:
    print(f"case reviews: before {len(before)} rows, after {len(after)} rows")
    assert before, "the 'before' side is empty: nothing to compare"
    assert after, "the 'after' side is empty: nothing to compare"
    assert after == before


def save_button(at: AppTest) -> Any:
    return next(b for b in at.button if b.label == "Save")


def save(at: AppTest, status: str, notes: str = "") -> None:
    """A deliberate save: choose a status (the page reruns), type notes, click Save."""
    at.selectbox[0].set_value(status).run()
    if notes:
        at.text_area[0].input(notes).run()
    save_button(at).click().run()
    assert not at.exception, at.exception


def stray_submit(at: AppTest) -> None:
    """A Save trigger the analyst did not click (e.g. resent by a stale browser tab). A raw
    browser message is not bound by the button's disabled flag, so this bypasses it (AppTest
    refuses clicks on disabled buttons): the server side has to reject the save."""
    save_button(at)._value = True
    at.run()
    assert not at.exception, at.exception


def test_a_second_submit_after_a_save_writes_nothing(fake: FakeRepository) -> None:
    """Incident pattern: after a save, a submit the analyst did not make flipped the label."""
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    save(at, "confirmed_fraud", "card testing")
    before = all_reviews(fake)
    stray_submit(at)
    after = all_reviews(fake)
    assert_same_reviews(before, after)
    assert (fake.cases[1]["status"], fake.cases[1]["version"]) == ("confirmed_fraud", 2)


def test_a_save_with_a_stale_seen_version_is_rejected(fake: FakeRepository) -> None:
    """Alice chose a status while looking at version 1. Bob saves; alice's page reruns (any
    rerun, e.g. a reconnect) and shows version 2. Her save is checked against version 1."""
    alice = app(fake, "views/case.py", alert_id=1, analyst="alice")
    alice.selectbox[0].set_value("false_positive").run()
    bob = app(fake, "views/case.py", alert_id=1, analyst="bob")
    save(bob, "investigating")
    alice.run()  # a rerun without a submit: the page now shows version 2
    before = all_reviews(fake)
    save_button(alice).click().run()
    after = all_reviews(fake)
    assert_same_reviews(before, after)
    assert [r[3] for r in after] == ["bob"]
    errors = [str(e.value) for e in alice.error]
    assert any("This case changed" in e and "Reload" in e for e in errors), errors
    assert (fake.cases[1]["status"], fake.cases[1]["version"]) == ("investigating", 2)


def test_a_save_with_no_status_is_rejected(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    save(at, "investigating")  # one deliberate review, so both snapshots are non-empty
    assert at.selectbox[0].value is None  # the dropdown starts empty again after a save
    before, calls = all_reviews(fake), fake.calls
    stray_submit(at)  # Save with no status chosen (Streamlit drops it: the button is disabled)
    after = all_reviews(fake)
    assert_same_reviews(before, after)
    assert fake.calls == calls  # never reached the database
    # our own server-side check, for a submit that does reach the callback
    from src.dashboard import case_form

    state: dict[str, Any] = {}
    token = case_form.form_token(state, 1, 2, "investigating").token
    assert case_form.save(state, fake, token, "alice") == (
        "error",
        "Choose a new status; nothing was saved.",
    )
    assert all_reviews(fake) == after and fake.calls == calls


def test_changing_the_alert_id_after_a_save_writes_nothing(fake: FakeRepository) -> None:
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    save(at, "confirmed_fraud", "card testing")
    at.text_area[0].input("draft for alert 1, not saved").run()
    before = all_reviews(fake)
    at.number_input[0].set_value(2).run()
    at.run()  # plain reruns
    at.run()
    assert at.text_area[0].value == ""  # the old form's contents are discarded
    assert at.selectbox[0].value is None
    stray_submit(at)  # and a stray Save on the new alert
    after = all_reviews(fake)
    assert_same_reviews(before, after)
    assert (fake.cases[2]["status"], fake.cases[2]["version"]) == ("open", 1)


def test_save_is_disabled_until_a_name_and_a_status_are_set(fake: FakeRepository) -> None:
    nobody = app(fake, "views/case.py", alert_id=1)
    assert nobody.selectbox[0].value is None
    assert save_button(nobody).disabled and "Enter your analyst name" in texts(nobody)
    nobody.selectbox[0].set_value("investigating").run()
    assert save_button(nobody).disabled and "Enter your analyst name" in texts(nobody)
    named = app(fake, "views/case.py", alert_id=1, analyst="alice")
    assert named.selectbox[0].value is None
    assert save_button(named).disabled and "Choose a new status" in texts(named)
    named.selectbox[0].set_value("investigating").run()
    assert not save_button(named).disabled
    assert "Save is disabled" not in texts(named)


def test_a_used_token_is_never_saved_again_and_each_save_is_logged(
    fake: FakeRepository, caplog: pytest.LogCaptureFixture
) -> None:
    from src.dashboard import case_form

    state: dict[str, Any] = {}
    form = case_form.form_token(state, 1, 1, "open")
    state[case_form.widget_key("case_to_status", form.token)] = "confirmed_fraud"
    with caplog.at_level(logging.INFO, logger="src.dashboard"):
        assert case_form.save(state, fake, form.token, "alice")[0] == "success"
        before, calls = all_reviews(fake), fake.calls
        state[case_form.widget_key("case_to_status", form.token)] = "false_positive"  # resent
        kind, text = case_form.save(state, fake, form.token, "alice")
    after = all_reviews(fake)
    assert_same_reviews(before, after)
    assert kind == "error" and "no longer current" in text and fake.calls == calls
    lines = [r.getMessage() for r in caplog.records if r.name == "src.dashboard.case_form"]
    assert lines == [
        f"case save ok: alert=1 from=open to=confirmed_fraud analyst='alice' "
        f"token={form.token} version=1->2",
        f"case save rejected: alert=1 from=open to=false_positive analyst='alice' "
        f"token={form.token} version=1->None reason={text!r}",
    ]
    # the database is the backstop: the same token reaching it again writes nothing
    with pytest.raises(data.DuplicateSubmitError):
        fake.change_status(1, 2, "false_positive", "alice", "", submit_token=form.token)
    assert all_reviews(fake) == before


def test_a_new_alert_discards_the_old_token_and_its_form(fake: FakeRepository) -> None:
    from src.dashboard import case_form

    state: dict[str, Any] = {}
    first = case_form.form_token(state, 1, 1, "open")
    assert case_form.form_token(state, 1, 1, "open") == first  # stable across reruns
    state[case_form.widget_key("case_notes", first.token)] = "draft"
    second = case_form.form_token(state, 2, 1, "open")
    assert second.token != first.token
    assert case_form.widget_key("case_notes", first.token) not in state
    assert state[case_form.TOKENS][first.token].used
    assert case_form.save(state, fake, first.token, "alice")[0] == "error"
    assert fake.calls == 0


def test_case_page_has_no_form_so_enter_cannot_submit(fake: FakeRepository) -> None:
    """Enter in a form field (and Ctrl+Enter in a form's text area) submits an st.form. The case
    page has no form; committing the notes (Ctrl+Enter) only reruns the page."""
    at = app(fake, "views/case.py", alert_id=1, analyst="alice")
    assert at.get("form") == []
    save(at, "investigating")
    before = all_reviews(fake)
    at.selectbox[0].set_value("confirmed_fraud").run()
    at.text_area[0].input("typed, then Ctrl+Enter").run()
    after = all_reviews(fake)
    assert_same_reviews(before, after)


# --------------------------------------------------------------------------- display (2026-10-27)
# These drive the real user path: sidebar navigation, "Open alert id" + "Open case" (open_case /
# st.switch_page) and typing into widgets; only the in-memory repository is injected.


def start(fake: FakeRepository) -> AppTest:
    return app(fake)  # Overview, no session state besides the fake repository


def go_to(at: AppTest, page: str) -> AppTest:
    at.switch_page(page)  # what a sidebar navigation click does
    at.run()
    assert not at.exception, at.exception
    return at


def open_case_from_queue(at: AppTest, alert_id: int) -> AppTest:
    go_to(at, "views/alerts.py")
    next(n for n in at.number_input if n.label == "Open alert id").set_value(alert_id)
    next(b for b in at.button if b.label == "Open case").click().run()
    assert not at.exception, at.exception
    assert at.title[0].value == "Case detail"
    # The browser's URL now points at Case detail; AppTest would rerun the page last chosen with
    # at.switch_page (the queue), so point it at Case detail too. alert_id comes from open_case.
    at.switch_page("views/case.py")
    return at


def frame(at: AppTest, first_column: str) -> Any:
    return next(d.value for d in at.dataframe if list(d.value.columns)[0] == first_column)


def column_config(at: AppTest, first_column: str) -> dict[str, Any]:
    import json

    element = next(d for d in at.dataframe if list(d.value.columns)[0] == first_column)
    return dict(json.loads(element.proto.columns))


def test_fmt_helpers() -> None:
    from src.dashboard import fmt

    assert fmt.ist(datetime(2026, 10, 26, 9, 0, tzinfo=UTC)) == "2026-10-26 14:30 IST"
    assert fmt.ist(datetime(2026, 10, 26, 20, 0)) == "2026-10-27 01:30 IST"  # naive = UTC
    assert fmt.ist(NOW, seconds=True) == "2026-10-26 14:30:00 IST"
    assert fmt.ist(None) == ""
    assert [fmt.value(v) for v in (177.0, 529.0, 3_461_885, 1.5, "W", True)] == [
        "177",
        "529",
        "3461885",
        "1.5",
        "W",
        "True",
    ]
    assert [fmt.value(v) for v in (None, "", "  ", float("nan"))] == ["missing"] * 4
    assert [fmt.amount(v) for v in (994.0, 117, 1234.5, None)] == [
        "$994.00",
        "$117.00",
        "$1,234.50",
        "missing",
    ]
    assert [fmt.label(v) for v in ("fraud", "not_fraud", None, float("nan"), "")] == [
        "fraud",
        "not_fraud",
        "—",
        "—",
        "—",
    ]


def test_stale_alert_message_shows_ist() -> None:
    err = data.StaleAlertError(1, 1, "investigating", 2, "bob", NOW)
    assert "at 2026-10-26 14:30:00 IST" in str(err)


def test_queue_shows_human_statuses_dollar_amounts_and_no_version(fake: FakeRepository) -> None:
    fake.cases[2]["status"] = "false_positive"
    at = go_to(start(fake), "views/alerts.py")
    at.multiselect[0].set_value(["open", "false_positive"]).run()
    queue = frame(at, "Alert")
    assert list(queue.columns) == [
        "Alert",
        "Score",
        "Status",
        "Day",
        "Amount",
        "Product",
        "Card",
        "Transaction",
    ]
    assert list(queue["Status"]) == ["Open", "False positive"]
    config = column_config(at, "Alert")
    assert config["Amount"]["type_config"]["format"] == "$%.2f"
    for name in ("Alert", "Day", "Transaction"):
        assert config[name]["type_config"]["format"] == "%d", name
    assert "anonymised" in config["Product"]["help"]
    assert "anonymised card key" in config["Card"]["help"].lower()
    assert "Anonymised card key" in texts(at) and "product codes are anonymised" in texts(at)


def test_case_fields_show_whole_numbers_dollars_and_missing(fake: FakeRepository) -> None:
    fake.cases[1]["event"] = {
        "TransactionID": 3_000_001,
        "TransactionAmt": 994.0,
        "ProductCD": "W",
        "card1": 13526.0,
        "card2": 529.0,
        "addr1": 177.0,
        "dist1": None,
        "P_emaildomain": "",
        "C1": 1.5,
    }
    at = open_case_from_queue(start(fake), 1)
    fields = dict(frame(at, "Field").itertuples(index=False))
    assert fields == {
        "TransactionID": "3000001",
        "TransactionAmt": "$994.00",
        "ProductCD": "W",
        "card1": "13526",
        "card2": "529",
        "addr1": "177",
        "dist1": "missing",
        "P_emaildomain": "missing",
        "C1": "1.5",
    }
    shown = texts(at)
    assert "product codes are anonymised" in shown
    assert "Anonymised card key" in shown and "Customer (uid = card key + addr1)" in shown


def test_case_times_review_labels_and_drift_runs_are_in_ist(fake: FakeRepository) -> None:
    at = start(fake)
    at.sidebar.text_input(key="analyst").input("alice").run()
    open_case_from_queue(at, 1)
    assert "alert created 2026-10-26 14:30 IST, updated 2026-10-26 14:30 IST" in texts(at)
    save(at, "investigating")  # in-memory fake only
    history = frame(at, "Time (IST)")
    assert list(history.iloc[0][["Time (IST)", "From", "To", "Label"]]) == [
        "2026-10-26 14:30 IST",
        "Open",
        "Investigating",
        "—",
    ]
    drift = frame(go_to(at, "views/drift.py"), "Report")
    assert list(drift["Run at (IST)"]) == ["2026-10-26 14:30 IST"]


def test_other_alerts_for_this_customer_are_grouped_by_uid(fake: FakeRepository) -> None:
    uid = fake.cases[1]["uid"]
    same_card_other_region = uid.rsplit("|", 1)[0] + "|999"
    fake.cases[3] = make_case(3, 0.55, uid) | {"status": "confirmed_fraud", "transaction_day": 150}
    fake.cases[4] = make_case(4, 0.99, same_card_other_region)  # same card key, other customer
    at = open_case_from_queue(start(fake), 1)
    assert "Other alerts for this customer" in [s.value for s in at.subheader]
    others = frame(at, "Alert")
    assert list(others.columns) == ["Alert", "Score", "Status", "Day"]
    assert others.to_dict("records") == [
        {"Alert": 3, "Score": 0.55, "Status": "Confirmed fraud", "Day": 150}
    ]
    assert "Showing all 1." in texts(at)


def test_other_alerts_are_capped_at_20_and_warn_without_addr1(fake: FakeRepository) -> None:
    uid = "5555|111|150|visa|226|debit|<NA>"
    for i in range(10, 36):  # 26 alerts of one uid
        fake.cases[i] = make_case(i, 0.5 + i / 100, uid)
    at = open_case_from_queue(start(fake), 10)
    others = frame(at, "Alert")
    assert len(others) == 20
    assert list(others["Alert"])[:2] == [35, 34]  # highest score first
    assert "Showing the top 20 by score of 25." in texts(at)
    assert any("addr1 (billing region) is missing" in w.value for w in at.warning)


def test_overview_alerts_per_day_chart_starts_at_zero(fake: FakeRepository) -> None:
    import json

    at = start(fake)
    charts = at.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    assert spec["encoding"]["y"]["scale"] == {"zero": True, "domainMin": 0}
    assert "scored 2026-10-26 14:30 IST" in texts(at)


def test_drift_page_explains_an_empty_drift_reports_table(fake: FakeRepository) -> None:
    fake.list_drift_reports = lambda: []  # type: ignore[method-assign]
    at = go_to(start(fake), "views/drift.py")
    info = " ".join(str(i.value) for i in at.info)
    assert "No drift reports in this database yet" in info
    assert "create job drift-manual --from=cronjob/drift" in info
    assert at.dataframe == []
