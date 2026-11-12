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
    "feature": "Feature",
    "contribution": "Contribution (log-odds)",
    "created_at": "Time (IST)",
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
    "run_at": "Run at (IST)",
    "window_days": "Window (days, last day included)",
    "drifted_share": "Drifted share",
    "top_drifted": "Top drifted features",
    "prediction_psi": "Prediction PSI",
    "retrain": "Retrain",
    "retraining": "Retraining / promotion",
    "synthetic": "Synthetic",
    "alerts": "Alerts",
}


PRODUCT_HELP = "ProductCD: product codes are anonymised in the IEEE-CIS data (W, C, R, H, S)."
CARD_HELP = (
    "Anonymised card key (card1-card6 of the IEEE-CIS data), not a customer: one card key is "
    "shared by many customers."
)
RINGS_NOT_RUN = (
    "Fraud ring detection has not run in this deployment (it is a batch job run on the host)."
)
NOT_AVAILABLE = "—"  # a ring or drift value from a batch job that has not run here
EXPLAIN_NOT_RUN = (
    "No explanation data in this deployment: the feature file is built on the host "
    "(python -m src.dashboard.explain build) and mounted with the served model."
)
DRIFT_ROW8_NOTE = (
    "Row 8 was a --force-retrain run recorded before the forced flag was fixed (a forced run "
    "whose rule also fired was stored as not forced), so its stored flag says not forced. "
    "Rows 5-9 ran on 2026-10-24 before the trigger rule was committed and frozen (08b4b45, "
    "16:50 IST)."
)
DRIFT_RUNS_NOTE = (
    "These are the Oct 24 development runs. The clean trigger evidence (default window did not "
    "fire; days 170-181, last day included, fired without --force) is in "
    "docs/evidence/2026-10-27-drift-trigger/."
)


def window_label(start_day: float, end_day: float) -> str:
    """Drift windows are stored as closed intervals; ``*_day`` is the stored second / 86,400, so
    the floor of each end is the first and the last day included."""
    return f"{int(start_day // 1)}-{int(end_day // 1)}"


def retrain_label(report: dict[str, Any]) -> str:
    """Why retraining ran for a drift report, from the stored flags."""
    if str(report.get("promotion_decision") or "").startswith("blocked: dirty tree"):
        return "blocked (dirty tree)"
    if report["forced"]:
        return "forced"
    return "rule fired" if report["triggered"] else "no"


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

    def schema_state(self) -> data.SchemaState:
        with self.db.connection() as c:
            return data.schema_state(c)

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

    def customer_alerts(
        self, uid: str | None, exclude_alert_id: int
    ) -> tuple[list[dict[str, Any]], int]:
        with self.db.connection() as c:
            return data.customer_alerts(c, uid, exclude_alert_id)

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


@st.cache_resource(show_spinner="Loading the model and the feature file...")
def _explainer(model_dir: str, features_file: str) -> Any:
    from src.dashboard.explain import Explainer  # xgboost: only when the panel is shown

    if not Path(features_file).is_file() or not (Path(model_dir) / "model.json").is_file():
        return None
    return Explainer.load(Path(model_dir), Path(features_file))


def explainer() -> Any:
    """The Why-flagged explainer (served bundle + feature file), or None where not deployed."""
    from src.dashboard import explain

    model_dir = os.environ.get("EXPLAIN_MODEL_DIR", str(explain.MODEL_DIR))
    features = os.environ.get("EXPLAIN_FEATURES_FILE", str(explain.FEATURES_FILE))
    return _explainer(model_dir, features)


@st.cache_data(ttl=READ_TTL_S, show_spinner=False)
def cached(_repo: Any, cache_key: str, method: str, *args: Any) -> Any:
    """``_repo.<method>(*args)``, cached for READ_TTL_S per database and arguments."""
    return getattr(_repo, method)(*args)


def load(method: str, *args: Any) -> Any:
    r = repo()
    return cached(r, r.cache_key, method, *args)


def invalidate() -> None:
    cached.clear()


def require_schema(*optional: str) -> frozenset[str]:
    """Stop the page with a clear message when a table it reads is missing or incomplete.

    The core tables (``data.CORE_COLUMNS``) are always required. ``optional`` names the batch-job
    tables this page also reads (``data.OPTIONAL_COLUMNS``): one that does not exist at all is
    returned, so the page can say its job has not run; one that exists with missing columns stops
    the page like a missing core table.
    """
    unknown = set(optional) - set(data.OPTIONAL_COLUMNS)
    if unknown:
        raise ValueError(f"not optional dashboard tables: {sorted(unknown)}")
    state: data.SchemaState = load("schema_state")
    needed = set(data.CORE_COLUMNS) | set(optional)
    missing = [m for m in state.missing if m.split(".", 1)[0] in needed]
    if missing:
        st.error(
            "The database schema is not initialised for the dashboard. Start the API or a "
            "consumer once (src/serving/store.py creates it); the dashboard never runs DDL.\n\n"
            "Missing: " + ", ".join(missing)
        )
        st.stop()
    return state.absent & frozenset(optional)


def configure_logging() -> None:
    """Send the dashboard's own log lines (one per save) to stderr, i.e. the container log."""
    log = logging.getLogger("src.dashboard")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)


def analyst_name() -> str:
    """The signed-in user (``sign_in_gate``); recorded as ``analyst`` on every review."""
    return str(st.session_state.get("analyst", "")).strip()


def _sign_in(users: dict[str, str]) -> None:
    from src.dashboard import auth

    name = str(st.session_state.get("sign_in_user", "")).strip()
    password = str(st.session_state.get("sign_in_password", ""))
    if auth.verify(users, name, password):
        st.session_state["analyst"] = name
        st.session_state.pop("sign_in_error", None)
        logging.getLogger(__name__).info("dashboard sign-in user=%s", name)
    else:
        st.session_state["sign_in_error"] = "Unknown user or wrong password."
        logging.getLogger(__name__).warning("dashboard sign-in failed user=%s", name)
    st.session_state["sign_in_password"] = ""  # never keep the password in the session


def _sign_out() -> None:
    st.session_state.pop("analyst", None)


def sign_in_gate() -> bool:
    """Every page needs a signed-in analyst (bcrypt hashes in DASHBOARD_USERS_FILE). Returns
    False after rendering the sign-in form; the caller stops the run."""
    from src.dashboard import auth

    if analyst_name():
        with st.sidebar:
            st.write(f"Signed in as **{analyst_name()}**")
            st.button("Sign out", key="sign_out", on_click=_sign_out)
        return True
    st.title("Fraud analyst dashboard")
    try:
        users = auth.load_users()
    except auth.UsersFileError as exc:
        st.error(f"The dashboard users file is invalid: {exc}")
        return False
    if not users:
        st.error(
            f"No dashboard users are configured ({auth.users_file()}). Create one on the host: "
            "python -m src.dashboard.auth add <name>."
        )
        return False
    with st.form("sign_in"):
        st.text_input("Username", key="sign_in_user")
        st.text_input("Password", type="password", key="sign_in_password")
        st.form_submit_button("Sign in", on_click=_sign_in, args=(users,))
    if "sign_in_error" in st.session_state:
        st.error(st.session_state["sign_in_error"])
    return False


def open_case(alert_id: int) -> None:
    st.session_state["alert_id"] = int(alert_id)
    st.switch_page("views/case.py")
