"""Milestone 2 audit evidence: train/validation entity overlap and an identifier-input ablation.

Usage: ``python -m src.models.leakage_audit`` writes ``reports/leakage_audit.json``
(aggregates only). Uses the same split, features and training protocol as ``compare.py``.

* Entity overlap quantifies how many validation rows belong to card/uid proxies already seen
  (and already labelled fraud) in training. IEEE-CIS propagates a chargeback label to later
  transactions of the same card, so overlap can make validation scores optimistic.
* The ablation retrains the baseline without the raw identifier-like inputs (card and address
  codes) to measure how much of the score depends on recognising specific entities.

Validation labels are used only for the reported aggregates, never for fitting.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.entity import CARD_ID, USER_ID, build_entity_keys
from src.features.history import HistoryConfig
from src.features.split import chronological_split
from src.models.compare import (
    REPORTS_DIR,
    ComparisonConfig,
    build_model_frame,
    evaluate,
    feature_columns,
    score,
    train_variant,
)

logger = logging.getLogger(__name__)

IDENTIFIER_INPUTS = ("card1", "card2", "card3", "card5", "addr1", "addr2")


def _r(x: float) -> float | None:
    return None if pd.isna(x) else round(float(x), 4)


def entity_overlap(
    train_key: pd.Series[Any],
    valid_key: pd.Series[Any],
    train_y: pd.Series[int],
    valid_y: pd.Series[int],
) -> dict[str, Any]:
    """Row- and entity-level overlap of ``valid_key`` with ``train_key`` (NaN keys excluded)."""
    seen = set(train_key.dropna())
    fraud_seen = set(train_key[train_y.to_numpy() == 1].dropna())
    valid_entities = set(valid_key.dropna())
    carried = valid_key.isin(fraud_seen).to_numpy()
    return {
        "valid_rows_entity_seen_in_train": _r(valid_key.isin(seen).mean()),
        "valid_entities": len(valid_entities),
        "valid_entities_seen_in_train": _r(
            len(valid_entities & seen) / len(valid_entities) if valid_entities else float("nan")
        ),
        "valid_rows_entity_had_train_fraud": _r(carried.mean()),
        "valid_fraud_rate_if_entity_had_train_fraud": _r(valid_y[carried].mean()),
        "valid_fraud_rate_otherwise": _r(valid_y[~carried].mean()),
    }


def run_audit(df: pd.DataFrame, cfg: ComparisonConfig | None = None) -> dict[str, Any]:
    cfg = cfg or ComparisonConfig()
    hist = HistoryConfig()
    frame = build_model_frame(df, "baseline", hist)
    train, valid, boundary = chronological_split(frame, valid_fraction=cfg.valid_fraction)
    keys = build_entity_keys(df)

    overlap = {
        k: entity_overlap(
            keys.loc[train.index, k], keys.loc[valid.index, k], train[LABEL], valid[LABEL]
        )
        for k in (CARD_ID, USER_ID)
    }
    all_cols = feature_columns(hist)
    ablation = {}
    for name, cols in (
        ("all_inputs", all_cols),
        ("without_identifier_inputs", [c for c in all_cols if c not in IDENTIFIER_INPUTS]),
    ):
        started = time.perf_counter()
        trained = train_variant(train, cols, cfg)
        m = evaluate(
            valid[LABEL].to_numpy(),
            score(trained, valid.drop(columns=LABEL), cols),
            trained.threshold,
        )
        ablation[name] = {
            "n_inputs": len(cols),
            "valid_average_precision": _r(m["average_precision"]),
            "holdout_average_precision": _r(trained.holdout_average_precision),
            "precision": _r(m["at_threshold"]["precision"]),
            "recall": _r(m["at_threshold"]["recall"]),
            "flagged_rate": _r(m["at_threshold"]["flagged_rate"]),
            "seconds": round(time.perf_counter() - started, 1),
        }
    return {
        "split": {
            "train_end": boundary.train_end,
            "valid_start": boundary.valid_start,
            "train_rows": len(train),
            "valid_rows": len(valid),
        },
        "seed": cfg.seed,
        "identifier_inputs_removed": list(IDENTIFIER_INPUTS),
        "entity_overlap": overlap,
        "identifier_ablation": ablation,
    }


def main(
    raw_dir: Path = DEFAULT_RAW_DIR, out: Path = REPORTS_DIR / "leakage_audit.json"
) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    report = {"dataset": "IEEE-CIS train", **run_audit(load_ieee_cis(raw_dir, usecols=[]))}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out)
    from src.models.tracking import flatten_metrics, log_report_run  # mlflow: CLI only

    log_report_run(
        "leakage-audit",
        [out],
        metrics=flatten_metrics(report),
        tags={"module": "src.models.leakage_audit"},
    )
    return report


if __name__ == "__main__":
    main()
