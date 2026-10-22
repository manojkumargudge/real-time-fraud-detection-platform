"""Aggregate-only data-quality report for the IEEE-CIS train split.

Usage: ``python -m src.data.quality`` writes ``reports/data_quality.json`` and
``reports/data_quality.md``. Statistics only: no rows, identifiers or raw values are written.

Checks: file sizes and dimensions, key uniqueness (enforced by the loader), exact duplicate
rows ignoring ``TransactionID``, label values and prevalence, value ranges, missingness,
left-join integrity, stage timings and the process's measured peak memory.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import time
from pathlib import Path
from typing import Any, cast

import pandas as pd

from src.data.eda import column_family, merge_integrity
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

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def peak_memory_mb() -> float | None:
    """Peak resident memory of this process in MB (Windows working set / POSIX max RSS)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        ok = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return round(counters.PeakWorkingSetSize / 1e6, 1) if ok else None
    else:
        try:
            import resource
        except ImportError:
            return None
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(rss / 1e6 if sys.platform == "darwin" else rss / 1e3, 1)  # bytes vs KB


# Rates keep 6 decimals so 1-decimal percentages in the markdown are not double-rounded.
def _r(x: float, nd: int = 6) -> float:
    return round(float(x), nd)


def duplicate_rows_ignoring_key(df: pd.DataFrame) -> int:
    """Rows identical to an earlier row in every column except ``TransactionID``."""
    hashes = pd.util.hash_pandas_object(df.drop(columns=[KEY]), index=False)
    return int(hashes.duplicated().sum())


def file_profile(df: pd.DataFrame, path: Path) -> dict[str, Any]:
    missing = df.isna().mean()
    return {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "rows": len(df),
        "columns": df.shape[1],
        "transaction_id_unique": bool(df[KEY].is_unique),
        "exact_duplicate_rows_ignoring_id": duplicate_rows_ignoring_key(df),
        "cell_missing_rate": _r(df.isna().to_numpy().mean()),
        "columns_fully_present": int((missing == 0).sum()),
        "columns_over_50pct_missing": int((missing > 0.5).sum()),
        "columns_over_90pct_missing": int((missing > 0.9).sum()),
        "columns_fully_missing": int((missing == 1).sum()),
    }


def value_checks(txn: pd.DataFrame) -> dict[str, Any]:
    labels = txn[LABEL].value_counts().sort_index()
    dt, amt = txn["TransactionDT"], txn["TransactionAmt"]
    return {
        "label_values": {str(int(cast(int, k))): int(v) for k, v in labels.items()},
        "fraud_rate": _r(txn[LABEL].mean()),
        "transaction_dt_min": int(dt.min()),
        "transaction_dt_max": int(dt.max()),
        "transaction_dt_sorted": bool(dt.is_monotonic_increasing),
        "rows_sharing_a_timestamp": int(dt.duplicated(keep=False).sum()),
        "amount_min": _r(amt.min(), 3),
        "amount_max": _r(amt.max(), 2),
        "amount_zero_rows": int((amt == 0).sum()),
        "amount_negative_rows": int((amt < 0).sum()),
    }


def missingness_by_family(merged: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    by_family = merged.isna().mean().groupby(column_family).agg(["size", "mean"])
    return {
        cast(str, fam): {"columns": int(r["size"]), "mean_missing": _r(r["mean"])}
        for fam, r in by_family.sort_values("mean").iterrows()
    }


def build_report(raw_dir: Path = DEFAULT_RAW_DIR) -> dict[str, Any]:
    paths = IEEECISPaths.from_dir(raw_dir, "train")
    started = time.perf_counter()
    timings: dict[str, float] = {}

    t = time.perf_counter()
    txn = load_transactions(paths.transaction)
    timings["load_transactions"] = time.perf_counter() - t
    t = time.perf_counter()
    ident = load_identity(paths.identity)
    timings["load_identity"] = time.perf_counter() - t
    t = time.perf_counter()
    merged = merge_transaction_identity(txn, ident)
    timings["merge"] = time.perf_counter() - t

    t = time.perf_counter()
    report: dict[str, Any] = {
        "dataset": "IEEE-CIS train split",
        "files": {
            "transaction": file_profile(txn, paths.transaction),
            "identity": file_profile(ident, paths.identity),
        },
        "values": value_checks(txn),
        "merge": {
            "join": f"left join of identity onto transactions on {KEY} (one-to-one validated)",
            **merge_integrity(txn, ident, merged),
            "merged_columns": merged.shape[1],
            "has_identity_rate": _r(merged[HAS_IDENTITY].mean()),
            "merged_memory_mb_optimized_dtypes": round(
                merged.memory_usage(deep=True).sum() / 1e6, 1
            ),
        },
        "missingness_by_family_merged": missingness_by_family(merged),
    }
    timings["checks"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - started
    report["execution"] = {
        "seconds": {k: round(v, 1) for k, v in timings.items()},
        "peak_memory_mb": peak_memory_mb(),
        "peak_memory_method": (
            "Windows PeakWorkingSetSize of this process"
            if sys.platform == "win32"
            else "getrusage ru_maxrss of this process"
        ),
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "platform": platform.platform(),
    }
    return report


def render_markdown(r: dict[str, Any]) -> str:
    f, v, m, e = r["files"], r["values"], r["merge"], r["execution"]
    file_rows = [
        f"| {p['file']} | {p['size_bytes'] / 1e6:,.1f} MB | {p['rows']:,} | {p['columns']} | "
        f"{'yes' if p['transaction_id_unique'] else 'NO'} | "
        f"{p['exact_duplicate_rows_ignoring_id']:,} | {100 * p['cell_missing_rate']:.1f}% | "
        f"{p['columns_over_50pct_missing']} / {p['columns_over_90pct_missing']} / "
        f"{p['columns_fully_missing']} |"
        for p in (f["transaction"], f["identity"])
    ]
    fam_rows = [
        f"| `{k}` | {x['columns']} | {100 * x['mean_missing']:.1f}% |"
        for k, x in r["missingness_by_family_merged"].items()
    ]
    secs = e["seconds"]
    return "\n".join(
        [
            "# Data Quality Report — IEEE-CIS train split",
            "",
            "Generated by `python -m src.data.quality`; values in "
            "[`data_quality.json`](data_quality.json). Aggregates only.",
            "",
            "## Files",
            "",
            "| File | Size | Rows | Columns | ID unique | Exact duplicate rows (ignoring ID) "
            "| Cells missing | Columns >50% / >90% / 100% missing |",
            "|---|---|---|---|---|---|---|---|",
            *file_rows,
            "",
            "## Labels and values (transaction file)",
            "",
            f"- Labels: {v['label_values']} → fraud prevalence {100 * v['fraud_rate']:.3f}%",
            f"- `TransactionDT`: {v['transaction_dt_min']:,} – {v['transaction_dt_max']:,} s, "
            f"sorted: {v['transaction_dt_sorted']}, rows sharing a timestamp: "
            f"{v['rows_sharing_a_timestamp']:,}",
            f"- `TransactionAmt`: min {v['amount_min']}, max {v['amount_max']:,}, "
            f"zero rows {v['amount_zero_rows']}, negative rows {v['amount_negative_rows']}",
            "",
            "## Merge",
            "",
            f"- {m['join']}",
            f"- Rows: transactions {m['transaction_rows']:,}, identity {m['identity_rows']:,}, "
            f"merged {m['merged_rows']:,} (row count preserved: {m['row_count_preserved']})",
            f"- Identity rows matched {m['identity_rows_matched']:,}, orphaned "
            f"{m['identity_rows_orphaned']:,}; merged IDs unique: {m['transaction_ids_unique']}",
            f"- Merged columns {m['merged_columns']}, `has_identity` rate "
            f"{100 * m['has_identity_rate']:.2f}%, in-memory "
            f"{m['merged_memory_mb_optimized_dtypes']:,} MB",
            "",
            "## Missingness by column family (merged)",
            "",
            "| Family | Columns | Mean missing |",
            "|---|---|---|",
            *fam_rows,
            "",
            "## Execution",
            "",
            f"- Seconds: load transactions {secs['load_transactions']}, load identity "
            f"{secs['load_identity']}, merge {secs['merge']}, checks {secs['checks']}, "
            f"total {secs['total']}",
            f"- Peak memory: {e['peak_memory_mb']} MB ({e['peak_memory_method']})",
            f"- Python {e['python']}, pandas {e['pandas']}, {e['platform']}",
            "",
        ]
    )


def main(raw_dir: Path = DEFAULT_RAW_DIR, out_dir: Path = REPORTS_DIR) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    report = build_report(raw_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data_quality.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "data_quality.md").write_text(render_markdown(report), encoding="utf-8")
    logger.info("wrote %s", out_dir / "data_quality.json")
    return report


if __name__ == "__main__":
    main()
