"""Aggregate EDA metrics for IEEE-CIS. Emits statistics only, never raw rows.

Usage: ``python -m src.data.eda`` writes ``reports/eda_metrics.json``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.data.load import (
    DEFAULT_RAW_DIR,
    HAS_IDENTITY,
    KEY,
    LABEL,
    IEEECISPaths,
    load_identity,
    load_transactions,
    merge_transaction_identity,
)

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86_400
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

# Source columns for each planned feature (Milestone 2).
PLANNED_FEATURES: dict[str, list[str]] = {
    "transaction_velocity": ["TransactionDT", "card1"],
    "amount_vs_user_average": ["TransactionAmt", "card1", "addr1"],
    "device_change": ["DeviceInfo", "DeviceType"],
    "location_change": ["addr1", "addr2", "dist1"],
    "email_domain_risk": ["P_emaildomain", "R_emaildomain"],
    "graph_entities": ["card1", "addr1", "P_emaildomain", "DeviceInfo"],
}
KEY_COLUMNS = ["card2", "addr1", "dist1", "P_emaildomain", "R_emaildomain", "DeviceInfo", "D1"]


def _r(x: float, nd: int = 5) -> float:
    return round(float(x), nd)


def column_family(name: str) -> str:
    """Group anonymised columns: ``V123`` -> ``V``, ``id_05`` -> ``id``, ``card1`` -> ``card``."""
    return re.sub(r"_?\d+$", "", name)


def merge_integrity(
    transactions: pd.DataFrame, identity: pd.DataFrame, merged: pd.DataFrame
) -> dict[str, Any]:
    matched = int(merged[HAS_IDENTITY].sum())
    return {
        "transaction_rows": len(transactions),
        "identity_rows": len(identity),
        "merged_rows": len(merged),
        "row_count_preserved": len(merged) == len(transactions),
        "transaction_ids_unique": bool(merged[KEY].is_unique),
        "identity_rows_matched": matched,
        "identity_rows_orphaned": len(identity) - matched,
        "transaction_dt_sorted": bool(merged["TransactionDT"].is_monotonic_increasing),
    }


def compute_eda_metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Compute aggregate statistics on a merged, labelled frame."""
    y = df[LABEL]
    fraud, legit = int(y.sum()), int((y == 0).sum())
    m: dict[str, Any] = {
        "overview": {
            "rows": len(df),
            "columns": df.shape[1],
            "memory_mb_optimized_dtypes": round(df.memory_usage(deep=True).sum() / 1e6, 1),
        },
        "class_balance": {
            "fraud_count": fraud,
            "legit_count": legit,
            "fraud_rate": _r(y.mean()),
            "neg_pos_ratio": round(legit / max(fraud, 1), 2),
        },
    }

    has_id = df[HAS_IDENTITY]
    m["identity_coverage"] = {
        "has_identity_rate": _r(has_id.mean(), 4),
        "fraud_rate_with_identity": _r(y[has_id].mean()) if has_id.any() else None,
        "fraud_rate_without_identity": _r(y[~has_id].mean()) if (~has_id).any() else None,
    }

    dt = df["TransactionDT"]
    day = (dt // SECONDS_PER_DAY).astype(int)
    daily = df.groupby(day)[LABEL].agg(["size", "mean"])
    month = (day - day.min()) // 30
    m["transaction_dt"] = {
        "min_seconds": int(dt.min()),
        "max_seconds": int(dt.max()),
        "span_days": _r((dt.max() - dt.min()) / SECONDS_PER_DAY, 1),
        "distinct_days": int(day.nunique()),
        "daily_transactions": {
            "min": int(daily["size"].min()),
            "median": _r(daily["size"].median(), 1),
            "max": int(daily["size"].max()),
        },
        "daily_fraud_rate": {
            "min": _r(daily["mean"].min()),
            "median": _r(daily["mean"].median()),
            "max": _r(daily["mean"].max()),
        },
        "fraud_rate_by_30_day_block": {
            int(cast(int, k)): {"rows": int(v["size"]), "fraud_rate": _r(v["mean"])}
            for k, v in df.groupby(month)[LABEL].agg(["size", "mean"]).iterrows()
        },
    }

    missing = df.isna().mean()
    by_family = missing.groupby(column_family).agg(["size", "mean"])
    m["missingness"] = {
        "columns_with_no_missing": int((missing == 0).sum()),
        "columns_over_50pct_missing": int((missing > 0.5).sum()),
        "columns_over_90pct_missing": int((missing > 0.9).sum()),
        "top_10_missing": {c: _r(v, 4) for c, v in missing.nlargest(10).items()},
        "mean_missing_by_family": {
            fam: {"columns": int(r["size"]), "mean_missing": _r(r["mean"], 4)}
            for fam, r in by_family.sort_values("mean").iterrows()
        },
        "key_column_missing_by_class": {
            c: {
                "legit": _r(df.loc[y == 0, c].isna().mean(), 4),
                "fraud": _r(df.loc[y == 1, c].isna().mean(), 4),
            }
            for c in KEY_COLUMNS
            if c in df.columns
        },
    }

    m["feature_availability"] = {
        name: {
            "all_present_rate": _r(df[cols].notna().all(axis=1).mean(), 4),
            "any_present_rate": _r(df[cols].notna().any(axis=1).mean(), 4),
        }
        for name, cols in PLANNED_FEATURES.items()
    }

    m["transaction_amount_by_class"] = {
        ("fraud" if k == 1 else "legit"): {
            f"p{int(cast(float, q) * 100)}": _r(v, 2)
            for q, v in amounts.quantile([0.5, 0.95, 0.99]).items()
        }
        for k, amounts in df.groupby(LABEL)["TransactionAmt"]
    }
    return m


def main(raw_dir: Path = DEFAULT_RAW_DIR, out: Path = REPORTS_DIR / "eda_metrics.json") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    paths = IEEECISPaths.from_dir(raw_dir, "train")
    transactions = load_transactions(paths.transaction)
    identity = load_identity(paths.identity)
    merged = merge_transaction_identity(transactions, identity)

    metrics = {
        "dataset": "IEEE-CIS train",
        "merge_integrity": merge_integrity(transactions, identity, merged),
    }
    metrics.update(compute_eda_metrics(merged))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out)


if __name__ == "__main__":
    main()
