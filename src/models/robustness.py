"""Seed robustness of the baseline vs candidate comparison.

Usage: ``python -m src.models.robustness`` writes ``reports/model_comparison_seeds.json``
(aggregates only). The primary seed-42 report from ``src.models.compare`` is not touched.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis
from src.models.compare import REPORTS_DIR, ComparisonConfig, run_comparison

logger = logging.getLogger(__name__)

DEFAULT_SEEDS = (1, 2, 3)


def summarise_seed(report: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant, m in report["results"].items():
        t = m["at_threshold"]
        out[variant] = {
            "average_precision": m["average_precision"],
            "precision": t["precision"],
            "recall": t["recall"],
            "f1": t["f1"],
            "flagged_rate": t["flagged_rate"],
            "threshold": t["threshold"],
        }
    out["ap_delta"] = out["candidate"]["average_precision"] - out["baseline"]["average_precision"]
    out["bootstrap"] = report["ap_delta_candidate_minus_baseline_day_bootstrap"]
    out["valid_id_digest"] = report["split"]["valid_id_digest"]
    return out


def run_seeds(
    df: Any, seeds: tuple[int, ...], resamples: int, base: ComparisonConfig | None = None
) -> dict[str, Any]:
    per_seed = {}
    for seed in seeds:
        started = time.perf_counter()
        cfg = replace(base or ComparisonConfig(), seed=seed, bootstrap_resamples=resamples)
        report = run_comparison(df, cfg)
        per_seed[seed] = summarise_seed(report) | {"seconds": round(time.perf_counter() - started)}
        s = per_seed[seed]
        print(
            f"seed={seed} "
            + " ".join(
                f"{v}: ap={s[v]['average_precision']:.4f} p={s[v]['precision']:.4f} "
                f"r={s[v]['recall']:.4f} f1={s[v]['f1']:.4f}"
                for v in ("baseline", "candidate")
            )
            + f" delta={s['ap_delta']:+.4f}",
            flush=True,
        )
    deltas = np.array([s["ap_delta"] for s in per_seed.values()])
    if len({s["valid_id_digest"] for s in per_seed.values()}) != 1:
        raise RuntimeError("seeds were evaluated on different validation rows")
    return {
        "seeds": list(seeds),
        "bootstrap_resamples_per_seed": resamples,
        "per_seed": per_seed,
        "ap_delta_min": float(deltas.min()),
        "ap_delta_max": float(deltas.max()),
        "ap_delta_mean": float(deltas.mean()),
        "all_seeds_candidate_better": bool((deltas > 0).all()),
    }


def main(
    raw_dir: Path = DEFAULT_RAW_DIR,
    out: Path = REPORTS_DIR / "model_comparison_seeds.json",
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    resamples: int = 100,
) -> dict[str, Any]:
    logging.basicConfig(level=logging.WARNING)
    df = load_ieee_cis(raw_dir, usecols=[])
    result = run_seeds(df, seeds, resamples)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    from src.models.tracking import flatten_metrics, log_report_run  # mlflow: CLI only

    log_report_run(
        "model-comparison-seeds",
        [out],
        metrics=flatten_metrics(result),
        tags={"module": "src.models.robustness"},
    )
    return result


if __name__ == "__main__":
    main()
