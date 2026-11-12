"""M4: drift-trigger calibration (scripts/drift_calibration.py)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from scripts import drift_calibration as cal
from src.features.history import TIME, HistoryConfig
from src.models.compare import ComparisonConfig, build_model_frame
from src.monitoring.windows import Window, days
from src.serving.model import ModelBundle
from tests.test_monitoring import small_cfg

DAY = 86_400


def test_windows_are_fixed_non_overlapping_and_inside_the_training_period() -> None:
    train = Window("champion_train", 86_400, 12_192_899)  # v2: TransactionDT 86,400 .. 12,192,899
    windows = cal.plan_windows(train, reference_days=30)
    print([(w.start / DAY, (w.end + 1) / DAY) for w in windows])
    assert len(windows) == 11
    assert windows[0].start == train.start + days(60)
    for a, b in zip(windows, windows[1:], strict=False):
        assert b.start == a.end + 1  # adjacent, no overlap
    assert all(w.end - w.start + 1 == days(7) for w in windows)
    assert windows[-1].end <= train.end < windows[-1].end + days(7)


def test_a_window_is_evaluated_with_the_frozen_rules(
    synthetic_frame: pd.DataFrame, bundle_dir: Any
) -> None:
    champion = ModelBundle.load(bundle_dir)
    frame = build_model_frame(synthetic_frame, "baseline", HistoryConfig())
    start = int(frame[TIME].min())
    window = Window("cal", start + days(45), start + days(52) - 1)
    recipe = ComparisonConfig(seed=42, imbalance="none", max_rounds=40, early_stopping_rounds=10)
    cfg = small_cfg(performance={"min_positives": 1})  # reference 10 days, min_rows 1
    r = cal.evaluate_window(frame, window, cfg, recipe, champion.feature_columns, start)
    assert r.current_rows > 0 and r.reference_rows > 0 and r.sufficient
    assert len(r.top_gain_features) == cfg.feature_drift.top_gain_features
    assert set(r.drifted_top_gain_features) <= set(r.top_gain_features)
    assert r.fired == bool(r.reasons)

    strict = small_cfg(current={"min_rows": 1_000_000})
    s = cal.evaluate_window(frame, window, strict, recipe, champion.feature_columns, start)
    assert not s.fired and not s.sufficient
    assert s.reasons and s.reasons[0].startswith("insufficient data")


def test_report_counts_fires_and_rules() -> None:
    def result(fired: bool, reasons: list[str]) -> cal.WindowResult:
        return cal.WindowResult(
            61,
            68,
            20_000,
            90_000,
            True,
            0.1,
            ["card3"],
            0.01,
            False,
            ["card3"],
            ["card3"],
            20_000,
            600,
            0.3,
            0.31,
            fired,
            reasons,
            1.0,
        )

    md = cal.render(
        [result(True, ["top-feature drift: card3 drifted"]), result(False, [])],
        {
            "finished": "x",
            "git_commit": "abc",
            "config_sha256": "0" * 64,
            "min_rows": 1000,
            "seconds": 1.0,
        },
    )
    assert "False fires: 1 of 2 windows (50%)" in md
    assert "top-feature drift 1" in md
    assert "card3 2" in md


@pytest.mark.parametrize("reason", ["share of drifted features 0.6 > 0.5", "psi: 0.3"])
def test_rule_of_takes_the_rule_name(reason: str) -> None:
    assert cal.rule_of(reason) == reason.split(":", 1)[0].strip()
