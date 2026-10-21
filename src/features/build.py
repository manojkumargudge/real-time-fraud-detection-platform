"""Milestone 2 feature pipeline on IEEE-CIS: history features -> time split -> train-only fit.

Usage: ``python -m src.features.build`` writes aggregate statistics (never raw rows) to
``reports/feature_metrics.json``.

History features are computed once over the full chronological file. That is leak-free because
each row only uses strictly earlier transactions and no labels: training rows never see
validation rows, and validation rows see the history a live system would have had.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.entity import USER_ID
from src.features.history import TIME, HistoryConfig, compute_history_features
from src.features.preprocess import UNSEEN, CategoricalEncoder
from src.features.split import SplitBoundary, chronological_split

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def _r(x: float, nd: int = 4) -> float | None:
    return None if pd.isna(x) else round(float(x), nd)


def feature_summary(frame: pd.DataFrame, names: list[str]) -> dict[str, dict[str, Any]]:
    summary = {}
    for n in names:
        s = frame[n].dropna()
        summary[n] = {
            "null_rate": _r(frame[n].isna().mean()),
            "mean": _r(s.mean()) if len(s) else None,
            "p50": _r(s.median()) if len(s) else None,
        }
    return summary


def split_metrics(
    train: pd.DataFrame,
    valid: pd.DataFrame,
    boundary: SplitBoundary,
    encoder: CategoricalEncoder,
    names: list[str],
) -> dict[str, Any]:
    train_uids = set(train[USER_ID].dropna())
    valid_uid = valid[USER_ID]
    enc_valid = encoder.transform(valid[list(encoder.columns)])
    return {
        "boundary": {
            "train_end": boundary.train_end,
            "valid_start": boundary.valid_start,
            "gap_seconds": boundary.gap_seconds,
        },
        "rows": {"train": len(train), "valid": len(valid)},
        "fraud_rate": {"train": _r(train[LABEL].mean(), 5), "valid": _r(valid[LABEL].mean(), 5)},
        "max_train_dt_lt_min_valid_dt": bool(train[TIME].max() < valid[TIME].min()),
        "valid_rows_with_uid_unseen_in_train": _r((~valid_uid.isin(train_uids)).mean()),
        "valid_rows_with_no_prior_uid_history": _r((valid["uid_prior_txn_count"] == 0).mean()),
        "encoder_vocab_sizes": {c: len(v) for c, v in (encoder.vocab_ or {}).items()},
        "valid_unseen_category_rate": {
            c: _r((enc_valid[c] == UNSEEN).mean()) for c in encoder.columns
        },
        "features_train": feature_summary(train, names),
        "features_valid": feature_summary(valid, names),
    }


def main(
    raw_dir: Path = DEFAULT_RAW_DIR,
    out: Path = REPORTS_DIR / "feature_metrics.json",
    *,
    valid_fraction: float = 0.2,
    gap_seconds: int = 0,
) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    started = time.perf_counter()
    config = HistoryConfig()
    # Only the columns the loader requires (feature inputs) are read; V/C/M/id_* are skipped.
    df = load_ieee_cis(raw_dir, usecols=[])
    feats = compute_history_features(df, config)
    feature_seconds = time.perf_counter() - started

    encoder = CategoricalEncoder()
    frame = pd.concat([df[[KEY, TIME, LABEL, *encoder.columns]], feats], axis=1)
    del df, feats
    train, valid, boundary = chronological_split(
        frame, valid_fraction=valid_fraction, gap_seconds=gap_seconds
    )
    encoder.fit(train)

    names = config.feature_names()
    metrics: dict[str, Any] = {
        "dataset": "IEEE-CIS train",
        "rows": len(frame),
        "distinct_uid": int(frame[USER_ID].nunique()),
        "distinct_card_id": int(frame["card_id"].nunique()),
        "rows_missing_uid": int(frame[USER_ID].isna().sum()),
        "split": split_metrics(train, valid, boundary, encoder, names),
        "runtime_seconds": {
            "load_and_features": round(feature_seconds, 1),
            "total": round(time.perf_counter() - started, 1),
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out)
    return metrics


if __name__ == "__main__":
    main()
