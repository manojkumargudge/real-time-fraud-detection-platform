"""Glue between the Streamlit pages and the data layer: the repository, caching, session state.

Pages call ``repo()`` and the cached loaders below; they contain no SQL. Tests replace the
repository with a fake through ``st.session_state["repo"]``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from src.dashboard import data
from src.dashboard.db import Database

READ_TTL_S = 15  # reads may be this stale; a write clears the cache
REPO_ROOT = Path(__file__).resolve().parents[2]
# Below this many reviewed alerts the precision is not shown: with n < 30 a single review moves
# it by more than 3 points, so the number would mostly be noise.
MIN_REVIEWS_FOR_PRECISION = 30

STATUS_LABELS = {
    "open": "Open",
    "investigating": "Investigating",
    "confirmed_fraud": "Confirmed fraud",
    "false_positive": "False positive",
    "closed": "Closed",
}
# Plain column labels for every table and chart (data keys stay as they are).
COLUMN_LABELS = {
    "alert_id": "Alert",
    "transaction_id": "Transaction",
    "score": "Score",
    "status": "Status",
    "transaction_day": "Day",
    "amount": "Amount",
    "product": "Product",
    "card_id": "Card",
    "card": "Card",
    "uid": "User (uid)",
    "version": "Version",
    "field": "Field",
    "value": "Value",
    "created_at": "Time",
    "analyst": "Analyst",
    "from_status": "From",
    "to_status": "To",
    "label": "Label",
    "notes": "Notes",
    "alert_version": "Version after",
    "rank": "Rank",
    "uid_count": "Users",
    "card_count": "Cards",
    "tx_count": "Transactions",
    "fraud_rate": "Fraud rate",
    "mean_risk": "Mean risk",
    "scored_tx": "Scored transactions",
    "shared_devices": "Shared devices",
    "id": "Report",
    "run_at": "Run at",
    "window_days": "Window (days)",
    "drifted_share": "Drifted share",
    "top_drifted": "Top drifted features",
    "prediction_psi": "Prediction PSI",
    "triggered": "Triggered",
    "retraining": "Retraining / promotion",
    "synthetic": "Synthetic",
    "alerts": "Alerts",
}


def labelled(frame: Any) -> Any:
    """A copy of ``frame`` with plain column labels for display."""
    return frame.rename(columns=COLUMN_LABELS)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


@dataclass
class Repository:
    """The dashboard's view of the database; each method borrows one connection."""

    db: Database
    reports_dir: Path

    @property
    def cache_key(self) -> str:
        return self.db.dsn.rsplit("@", 1)[-1]  # host/db only: never key caches on the password

    def missing_schema(self) -> list[str]:
        with self.db.connection() as c:
            return data.missing_schema(c)

    def overview(self) -> dict[str, Any]:
        with self.db.connection() as c:
            return data.overview(c)

    def list_alerts(self, filt: data.AlertFilter, page: int, page_size: int) -> data.Page:
        with self.db.connection() as c:
            return data.list_alerts(c, filt, page, page_size)

    def get_case(self, alert_id: int) -> dict[str, Any] | None:
        with self.db.connection() as c:
            return data.get_case(c, alert_id)

    def review_history(self, alert_id: int) -> list[dict[str, Any]]:
        with self.db.connection() as c:
            return data.review_history(c, alert_id)

    def ring_for_uid(self, uid: str | None) -> dict[str, Any] | None:
        with self.db.connection() as c:
            return data.ring_for_uid(c, uid)

    def change_status(
        self,
        alert_id: int,
        expected_version: int,
        to_status: str,
        analyst: str,
        notes: str,
        *,
        submit_token: str,
    ) -> int:
        with self.db.connection() as c:
            return data.change_status(
                c, alert_id, expected_version, to_status, analyst, notes, submit_token=submit_token
            )

    def list_rings(self) -> list[dict[str, Any]]:
        with self.db.connection() as c:
            return data.list_rings(c)

    def get_ring(self, rank: int) -> dict[str, Any] | None:
        with self.db.connection() as c:
            return data.get_ring(c, rank)

    def ring_alerts(self, uids: list[str]) -> list[dict[str, Any]]:
        with self.db.connection() as c:
            return data.ring_alerts(c, uids)

    def list_drift_reports(self) -> list[dict[str, Any]]:
        with self.db.connection() as c:
            return data.list_drift_reports(c)

    def report_file(self, report_path: str | None) -> Path | None:
        return data.report_file(report_path, self.reports_dir)


@st.cache_resource
def _default_repository(dsn: str, reports_dir: str) -> Repository:
    return Repository(Database(dsn), Path(reports_dir))


def repo() -> Any:
    """The session's repository: a test fake if one was injected, else the database."""
    if "repo" in st.session_state:
        return st.session_state["repo"]
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        st.error("DATABASE_URL is not set: the dashboard needs the platform's PostgreSQL.")
        st.stop()
    reports = os.environ.get("DASHBOARD_REPORTS_DIR", str(REPO_ROOT / "reports" / "drift"))
    return _default_repository(dsn, reports)


@st.cache_data(ttl=READ_TTL_S, show_spinner=False)
def cached(_repo: Any, cache_key: str, method: str, *args: Any) -> Any:
    """``_repo.<method>(*args)``, cached for READ_TTL_S per database and arguments."""
    return getattr(_repo, method)(*args)


def load(method: str, *args: Any) -> Any:
    r = repo()
    return cached(r, r.cache_key, method, *args)


def invalidate() -> None:
    cached.clear()


def require_schema() -> None:
    """Stop the page with a clear message when the dashboard schema is missing."""
    missing = load("missing_schema")
    if missing:
        st.error(
            "The database schema is not initialised for the dashboard. Start the API or a "
            "consumer once (src/serving/store.py creates it); the dashboard never runs DDL.\n\n"
            "Missing: " + ", ".join(missing)
        )
        st.stop()


def configure_logging() -> None:
    """Send the dashboard's own log lines (one per save) to stderr, i.e. the container log."""
    log = logging.getLogger("src.dashboard")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def analyst_name() -> str:
    """The analyst name typed once per session (sidebar); no authentication."""
    return str(st.session_state.get("analyst", "")).strip()


def sidebar() -> None:
    with st.sidebar:
        # persist_state="session": since Streamlit 1.46, st.switch_page (open_case) deletes widget
        # keys at the start of the new page's run, so the name was lost on every opened case.
        st.text_input(
            "Analyst name",
            key="analyst",
            persist_state="session",
            help="Recorded on every review. No authentication: the name is self-declared.",
        )
        if not analyst_name():
            st.caption("Enter your name to change cases.")


def open_case(alert_id: int) -> None:
    st.session_state["alert_id"] = int(alert_id)
    st.switch_page("views/case.py")
