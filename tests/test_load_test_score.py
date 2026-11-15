"""scripts/load_test_score.py: ``--skip`` slices the replay events without touching any database."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.load_test_score import skip_events


def _events(n: int) -> list[dict[str, Any]]:
    return [{"TransactionID": 100 + i, "TransactionDT": 1_000 + i} for i in range(n)]


def test_skip_zero_keeps_all_events_in_order() -> None:
    events = _events(5)
    assert skip_events(events, 0) == events


def test_skip_drops_exactly_the_first_events_and_keeps_order() -> None:
    kept = skip_events(_events(5), 2)
    assert [e["TransactionID"] for e in kept] == [102, 103, 104]


def test_consecutive_runs_get_disjoint_contiguous_slices() -> None:
    events = _events(10)
    used_by_run1 = 4
    run1 = events[:used_by_run1]
    run2 = skip_events(events, used_by_run1)
    assert {e["TransactionID"] for e in run1}.isdisjoint(e["TransactionID"] for e in run2)
    assert run1 + run2 == events


@pytest.mark.parametrize("skip", [-1, 5, 6])
def test_skip_outside_the_events_is_rejected(skip: int) -> None:
    with pytest.raises(SystemExit, match="--skip must be in"):
        skip_events(_events(5), skip)
