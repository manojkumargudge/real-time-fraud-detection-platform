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


DDL = """
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
        CHECK (status IN ('open', 'confirmed_fraud', 'false_positive', 'closed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


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
