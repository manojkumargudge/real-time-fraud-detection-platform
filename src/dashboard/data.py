"""Data access for the analyst dashboard: every SQL statement of the dashboard lives here.

All queries are parameterised; filters arrive as typed values, never as SQL text. Every function
takes a connection borrowed by the caller (``src/dashboard/db.py``), so no connection is shared
between Streamlit sessions. The dashboard never runs DDL: the schema belongs to
``src/serving/store.py``; ``missing_schema`` reports what is missing.

Status changes use optimistic locking on ``alerts.version``: the analyst's form carries the
version it was rendered from, and a change is applied only if the row still has that version.
Each form also carries a one-time submit token, stored with the review under a UNIQUE index, so
the same submit can never write twice. The alert update and its ``case_reviews`` row are written
in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.dashboard import fmt
from src.dashboard.workflow import Label, validate_transition

SECONDS_PER_DAY = 86_400
MAX_PAGE_SIZE = 200
MAX_CUSTOMER_ALERTS = 20  # "Other alerts for this customer" on Case detail
REQUIRED_COLUMNS = {
    "alerts": set("alert_id transaction_id score status created_at updated_at version".split()),
    "case_reviews": set(
        "id alert_id transaction_id from_status to_status label analyst notes alert_version "
        "created_at submit_token".split()
    ),
    "scored_transactions": {"transaction_id", "transaction_dt", "uid", "event", "model_version"},
    "fraud_rings": {"rank", "uids", "fraud_rate"},
    "drift_reports": {"id", "report_path", "triggered"},
}


class SchemaNotReadyError(RuntimeError):
    """Tables or columns the dashboard needs are missing (run the API or a consumer once)."""


class AlertNotFoundError(LookupError):
    pass


class StaleAlertError(RuntimeError):
    """The alert changed since the analyst loaded it (optimistic-lock conflict)."""

    def __init__(
        self,
        alert_id: int,
        expected_version: int,
        current_status: str,
        current_version: int,
        changed_by: str | None,
        changed_at: datetime | None,
    ) -> None:
        self.alert_id, self.expected_version = alert_id, expected_version
        self.current_status, self.current_version = current_status, current_version
        self.changed_by, self.changed_at = changed_by, changed_at
        who = f" by {changed_by}" if changed_by else ""
        when = f" at {fmt.ist(changed_at, seconds=True)}" if changed_at else ""
        super().__init__(
            f"This case changed since you loaded it: alert {alert_id} was changed{who}{when}; it "
            f"is now {current_status} (version {current_version}, you had version "
            f"{expected_version}). Reload the case to see the change; nothing was saved."
        )


class DuplicateSubmitError(RuntimeError):
    """This form submit (its one-time token) was already saved; nothing is written again."""

    def __init__(self, alert_id: int, submit_token: str) -> None:
        self.alert_id, self.submit_token = alert_id, submit_token
        super().__init__(
            f"This form was already saved (alert {alert_id}); nothing was saved again. "
            "Reload the case to see its current state."
        )


SUBMIT_TOKEN_INDEX = "ux_case_reviews_submit_token"


@dataclass(frozen=True)
class AlertFilter:
    statuses: tuple[str, ...] = ()  # empty = all
    min_score: float = 0.0
    max_score: float = 1.0
    day_from: int | None = None  # transaction day (TransactionDT // 86400), inclusive
    day_to: int | None = None


@dataclass(frozen=True)
class Page:
    rows: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


def missing_schema(conn: psycopg.Connection[Any]) -> list[str]:
    """``table.column`` names the dashboard needs but the database lacks (empty = ready)."""
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (list(REQUIRED_COLUMNS),),
    ).fetchall()
    present: dict[str, set[str]] = {}
    for table, column in rows:
        present.setdefault(table, set()).add(column)
    return sorted(
        f"{t}.{c}" for t, cols in REQUIRED_COLUMNS.items() for c in cols - present.get(t, set())
    )


def require_schema(conn: psycopg.Connection[Any]) -> None:
    missing = missing_schema(conn)
    if missing:
        raise SchemaNotReadyError(
            "database schema not initialised for the dashboard (missing: "
            + ", ".join(missing[:8])
            + (" ..." if len(missing) > 8 else "")
            + "). Start the API or a consumer once: src/serving/store.py creates it."
        )


# --------------------------------------------------------------------------- overview


def overview(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Alert counts by status, alerts per transaction day, review precision, model version."""
    by_status = dict(conn.execute("SELECT status, count(*) FROM alerts GROUP BY status").fetchall())
    per_day = conn.execute(
        "SELECT s.transaction_dt / %s AS day, count(*) FROM alerts a "
        "JOIN scored_transactions s USING (transaction_id) GROUP BY 1 ORDER BY 1",
        (SECONDS_PER_DAY,),
    ).fetchall()
    labels = latest_labels(conn)  # the same definition retraining uses
    reviewed = len(labels)
    fraud = sum(1 for row in labels if row.label == "fraud")
    newest = conn.execute(
        "SELECT model_version, scored_at FROM scored_transactions "
        "ORDER BY scored_at DESC, transaction_id DESC LIMIT 1"
    ).fetchone()
    return {
        "by_status": {str(k): int(v) for k, v in by_status.items()},
        "total": int(sum(by_status.values())),
        "per_day": [(int(d), int(n)) for d, n in per_day],
        "reviewed": int(reviewed),
        "confirmed_fraud": int(fraud),
        "precision": (fraud / reviewed) if reviewed else None,
        "model_version": newest[0] if newest else None,
        "model_last_scored_at": newest[1] if newest else None,
    }


# --------------------------------------------------------------------------- alert queue


# One static statement: optional filters are NULL parameters, never SQL assembled at runtime.
_ALERT_WHERE = (
    "WHERE a.score >= %(min_score)s AND a.score <= %(max_score)s "
    "AND (%(statuses)s::text[] IS NULL OR a.status = ANY(%(statuses)s::text[])) "
    "AND (%(dt_from)s::bigint IS NULL OR s.transaction_dt >= %(dt_from)s::bigint) "
    "AND (%(dt_to)s::bigint IS NULL OR s.transaction_dt < %(dt_to)s::bigint)"
)
_COUNT_ALERTS = (
    "SELECT count(*) FROM alerts a JOIN scored_transactions s USING (transaction_id) "
    "WHERE a.score >= %(min_score)s AND a.score <= %(max_score)s "
    "AND (%(statuses)s::text[] IS NULL OR a.status = ANY(%(statuses)s::text[])) "
    "AND (%(dt_from)s::bigint IS NULL OR s.transaction_dt >= %(dt_from)s::bigint) "
    "AND (%(dt_to)s::bigint IS NULL OR s.transaction_dt < %(dt_to)s::bigint)"
)
_PAGE_ALERTS = (
    "SELECT a.alert_id, a.transaction_id, a.score, a.status, a.version, a.updated_at, "
    "s.transaction_dt, s.transaction_dt / %(day)s AS transaction_day, "
    "(s.event ->> 'TransactionAmt')::float8 AS amount, s.event ->> 'ProductCD' AS product, "
    "s.card_id, s.model_version FROM alerts a JOIN scored_transactions s USING (transaction_id) "
    "WHERE a.score >= %(min_score)s AND a.score <= %(max_score)s "
    "AND (%(statuses)s::text[] IS NULL OR a.status = ANY(%(statuses)s::text[])) "
    "AND (%(dt_from)s::bigint IS NULL OR s.transaction_dt >= %(dt_from)s::bigint) "
    "AND (%(dt_to)s::bigint IS NULL OR s.transaction_dt < %(dt_to)s::bigint) "
    "ORDER BY a.score DESC, a.alert_id LIMIT %(limit)s OFFSET %(offset)s"
)
if _ALERT_WHERE not in _COUNT_ALERTS or _ALERT_WHERE not in _PAGE_ALERTS:  # keep them in step
    raise RuntimeError("alert queue statements use different filters")


def list_alerts(
    conn: psycopg.Connection[Any], filt: AlertFilter, page: int = 1, page_size: int = 50
) -> Page:
    """One page of alerts matching ``filt``, highest score first (ties: alert_id)."""
    page, page_size = max(1, page), min(max(1, page_size), MAX_PAGE_SIZE)
    params: dict[str, Any] = {
        "min_score": float(filt.min_score),
        "max_score": float(filt.max_score),
        "statuses": list(filt.statuses) or None,
        "dt_from": None if filt.day_from is None else int(filt.day_from) * SECONDS_PER_DAY,
        "dt_to": None if filt.day_to is None else (int(filt.day_to) + 1) * SECONDS_PER_DAY,
    }
    total_row = conn.execute(_COUNT_ALERTS, params).fetchone()
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            _PAGE_ALERTS,
            params | {"day": SECONDS_PER_DAY, "limit": page_size, "offset": (page - 1) * page_size},
        ).fetchall()
    total = int(total_row[0]) if total_row else 0
    return Page(rows=list(rows), total=total, page=page, page_size=page_size)


# --------------------------------------------------------------------------- case detail


def get_case(conn: psycopg.Connection[Any], alert_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT a.alert_id, a.transaction_id, a.score, a.status, a.version, a.created_at, "
            "a.updated_at, s.transaction_dt, s.transaction_dt / %s AS transaction_day, "
            "s.card_id, s.uid, s.event, s.threshold, s.model_version, s.scored_at "
            "FROM alerts a JOIN scored_transactions s USING (transaction_id) "
            "WHERE a.alert_id = %s",
            (SECONDS_PER_DAY, alert_id),
        ).fetchone()
    return dict(row) if row else None


def customer_alerts(
    conn: psycopg.Connection[Any], uid: str | None, exclude_alert_id: int
) -> tuple[list[dict[str, Any]], int]:
    """Other alerts of the same customer (``uid`` = card fields + addr1, src/features/entity.py),
    highest score first, at most MAX_CUSTOMER_ALERTS rows, and how many there are in total.
    Grouped by uid, never by card_id: one card key is shared by many customers."""
    if uid is None:
        return [], 0
    params = {"uid": uid, "exclude": exclude_alert_id}
    total_row = conn.execute(
        "SELECT count(*) FROM alerts a JOIN scored_transactions s USING (transaction_id) "
        "WHERE s.uid = %(uid)s AND a.alert_id <> %(exclude)s",
        params,
    ).fetchone()
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT a.alert_id, a.score, a.status, s.transaction_dt / %(day)s AS transaction_day "
            "FROM alerts a JOIN scored_transactions s USING (transaction_id) "
            "WHERE s.uid = %(uid)s AND a.alert_id <> %(exclude)s "
            "ORDER BY a.score DESC, a.alert_id LIMIT %(limit)s",
            params | {"day": SECONDS_PER_DAY, "limit": MAX_CUSTOMER_ALERTS},
        ).fetchall()
    return list(rows), int(total_row[0]) if total_row else 0


def review_history(conn: psycopg.Connection[Any], alert_id: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT id, from_status, to_status, label, analyst, notes, alert_version, created_at "
            "FROM case_reviews WHERE alert_id = %s ORDER BY id",
            (alert_id,),
        ).fetchall()
    return list(rows)


def change_status(
    conn: psycopg.Connection[Any],
    alert_id: int,
    expected_version: int,
    to_status: str,
    analyst: str,
    notes: str | None = None,
    *,
    submit_token: str | None = None,
) -> int:
    """Move an alert to ``to_status`` and append its review; returns the new version.

    ``submit_token`` is the one-time token of the dashboard form (stored, UNIQUE); None only for
    scripts and tests. Raises ``DuplicateSubmitError`` (token already used), ``StaleAlertError``
    (the alert's version is no longer ``expected_version``), ``InvalidTransitionError``
    (workflow), ``AlertNotFoundError`` or ``ValueError`` (no status, no analyst name, blank
    token). Nothing is written unless both the update and the review succeed.
    """
    if not (to_status or "").strip():
        raise ValueError("Choose a new status; nothing was saved.")
    analyst = analyst.strip()
    if not analyst:
        raise ValueError("an analyst name is required to change a case")
    if submit_token is not None and not submit_token.strip():
        raise ValueError("a blank submit token is not allowed")
    notes = (notes or "").strip() or None
    try:
        return _change_status(
            conn, alert_id, expected_version, to_status, analyst, notes, submit_token
        )
    except psycopg.errors.UniqueViolation as exc:  # a concurrent save with the same token
        if submit_token is not None and exc.diag.constraint_name == SUBMIT_TOKEN_INDEX:
            raise DuplicateSubmitError(alert_id, submit_token) from exc
        raise


def _change_status(
    conn: psycopg.Connection[Any],
    alert_id: int,
    expected_version: int,
    to_status: str,
    analyst: str,
    notes: str | None,
    submit_token: str | None,
) -> int:
    with conn.transaction():
        row = conn.execute(
            "SELECT status, version, transaction_id FROM alerts WHERE alert_id = %s FOR UPDATE",
            (alert_id,),
        ).fetchone()
        if row is None:
            raise AlertNotFoundError(f"alert {alert_id} does not exist")
        status, version, transaction_id = row
        if (
            submit_token is not None
            and conn.execute(
                "SELECT 1 FROM case_reviews WHERE submit_token = %s", (submit_token,)
            ).fetchone()
        ):
            raise DuplicateSubmitError(alert_id, submit_token)
        if version != expected_version:
            last = conn.execute(
                "SELECT analyst, created_at FROM case_reviews WHERE alert_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (alert_id,),
            ).fetchone()
            raise StaleAlertError(
                alert_id, expected_version, status, version, *(last or (None, None))
            )
        label = validate_transition(status, to_status)
        updated = conn.execute(
            "UPDATE alerts SET status = %s, version = version + 1, updated_at = now() "
            "WHERE alert_id = %s AND version = %s RETURNING version",
            (to_status, alert_id, expected_version),
        ).fetchone()
        if updated is None:  # cannot happen under FOR UPDATE; never write a review without it
            raise RuntimeError(f"alert {alert_id} update matched no row")
        conn.execute(
            "INSERT INTO case_reviews (alert_id, transaction_id, from_status, to_status, label, "
            "analyst, notes, alert_version, submit_token) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                alert_id,
                transaction_id,
                status,
                to_status,
                label,
                analyst,
                notes,
                updated[0],
                submit_token,
            ),
        )
    return int(updated[0])


# --------------------------------------------------------------------------- labels


# Latest labelled review per transaction: a correction (a later review with a new label) wins.
_LATEST_LABELS = (
    "SELECT DISTINCT ON (transaction_id) transaction_id, label, created_at "
    "FROM case_reviews WHERE label IS NOT NULL ORDER BY transaction_id, id DESC"
)


@dataclass(frozen=True)
class LabelRow:
    transaction_id: int
    label: Label
    reviewed_at: datetime


def latest_labels(conn: psycopg.Connection[Any]) -> list[LabelRow]:
    """Analyst label per reviewed transaction: the latest labelled review (for retraining)."""
    rows = conn.execute(_LATEST_LABELS).fetchall()
    return [LabelRow(int(t), label, at) for t, label, at in rows]


# --------------------------------------------------------------------------- fraud rings


def list_rings(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT rank, ring_id, uid_count, card_count, tx_count, total_amount, "
            "shared_devices, mean_risk, scored_tx, fraud_rate, first_dt, last_dt "
            "FROM fraud_rings ORDER BY rank"
        ).fetchall()
    return list(rows)


def get_ring(conn: psycopg.Connection[Any], rank: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM fraud_rings WHERE rank = %s", (rank,)).fetchone()
    return dict(row) if row else None


def ring_for_uid(conn: psycopg.Connection[Any], uid: str | None) -> dict[str, Any] | None:
    """The ring (if any) that has ``uid`` as a member."""
    if uid is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT rank, ring_id, uid_count, card_count, tx_count, fraud_rate, shared_devices "
            "FROM fraud_rings WHERE uids ? %s ORDER BY rank LIMIT 1",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def ring_alerts(
    conn: psycopg.Connection[Any], uids: list[str], limit: int = 200
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT a.alert_id, a.transaction_id, a.score, a.status, s.uid, "
            "s.transaction_dt / %s AS transaction_day FROM alerts a "
            "JOIN scored_transactions s USING (transaction_id) WHERE s.uid = ANY(%s) "
            "ORDER BY a.score DESC, a.alert_id LIMIT %s",
            (SECONDS_PER_DAY, uids, min(limit, MAX_PAGE_SIZE)),
        ).fetchall()
    return list(rows)


def card_of_uid(uid: str) -> str:
    """The card of a uid: uid = card_id + "|" + addr1 (src/features/entity.py)."""
    return uid.rsplit("|", 1)[0]


# --------------------------------------------------------------------------- drift


def list_drift_reports(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT id, run_at, synthetic, forced, window_start_dt / %(d)s AS window_start_day, "
            "window_end_dt / %(d)s AS window_end_day, current_rows, drifted_feature_share, "
            "top_drifted_features, drifted_top_gain_features, prediction_drift_score, "
            "prediction_drift_detected, delayed_pr_auc, triggered, trigger_reasons, "
            "promotion_decision, challenger_version, report_path FROM drift_reports ORDER BY id",
            {"d": SECONDS_PER_DAY},
        ).fetchall()
    return list(rows)


def report_file(report_path: str | None, reports_dir: Path) -> Path | None:
    """The stored report, found by file name only in ``reports_dir`` (paths are not rewritten:
    rows written on the Windows host hold ``C:\\...`` paths)."""
    if not report_path:
        return None
    pure = PureWindowsPath(report_path) if "\\" in report_path else PurePosixPath(report_path)
    name = pure.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    candidate = reports_dir / name
    try:
        if candidate.resolve().parent != reports_dir.resolve() or not candidate.is_file():
            return None
    except OSError:
        return None
    return candidate
