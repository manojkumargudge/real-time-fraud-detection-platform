"""Persistence for scored transactions and alerts.

``TransactionID`` is the idempotency key: saving an already-stored ID is a no-op that
returns ``False``. The stored events also serve as the online feature history.

* ``InMemoryStore``  -- tests and a dependency-free local API run (not durable).
* ``PostgresStore``  -- one connection guarded by a lock; adequate for a local demo, not a
  tuned production pool.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg.rows import DictRow, dict_row

from src.features.history import TIME

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredRecord:
    transaction_id: int
    transaction_dt: int
    card_id: str | None
    uid: str | None
    event: dict[str, Any]
    score: float
    threshold: float
    is_alert: bool
    model_version: str


class TransactionStore(Protocol):
    name: str

    def get(self, transaction_id: int) -> ScoredRecord | None: ...

    def card_history(self, card_id: str, before_dt: int) -> list[dict[str, Any]]: ...

    def save(self, record: ScoredRecord) -> bool: ...

    def ping(self) -> bool: ...

    def close(self) -> None: ...


class InMemoryStore:
    name = "memory"

    def __init__(self) -> None:
        self._records: dict[int, ScoredRecord] = {}
        self._by_card: dict[str, list[int]] = {}
        self._lock = threading.Lock()
        self.alerts: dict[int, float] = {}

    def get(self, transaction_id: int) -> ScoredRecord | None:
        return self._records.get(transaction_id)

    def card_history(self, card_id: str, before_dt: int) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._by_card.get(card_id, ()))
        events = [self._records[i].event for i in ids]
        return [e for e in events if e[TIME] < before_dt]

    def save(self, record: ScoredRecord) -> bool:
        with self._lock:
            if record.transaction_id in self._records:
                return False
            self._records[record.transaction_id] = record
            if record.card_id is not None:
                self._by_card.setdefault(record.card_id, []).append(record.transaction_id)
            if record.is_alert:
                self.alerts[record.transaction_id] = record.score
            return True

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def __len__(self) -> int:
        return len(self._records)


# Alert workflow states (transitions: src/dashboard/workflow.py). 'open' is the start state.
ALERT_STATUSES = ("open", "investigating", "confirmed_fraud", "false_positive", "closed")
# Fixed key for pg_advisory_xact_lock: the API and every consumer run the setup at startup,
# concurrently; the lock serialises them (concurrent CREATE TABLE IF NOT EXISTS can collide).
SCHEMA_LOCK_KEY = 7291406001


def _status_check_def(statuses: tuple[str, ...]) -> str:
    """The CHECK as pg_get_constraintdef() renders it, to detect an outdated constraint."""
    items = ", ".join(f"'{s}'::text" for s in statuses)
    return f"CHECK ((status = ANY (ARRAY[{items}])))"


# A plain literal (no SQL assembled at runtime); the checks below keep it in step with
# ALERT_STATUSES and SCHEMA_LOCK_KEY.
DDL = """
SELECT pg_advisory_xact_lock(7291406001);
CREATE TABLE IF NOT EXISTS scored_transactions (
    transaction_id BIGINT PRIMARY KEY,
    transaction_dt BIGINT NOT NULL CHECK (transaction_dt >= 0),
    card_id TEXT,
    uid TEXT,
    event JSONB NOT NULL,
    score DOUBLE PRECISION NOT NULL CHECK (score >= 0 AND score <= 1),
    threshold DOUBLE PRECISION NOT NULL,
    is_alert BOOLEAN NOT NULL,
    model_version TEXT NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_scored_card_dt ON scored_transactions (card_id, transaction_dt);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL UNIQUE REFERENCES scored_transactions (transaction_id),
    score DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'investigating', 'confirmed_fraud', 'false_positive', 'closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Analyst-owned fields (the dashboard). The scoring path inserts (transaction_id, score) only,
-- with ON CONFLICT DO NOTHING, so it never changes them. Existing rows get the defaults.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
-- Widen an older status CHECK; only when it differs, so a restart takes no ACCESS EXCLUSIVE lock.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'alerts'::regclass AND conname = 'alerts_status_check'
          -- adjacent literals on separate lines are concatenated by PostgreSQL
          AND pg_get_constraintdef(oid) = 'CHECK ((status = ANY (ARRAY[''open''::text, '
              '''investigating''::text, ''confirmed_fraud''::text, ''false_positive''::text, '
              '''closed''::text])))'
    ) THEN
        ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_status_check;
        ALTER TABLE alerts ADD CONSTRAINT alerts_status_check
        CHECK (status IN ('open', 'investigating', 'confirmed_fraud', 'false_positive', 'closed'));
    END IF;
END $$;
-- Analyst decisions, append-only (feeds retraining: latest labelled review per transaction).
CREATE TABLE IF NOT EXISTS case_reviews (
    id BIGSERIAL PRIMARY KEY,
    alert_id BIGINT NOT NULL REFERENCES alerts (alert_id),
    transaction_id BIGINT NOT NULL REFERENCES scored_transactions (transaction_id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    label TEXT CHECK (label IN ('fraud', 'not_fraud')),
    analyst TEXT NOT NULL CHECK (btrim(analyst) <> ''),
    notes TEXT,
    alert_version INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_case_reviews_alert ON case_reviews (alert_id, id);
CREATE INDEX IF NOT EXISTS ix_case_reviews_tx ON case_reviews (transaction_id, id);
-- One-time token of the dashboard form a review was saved from: the same submit can never write
-- twice (docs/incidents/2026-10-26-unintended-reviews.md). Additive; older rows keep NULL, and a
-- unique index allows any number of NULLs.
ALTER TABLE case_reviews ADD COLUMN IF NOT EXISTS submit_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS ux_case_reviews_submit_token ON case_reviews (submit_token);
-- end of submit token
-- Row-level UPDATE/DELETE are rejected. TRUNCATE (whole table) does not fire row triggers; the
-- benchmark clone uses it (scripts/bench_throughput.py).
CREATE OR REPLACE FUNCTION case_reviews_append_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'case_reviews is append-only: % rejected', TG_OP;
END
$fn$;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'case_reviews'::regclass AND tgname = 'case_reviews_append_only'
    ) THEN
        CREATE TRIGGER case_reviews_append_only BEFORE UPDATE OR DELETE ON case_reviews
            FOR EACH ROW EXECUTE FUNCTION case_reviews_append_only();
    END IF;
END $$;
"""

_JOINED = re.sub(r"'\s*\n\s*'", "", DDL)  # as PostgreSQL reads the split literal
_STATUS_LIST = "CHECK (status IN ({}))".format(", ".join(f"'{s}'" for s in ALERT_STATUSES))
if (
    f"pg_advisory_xact_lock({SCHEMA_LOCK_KEY})" not in DDL
    or _status_check_def(ALERT_STATUSES).replace("'", "''") not in _JOINED
    or DDL.count(_STATUS_LIST) != 2
):
    raise RuntimeError("store.DDL is out of step with ALERT_STATUSES / SCHEMA_LOCK_KEY")


class PostgresStore:
    name = "postgres"

    def __init__(self, dsn: str, *, init_schema: bool = True) -> None:
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn: psycopg.Connection[DictRow] = self._connect()
        if init_schema:
            with self._lock, self._conn.transaction():
                self._conn.execute(DDL)

    def _connect(self) -> psycopg.Connection[DictRow]:
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)

    def _cursor_conn(self) -> psycopg.Connection[DictRow]:
        if self._conn.closed:
            logger.warning("postgres connection closed; reconnecting")
            self._conn = self._connect()
        return self._conn

    def get(self, transaction_id: int) -> ScoredRecord | None:
        with self._lock:
            row = (
                self._cursor_conn()
                .execute(
                    "SELECT transaction_id, transaction_dt, card_id, uid, event, score, "
                    "threshold, is_alert, model_version FROM scored_transactions "
                    "WHERE transaction_id = %s",
                    (transaction_id,),
                )
                .fetchone()
            )
        return None if row is None else ScoredRecord(**row)

    def card_history(self, card_id: str, before_dt: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = (
                self._cursor_conn()
                .execute(
                    "SELECT event FROM scored_transactions "
                    "WHERE card_id = %s AND transaction_dt < %s ORDER BY transaction_dt, "
                    "transaction_id",
                    (card_id, before_dt),
                )
                .fetchall()
            )
        return [r["event"] for r in rows]

    def save(self, record: ScoredRecord) -> bool:
        with self._lock:
            conn = self._cursor_conn()
            with conn.transaction():
                inserted = conn.execute(
                    "INSERT INTO scored_transactions (transaction_id, transaction_dt, card_id, "
                    "uid, event, score, threshold, is_alert, model_version) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id",
                    (
                        record.transaction_id,
                        record.transaction_dt,
                        record.card_id,
                        record.uid,
                        json.dumps(record.event),
                        record.score,
                        record.threshold,
                        record.is_alert,
                        record.model_version,
                    ),
                ).fetchone()
                if inserted and record.is_alert:
                    conn.execute(
                        "INSERT INTO alerts (transaction_id, score) VALUES (%s, %s) "
                        "ON CONFLICT (transaction_id) DO NOTHING",
                        (record.transaction_id, record.score),
                    )
        return inserted is not None

    def ping(self) -> bool:
        try:
            with self._lock:
                self._cursor_conn().execute("SELECT 1").fetchone()
            return True
        except psycopg.Error:
            logger.exception("postgres ping failed")
            return False

    def counts(self) -> dict[str, int]:
        with self._lock:
            conn = self._cursor_conn()
            txn = conn.execute("SELECT count(*) AS n FROM scored_transactions").fetchone()
            alerts = conn.execute("SELECT count(*) AS n FROM alerts").fetchone()
        if txn is None or alerts is None:  # count(*) always returns exactly one row
            raise RuntimeError("count query returned no row")
        return {"scored_transactions": int(txn["n"]), "alerts": int(alerts["n"])}

    def close(self) -> None:
        self._conn.close()
