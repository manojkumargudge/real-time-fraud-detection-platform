"""Compare entity proxies on IEEE-CIS. Emits aggregate statistics only, never raw rows.

Usage: ``python -m src.features.proxy_report`` writes ``reports/proxy_comparison.json``.

Proxies compared: ``card_id``, ``uid`` (default) and the opt-in ``card_d1``, ``uid_d1``
(+ estimated account-start day). A *null control* shuffles D1 within each base entity: it
fragments groups at least as much but destroys any time alignment, so a candidate beating the
null on non-key purity is evidence that D1 encodes real structure, not just extra splitting.

Labels are used only for the descriptive fraud-rate-by-flag table, on the training window,
after all keys and flags are built. They never enter a key or a flag.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.entity import (
    CARD_ID,
    CARD_START_ID,
    SECONDS_PER_DAY,
    USER_ID,
    USER_START_ID,
    build_account_start_keys,
    build_entity_keys,
)
from src.features.history import TIME, compute_change_flags
from src.features.split import time_cutoff

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
SIZE_THRESHOLDS = (100, 500, 1000)
PURITY_COLUMNS = ("P_emaildomain", "DeviceInfo")
CARD_FLAG_COLUMNS = ("DeviceInfo", "DeviceType", "addr1", "addr2")
USER_FLAG_COLUMNS = ("DeviceInfo", "DeviceType")


def _r(x: float, nd: int = 4) -> float | None:
    return None if pd.isna(x) else round(float(x), nd)


def group_size_stats(key: pd.Series) -> dict[str, Any]:
    vc = key.value_counts()
    n = len(key)
    return {
        "entities": len(vc),
        "singletons": int((vc == 1).sum()),
        "size_p50": _r(vc.quantile(0.5), 1),
        "size_p90": _r(vc.quantile(0.9), 1),
        "size_p99": _r(vc.quantile(0.99), 1),
        "size_max": int(vc.max()) if len(vc) else 0,
        "row_weighted_size_p50": _r(key.map(vc).median(), 1),
        **{
            f"pct_rows_in_groups_gt_{c}": _r(100 * vc[vc > c].sum() / n, 2) for c in SIZE_THRESHOLDS
        },
    }


def modal_purity(key: pd.Series, values: pd.Series) -> float | None:
    """Share of rows matching their group's most common value (groups with >= 2 such rows)."""
    m = key.notna() & values.notna()
    g = pd.DataFrame({"k": key[m], "v": values[m].astype(str)})
    g = g[g.groupby("k")["k"].transform("size") > 1]
    if g.empty:
        return None
    top = g.groupby(["k", "v"]).size().groupby(level=0).max()
    return _r(top.sum() / len(g))


def shuffle_within(values: pd.Series, groups: pd.Series, seed: int) -> pd.Series:
    """Permute ``values`` inside each group (vectorised, deterministic for a seed)."""
    codes, _ = pd.factorize(groups, use_na_sentinel=True)
    rng = np.random.default_rng(seed)
    by_group = np.argsort(codes, kind="stable")
    shuffled = np.lexsort((rng.random(len(codes)), codes))
    out = values.to_numpy().copy()
    out[by_group] = values.to_numpy()[shuffled]
    return pd.Series(out, index=values.index, name=values.name)


def flag_stats(flags: pd.DataFrame, label: pd.Series, train: pd.Series) -> dict[str, Any]:
    stats = {}
    for col in flags.columns:
        f = flags[col]
        ft, yt = f[train], label[train]
        stats[col] = {
            "null_rate": _r(f.isna().mean()),
            "change_rate_non_null": _r(f.mean()),
            "train_fraud_rate_when_1": _r(yt[ft == 1].mean()) if (ft == 1).any() else None,
            "train_fraud_rate_when_0": _r(yt[ft == 0].mean()) if (ft == 0).any() else None,
        }
    return stats


def d1_profile(df: pd.DataFrame) -> dict[str, Any]:
    d1 = df["D1"]
    day = df[TIME] // SECONDS_PER_DAY
    block = (day - day.min()) // 30
    return {
        "missing_rate": _r(d1.isna().mean(), 5),
        "min": _r(d1.min(), 1),
        "max": _r(d1.max(), 1),
        "zero_rate": _r((d1 == 0).mean()),
        "zero_rate_by_product": {
            str(k): _r(v)
            for k, v in (d1 == 0).groupby(df["ProductCD"], observed=True).mean().items()
        },
        "missing_rate_by_30_day_block": {
            int(k): _r(v, 5) for k, v in d1.isna().groupby(block).mean().items()
        },
    }


def compare_proxies(df: pd.DataFrame, *, valid_fraction: float = 0.2, seed: int = 0) -> dict:
    keys = pd.concat([build_entity_keys(df), build_account_start_keys(df)], axis=1)
    for base, cand in ((CARD_ID, CARD_START_ID), (USER_ID, USER_START_ID)):
        null_df = df.assign(D1=shuffle_within(df["D1"], keys[base], seed))
        keys[f"{cand}_null"] = build_account_start_keys(null_df)[cand]

    train = df[TIME] < time_cutoff(df[TIME], valid_fraction)
    flags: dict[str, Any] = {}
    for name in (CARD_ID, CARD_START_ID, f"{CARD_START_ID}_null"):
        f = compute_change_flags(df, keys[name], CARD_FLAG_COLUMNS, "flag")
        flags[name] = flag_stats(f, df[LABEL], train)
    for name in (USER_ID, USER_START_ID, f"{USER_START_ID}_null"):
        f = compute_change_flags(df, keys[name], USER_FLAG_COLUMNS, "flag")
        flags[name] = flag_stats(f, df[LABEL], train)

    return {
        "rows": len(df),
        "train_rows_for_fraud_rate_tables": int(train.sum()),
        "d1": d1_profile(df),
        "group_sizes": {name: group_size_stats(keys[name]) for name in keys.columns},
        "non_key_purity": {
            name: {c: modal_purity(keys[name], df[c]) for c in PURITY_COLUMNS}
            for name in keys.columns
        },
        "change_flags": flags,
    }


def main(
    raw_dir: Path = DEFAULT_RAW_DIR, out: Path = REPORTS_DIR / "proxy_comparison.json"
) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    started = time.perf_counter()
    df = load_ieee_cis(raw_dir, usecols=[])
    metrics = {"dataset": "IEEE-CIS train", **compare_proxies(df)}
    metrics["runtime_seconds"] = round(time.perf_counter() - started, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", out)
    return metrics


if __name__ == "__main__":
    main()
