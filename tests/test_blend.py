"""GNN + XGBoost blend experiment (src/graph/blend.py): the pure parts, no torch, no raw data."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.graph import blend as b
from src.graph.build import GNN_SCORE


def test_weight_zero_is_xgboost_and_missing_gnn_falls_back_to_xgboost() -> None:
    xgb = np.array([0.1, 0.5, 0.9])
    gnn = np.array([0.8, np.nan, 0.2])
    assert np.array_equal(b.blend(xgb, gnn, 0.0), xgb)
    out = b.blend(xgb, gnn, 0.3)
    assert out[1] == xgb[1]  # unseen entity: the blend equals XGBoost exactly
    assert out[0] == pytest.approx(0.7 * 0.1 + 0.3 * 0.8)


def test_entity_scores_use_only_the_history_rows_and_skip_unscored_ones() -> None:
    history = pd.DataFrame({"uid": ["a", "a", "b", "c"], GNN_SCORE: [0.2, 0.4, np.nan, 0.9]})
    out = b.entity_scores(history, pd.Series(["a", "b", "d", "c"]))
    assert out[0] == pytest.approx(0.3)
    assert np.isnan(out[1]) and np.isnan(out[2])  # b has no scored row, d is unseen
    assert out[3] == pytest.approx(0.9)
    # Later rows (not in the history) never change an entity score.
    assert np.array_equal(
        b.entity_scores(history, pd.Series(["a"])),
        b.entity_scores(history.iloc[:2], pd.Series(["a"])),
    )


def test_choose_weight_is_the_largest_not_worse_by_more_than_the_margin() -> None:
    rows = [
        {"w": 0.0, "ci95_low": 0.0},
        {"w": 0.05, "ci95_low": -0.004},
        {"w": 0.1, "ci95_low": -0.0099},
        {"w": 0.15, "ci95_low": -0.02},
        {"w": 0.2, "ci95_low": -0.009},  # the rule takes the largest passing w, not the first run
    ]
    assert b.choose_weight(rows, margin=0.01) == 0.2
    assert b.choose_weight([{"w": 0.1, "ci95_low": -0.5}], margin=0.01) == 0.0


def side(**kw: object) -> b.Side:
    base: dict[str, object] = {
        "name": "t",
        "y": np.array([0, 1, 0, 1]),
        "day": np.array([1, 1, 2, 2]),
        "xgb": np.array([0.1, 0.9, 0.2, 0.7]),
        "gnn": np.array([0.3, np.nan, 0.1, 0.6]),
    }
    return b.Side(**(base | kw))  # type: ignore[arg-type]


def test_side_refuses_empty_unequal_or_fraud_free_inputs() -> None:
    assert "rows 4, frauds 2, with a GNN score 3 (75.0%)" in side().counts()
    with pytest.raises(AssertionError, match="empty side"):
        side(y=np.array([], dtype=int), day=np.array([]), xgb=np.array([]), gnn=np.array([]))
    with pytest.raises(AssertionError, match="no frauds"):
        side(y=np.array([0, 0, 0, 0]))
    with pytest.raises(AssertionError, match="differ in length"):
        side(gnn=np.array([0.1, 0.2]))


def test_compare_at_weight_zero_has_zero_delta() -> None:
    r = b.compare(side(), 0.0)
    assert r["ap_blend"] == r["ap_xgb"]
    assert r["delta_mean"] == 0.0 and r["ci95_low"] == 0.0 and r["ci95_high"] == 0.0


def test_time_blocks_are_contiguous_and_ties_share_a_block() -> None:
    t = np.array([0, 1, 2, 3, 3, 4, 5, 6, 7, 8])
    block = b.time_blocks(t, 3)
    assert list(block) == sorted(block) and set(block) == {0, 1, 2}
    assert block[3] == block[4]


def test_time_blocks_match_the_stack_run() -> None:
    pytest.importorskip("torch_geometric")
    from src.graph import stack

    t = np.random.default_rng(0).integers(0, 10_000, 5_000)
    assert np.array_equal(b.time_blocks(t, 3), stack.time_blocks(t, 3))
    assert stack.FOLDS == b.FOLDS
