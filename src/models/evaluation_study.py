"""Evaluation study: seen/unseen entities, identifier-input ablation, label-delay gaps.

Usage: ``python -m src.models.evaluation_study`` writes ``reports/evaluation_study.{json,md}``
(aggregates only: no rows, identifiers or predictions). Experimental: the serving model,
its features and the scoring path are unchanged.

Protocol (shared by every variant and gap):

* Validation window fixed at the Milestone 2 cutoff (``TransactionDT >= valid_start``), so all
  gaps and variants score the **identical** rows (checked by an ID digest).
* For a gap ``g``: training rows are ``TransactionDT < valid_start - g``; rows in
  ``[valid_start - g, valid_start)`` are excluded from training and evaluation. Larger gaps
  therefore also train on less, older data -- a confound reported alongside the results.
* History features are computed once with the point-in-time rule (strictly earlier rows, no
  labels). Gap transactions may appear in a validation row's *history* (events are observed
  immediately; only labels are delayed) but never in a training row's features.
* Model, rounds and threshold come from ``compare.train_variant`` on training rows only;
  validation labels are used only to compute the reported metrics.
* Gap lengths are a sensitivity grid. They are **not** estimates of chargeback latency, which
  this project's dataset documentation does not establish.
"""

from __future__ import annotations

import json
import logging
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import BoolArray, FloatArray, NumArray
from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.entity import CARD_ID, SECONDS_PER_DAY, USER_ID, build_entity_keys
from src.features.history import TIME, HistoryConfig
from src.features.split import SplitBoundary, chronological_split, time_cutoff
from src.models.compare import (
    REPORTS_DIR,
    ComparisonConfig,
    _id_digest,
    build_model_frame,
    feature_columns,
    score,
    train_variant,
)
from src.models.leakage_audit import IDENTIFIER_INPUTS
from src.models.metrics import (
    average_precision,
    day_block_bootstrap_ap,
    day_block_bootstrap_ap_delta,
)

logger = logging.getLogger(__name__)

DEFAULT_GAPS_DAYS = (0, 7, 30, 60)
BASELINE, RESTRICTED = "baseline", "no_identifier_codes"
LOW_ROUNDS = 20  # report-only warning threshold; does not change training

SUBGROUP_DEFINITIONS = {
    "all": "every validation row",
    "card_seen": "card_id present in the model's training rows",
    "card_unseen": "card_id absent from training rows (implies uid unseen: uid extends card_id)",
    "uid_seen": "uid present in the model's training rows",
    "uid_unseen": "uid absent from training rows (new card, or known card with a new addr1)",
    "card_seen_uid_unseen": "known card_id but a uid not in training rows (e.g. new addr1)",
    "no_entity_key": "card1 missing, so no card_id/uid can be formed",
}


@dataclass(frozen=True)
class StudyConfig:
    gaps_days: tuple[int, ...] = DEFAULT_GAPS_DAYS
    bootstrap_resamples: int = 200
    min_positives: int = 50  # below this many frauds a subgroup is flagged as unreliable
    model: ComparisonConfig = field(default_factory=ComparisonConfig)


def study_variants(hist: HistoryConfig) -> dict[str, list[str]]:
    cols = feature_columns(hist)
    return {BASELINE: cols, RESTRICTED: [c for c in cols if c not in IDENTIFIER_INPUTS]}


def gap_split(
    frame: pd.DataFrame, valid_start: int, gap_seconds: int
) -> tuple[pd.DataFrame, pd.DataFrame, SplitBoundary]:
    """Train ``< valid_start - gap``; validation ``>= valid_start``; gap rows in neither."""
    if gap_seconds < 0:
        raise ValueError("gap_seconds must be >= 0")
    return chronological_split(frame, train_end=valid_start - gap_seconds, gap_seconds=gap_seconds)


def subgroup_masks(train_keys: pd.DataFrame, valid_keys: pd.DataFrame) -> dict[str, BoolArray]:
    """Boolean masks over validation rows; "seen" means present in ``train_keys`` only."""
    seen_card = set(train_keys[CARD_ID].dropna())
    seen_uid = set(train_keys[USER_ID].dropna())
    card, uid = valid_keys[CARD_ID], valid_keys[USER_ID]
    has_card = card.notna().to_numpy()
    card_seen = has_card & card.isin(seen_card).to_numpy()
    has_uid = uid.notna().to_numpy()
    uid_seen = has_uid & uid.isin(seen_uid).to_numpy()
    return {
        "all": np.ones(len(valid_keys), dtype=bool),
        "card_seen": card_seen,
        "card_unseen": has_card & ~card_seen,
        "uid_seen": uid_seen,
        "uid_unseen": has_uid & ~uid_seen,
        "card_seen_uid_unseen": card_seen & has_uid & ~uid_seen,
        "no_entity_key": ~has_card,
    }


def _ratio(num: float, den: float) -> float | None:
    return float(num / den) if den else None


def group_metrics(
    y: NumArray,
    scores: NumArray,
    threshold: float,
    day: NumArray,
    *,
    min_positives: int,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    """Ranking (AP) and fixed-threshold metrics for one subgroup; robust to empty groups."""
    n, pos = len(y), int(y.sum())
    pred = scores >= threshold
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    precision, recall = _ratio(tp, tp + fp), _ratio(tp, pos)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else (0.0 if precision is not None and recall is not None else None)
    )
    ranking: dict[str, Any] = {"average_precision": None, "ci95": None}
    if pos > 0:
        ranking["average_precision"] = average_precision(y, scores)
        if resamples:
            b = day_block_bootstrap_ap(y, scores, day, resamples=resamples, seed=seed)
            ranking["ci95"] = [b["ci95_low"], b["ci95_high"]]
    return {
        "rows": n,
        "positives": pos,
        "prevalence": _ratio(pos, n),
        "reliable": pos >= min_positives,
        "ranking": ranking,
        "at_threshold": {
            "threshold": threshold,
            "tp": tp,
            "fp": fp,
            "fn": pos - tp,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "flagged": int(pred.sum()),
            "flagged_rate": _ratio(int(pred.sum()), n),
        },
    }


def run_study(df: pd.DataFrame, cfg: StudyConfig | None = None) -> dict[str, Any]:
    cfg = cfg or StudyConfig()
    hist = HistoryConfig()
    seed, resamples = cfg.model.seed, cfg.bootstrap_resamples
    frame = build_model_frame(df, "baseline", hist)  # point-in-time history, no labels
    keys = build_entity_keys(df)
    valid_start = time_cutoff(df[TIME], cfg.model.valid_fraction)
    variants = study_variants(hist)

    digest: str | None = None
    gaps: dict[str, Any] = {}
    for gap_days in cfg.gaps_days:
        gap_s = int(gap_days) * SECONDS_PER_DAY
        train, valid, boundary = gap_split(frame, valid_start, gap_s)
        d = _id_digest(valid[KEY])
        if digest is not None and d != digest:
            raise RuntimeError("validation rows differ between gaps")
        digest = d
        y = valid[LABEL].to_numpy()
        day = (valid[TIME] // SECONDS_PER_DAY).to_numpy()
        masks = subgroup_masks(keys.loc[train.index], keys.loc[valid.index])
        x_valid = valid.drop(columns=LABEL)  # scoring never sees validation labels

        results: dict[str, dict[str, Any]] = {}
        scores: dict[str, FloatArray] = {}
        for name, cols in variants.items():
            trained = train_variant(train, cols, cfg.model)
            s = score(trained, x_valid, cols)
            scores[name] = s
            results[name] = {
                "n_inputs": len(cols),
                "threshold": trained.threshold,
                "best_rounds": trained.best_rounds,
                "scale_pos_weight": trained.scale_pos_weight,
                "train_holdout_average_precision": trained.holdout_average_precision,
                "subgroups": {
                    g: group_metrics(
                        y[m],
                        s[m],
                        trained.threshold,
                        day[m],
                        min_positives=cfg.min_positives,
                        resamples=resamples,
                        seed=seed,
                    )
                    for g, m in masks.items()
                },
            }
            logger.info(
                "gap_days=%d variant=%s valid_ap=%.4f rounds=%d",
                gap_days,
                name,
                results[name]["subgroups"]["all"]["ranking"]["average_precision"],
                trained.best_rounds,
            )
        ablation = {
            g: day_block_bootstrap_ap_delta(
                y[masks[g]],
                scores[BASELINE][masks[g]],
                scores[RESTRICTED][masks[g]],
                day[masks[g]],
                resamples=resamples,
                seed=seed,
            )
            for g in ("all", "card_seen", "card_unseen", "uid_seen", "uid_unseen")
            if resamples and int(y[masks[g]].sum()) >= cfg.min_positives
        }
        excluded = (frame[TIME] >= boundary.train_end) & (frame[TIME] < valid_start)
        gaps[str(gap_days)] = {
            "train_end": boundary.train_end,
            "train_rows": len(train),
            "train_fraud_rate": float(train[LABEL].mean()),
            "gap_rows_excluded": int(excluded.sum()),
            "subgroup_rows": {g: int(m.sum()) for g, m in masks.items()},
            "variants": results,
            "ap_delta_restricted_minus_baseline": ablation,
        }

    return {
        "dataset": "IEEE-CIS train (labelled file only)",
        "validation": {
            "valid_start": valid_start,
            "rows": int((frame[TIME] >= valid_start).sum()),
            "fraud_rate": float(frame.loc[frame[TIME] >= valid_start, LABEL].mean()),
            "id_digest": digest,
        },
        "subgroup_definitions": SUBGROUP_DEFINITIONS,
        "variants": {
            BASELINE: "current serving feature set (28 inputs)",
            RESTRICTED: f"baseline without raw identifier codes {list(IDENTIFIER_INPUTS)}",
        },
        "gap_assumption": (
            "gap lengths are a sensitivity grid, not measured chargeback latency; the gap period "
            "is excluded from training and evaluation, and larger gaps also shrink training data"
        ),
        "config": {**asdict(cfg), "history_windows": dict(hist.windows)},
        "gaps": gaps,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "xgboost": xgb.__version__,
        },
    }


def _f(x: float | None, nd: int = 4) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.2f}%"


def _low_round_models(r: dict[str, Any]) -> list[tuple[str, str, int]]:
    """(gap, variant, rounds) for models early-stopped below ``LOW_ROUNDS`` boosting rounds."""
    return [
        (gap, name, res["best_rounds"])
        for gap, g in r["gaps"].items()
        for name, res in g["variants"].items()
        if res["best_rounds"] < LOW_ROUNDS
    ]


def _ci(c: list[float] | None) -> str:
    return "—" if not c else f"[{c[0]:.3f}, {c[1]:.3f}]"


def render_markdown(r: dict[str, Any]) -> str:
    v, gaps = r["validation"], r["gaps"]
    zero = gaps[str(min(int(k) for k in gaps))]
    lines = [
        "# Evaluation study: seen/unseen entities, identifier ablation, label-delay gaps",
        "",
        "Generated by `python -m src.models.evaluation_study`; all values in "
        "[`evaluation_study.json`](evaluation_study.json). Aggregates only. Experimental — the "
        "serving model is unchanged.",
        "",
        f"Validation window: `TransactionDT >= {v['valid_start']:,}`, {v['rows']:,} rows, fraud "
        f"prevalence {100 * v['fraud_rate']:.2f}%. Identical rows for every gap and variant "
        f"(digest `{v['id_digest']}`). Seed {r['config']['model']['seed']}. Subgroups with fewer "
        f"than {r['config']['min_positives']} frauds are marked unreliable (✗).",
        "",
        "## Subgroup definitions",
        "",
        *[f"- **{k}**: {d}" for k, d in r["subgroup_definitions"].items()],
        "",
        "## 1. Seen vs unseen entities (gap 0)",
        "",
        "PR-AUC (ranking) is reported separately from metrics at the training-selected threshold.",
        "",
        "| Variant | Subgroup | Rows | Frauds | Prevalence | PR-AUC [95% CI] | Precision | "
        "Recall | F1 | Flagged | Reliable |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, res in zero["variants"].items():
        for g, m in res["subgroups"].items():
            t = m["at_threshold"]
            lines.append(
                f"| {name} | {g} | {m['rows']:,} | {m['positives']:,} | "
                f"{_pct(m['prevalence'])} | "
                f"{_f(m['ranking']['average_precision'])} {_ci(m['ranking']['ci95'])} | "
                f"{_f(t['precision'])} | {_f(t['recall'])} | {_f(t['f1'])} | "
                f"{_pct(t['flagged_rate'])} | "
                f"{'✓' if m['reliable'] else '✗'} |"
            )
    lines += [
        "",
        "## 2. Identifier ablation: PR-AUC change (restricted − baseline)",
        "",
        "Paired day-block bootstrap on identical rows; only subgroups with enough frauds.",
        "",
        "| Gap (days) | Subgroup | Δ PR-AUC mean | 95% CI | Restricted better in |",
        "|---|---|---|---|---|",
    ]
    low = _low_round_models(r)
    low_gaps = {gap for gap, _, _ in low}
    for gap, g in gaps.items():
        for sub, b in g["ap_delta_restricted_minus_baseline"].items():
            lines.append(
                f"| {gap}{' ⚠' if gap in low_gaps else ''} | {sub} | {b['delta_mean']:+.4f} | "
                f"[{b['ci95_low']:+.4f}, {b['ci95_high']:+.4f}] | "
                f"{100 * b['share_b_better']:.0f}% |"
            )
    lines += [
        "",
        "## 3. Label-delay sensitivity (all validation rows)",
        "",
        "| Gap (days) | Train rows | Train fraud rate | Gap rows excluded | Card-unseen valid rows "
        "| Variant | Rounds | PR-AUC [95% CI] | Threshold | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for gap, g in gaps.items():
        for name, res in g["variants"].items():
            m = res["subgroups"]["all"]
            t = m["at_threshold"]
            lines.append(
                f"| {gap} | {g['train_rows']:,} | {100 * g['train_fraud_rate']:.2f}% | "
                f"{g['gap_rows_excluded']:,} | {g['subgroup_rows']['card_unseen']:,} | {name} | "
                f"{res['best_rounds']}{' ⚠' if res['best_rounds'] < LOW_ROUNDS else ''} | "
                f"{_f(m['ranking']['average_precision'])} {_ci(m['ranking']['ci95'])} | "
                f"{_f(t['threshold'])} | {_f(t['precision'])} | {_f(t['recall'])} | {_f(t['f1'])} |"
            )
    if low:
        lines += [
            "",
            f"⚠ Early stopping chose fewer than {LOW_ROUNDS} boosting rounds for: "
            + ", ".join(f"gap {gap} / {name} ({n} rounds)" for gap, name, n in low)
            + ". Such a model is barely trained, so its metrics and the ablation deltas for that "
            "gap mainly reflect a noisy round selection on the training holdout, not the feature "
            "set. Treat them as unreliable.",
        ]
    lines += [
        "",
        f"Gap assumption: {r['gap_assumption']}.",
        "",
        "Interpretation, recommendation and limitations: see "
        "[docs/evaluation_study.md](../docs/evaluation_study.md).",
        "",
    ]
    return "\n".join(lines)


def main(
    raw_dir: Path = DEFAULT_RAW_DIR, out_dir: Path = REPORTS_DIR, cfg: StudyConfig | None = None
) -> dict[str, Any]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    report = run_study(load_ieee_cis(raw_dir, usecols=[]), cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evaluation_study.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "evaluation_study.md").write_text(render_markdown(report), encoding="utf-8")
    logger.info("wrote %s", out_dir / "evaluation_study.json")
    from src.models.tracking import flatten_metrics, log_report_run  # mlflow: CLI only

    log_report_run(
        "evaluation-study",
        [out_dir / "evaluation_study.json", out_dir / "evaluation_study.md"],
        metrics=flatten_metrics(report),
        tags={"module": "src.models.evaluation_study"},
    )
    return report


if __name__ == "__main__":
    main()
