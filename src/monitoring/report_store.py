"""``drift_reports``: one row per drift-job run, plus reads of the scored transactions it needs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

DDL = """
CREATE TABLE IF NOT EXISTS drift_reports (
    id                        BIGSERIAL PRIMARY KEY,
    run_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    synthetic                 BOOLEAN NOT NULL,
    synthetic_note            TEXT,
    now_dt                    BIGINT NOT NULL,
    label_cutoff_dt           BIGINT NOT NULL,
    window_start_dt           BIGINT NOT NULL,
    window_end_dt             BIGINT NOT NULL,
    reference_start_dt        BIGINT NOT NULL,
    reference_end_dt          BIGINT NOT NULL,
    current_rows              INTEGER NOT NULL,
    reference_rows            INTEGER NOT NULL,
    feature_count             INTEGER NOT NULL,
    drifted_feature_count     INTEGER NOT NULL,
    drifted_feature_share     DOUBLE PRECISION NOT NULL,
    top_drifted_features      JSONB NOT NULL,
    prediction_drift_method   TEXT NOT NULL,
    prediction_drift_score    DOUBLE PRECISION NOT NULL,
    prediction_drift_detected BOOLEAN NOT NULL,
    performance_start_dt      BIGINT NOT NULL,
    performance_end_dt        BIGINT NOT NULL,
    delayed_rows              INTEGER NOT NULL,
    delayed_positives         INTEGER NOT NULL,
    delayed_pr_auc            DOUBLE PRECISION,
    champion_valid_pr_auc     DOUBLE PRECISION NOT NULL,
    triggered                 BOOLEAN NOT NULL,
    trigger_reasons           JSONB NOT NULL,
    retrain_run_id            TEXT,
    challenger_version        TEXT,
    promotion_decision        TEXT,
    promotion                 JSONB,
    windows                   JSONB NOT NULL,
    report_path               TEXT NOT NULL,
    config                    JSONB NOT NULL
);
-- Added 2026-10-24 (drift-rules). Rows written before have forced = false and NULL lists.
ALTER TABLE drift_reports ADD COLUMN IF NOT EXISTS forced BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE drift_reports ADD COLUMN IF NOT EXISTS top_gain_features JSONB;
ALTER TABLE drift_reports ADD COLUMN IF NOT EXISTS drifted_top_gain_features JSONB;
"""

_JSON_COLUMNS = {
    "top_drifted_features",
    "trigger_reasons",
    "promotion",
    "windows",
    "config",
    "top_gain_features",
    "drifted_top_gain_features",
}


@dataclass(frozen=True)
class DriftReportRow:
    synthetic: bool
    forced: bool
    top_gain_features: list[str]
    drifted_top_gain_features: list[str]
    synthetic_note: str | None
    now_dt: int
    label_cutoff_dt: int
    window_start_dt: int
    window_end_dt: int
    reference_start_dt: int
    reference_end_dt: int
    current_rows: int
    reference_rows: int
    feature_count: int
    drifted_feature_count: int
    drifted_feature_share: float
    top_drifted_features: list[dict[str, Any]]
    prediction_drift_method: str
    prediction_drift_score: float
    prediction_drift_detected: bool
    performance_start_dt: int
    performance_end_dt: int
    delayed_rows: int
    delayed_positives: int
    delayed_pr_auc: float | None
    champion_valid_pr_auc: float
    triggered: bool
    trigger_reasons: list[str]
    retrain_run_id: str | None
    challenger_version: str | None
    promotion_decision: str | None
    promotion: dict[str, Any] | None
    windows: dict[str, Any]
    report_path: str
    config: dict[str, Any]


def insert_report(dsn: str, row: DriftReportRow) -> int:
    values = {
        k: (None if v is None else json.dumps(v)) if k in _JSON_COLUMNS else v
        for k, v in row.__dict__.items()
    }
    query = sql.SQL("INSERT INTO drift_reports ({}) VALUES ({}) RETURNING id").format(
        sql.SQL(", ").join(map(sql.Identifier, values)),
        sql.SQL(", ").join(map(sql.Placeholder, values)),
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(DDL)
        out = conn.execute(query, values).fetchone()
    if out is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(out[0])


def load_scored(dsn: str) -> pd.DataFrame:
    """``transaction_id``, ``transaction_dt``, ``score``, ``model_version`` of scored events."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT transaction_id, transaction_dt, score, model_version FROM scored_transactions"
        ).fetchall()
    frame = pd.DataFrame(
        rows, columns=["transaction_id", "transaction_dt", "score", "model_version"]
    )
    return frame.astype({"transaction_id": "int64", "transaction_dt": "int64", "score": "float64"})
