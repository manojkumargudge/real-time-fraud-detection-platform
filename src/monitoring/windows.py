"""Time windows for the drift job, in transaction time (``TransactionDT`` seconds).

"Now" is the latest ``transaction_dt`` in ``scored_transactions``, never wall-clock time. A
label is usable only if its transaction is at least ``delay_days`` older than now; a label
exactly at the cutoff is usable. ``usable_labels`` is the only place labelled rows are selected,
so nothing newer than the cutoff can reach the performance check, retraining or the holdout.

All windows are closed integer intervals ``[start, end]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.features.entity import SECONDS_PER_DAY
from src.features.history import TIME


class WindowOverlapError(RuntimeError):
    """A training window overlaps the holdout: the comparison would be in-sample."""


@dataclass(frozen=True)
class Window:
    name: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window {self.name}: end {self.end} is before start {self.start}")

    def overlaps(self, other: Window) -> bool:
        return self.start <= other.end and other.start <= self.end

    def mask(self, dt: pd.Series[int]) -> pd.Series[bool]:
        return (dt >= self.start) & (dt <= self.end)

    def describe(self) -> str:
        return (
            f"{self.name}: TransactionDT {self.start}..{self.end} "
            f"(day {self.start / SECONDS_PER_DAY:.2f} .. {self.end / SECONDS_PER_DAY:.2f})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_dt": self.start,
            "end_dt": self.end,
            "start_day": round(self.start / SECONDS_PER_DAY, 3),
            "end_day": round(self.end / SECONDS_PER_DAY, 3),
        }


def days(n: float) -> int:
    return int(round(n * SECONDS_PER_DAY))


def label_cutoff(now_dt: int, delay_days: int) -> int:
    """Latest transaction time whose label may be used."""
    return now_dt - days(delay_days)


def usable_labels(frame: pd.DataFrame, cutoff: int) -> pd.DataFrame:
    """Rows whose label is old enough (``TransactionDT <= cutoff``); everything newer is dropped."""
    return frame.loc[frame[TIME] <= cutoff]


def day_window(name: str, start_day: float, end_day: float) -> Window:
    """``[start_day, end_day)`` in days of transaction time, as a closed window in seconds."""
    return Window(name, days(start_day), days(end_day) - 1)


@dataclass(frozen=True)
class RetrainWindows:
    champion_train: Window
    challenger_train: Window
    holdout: Window

    def all(self) -> list[Window]:
        return [self.champion_train, self.challenger_train, self.holdout]


def plan_retrain(
    *, champion_train: Window, train_dt_min: int, cutoff: int, holdout_days: int
) -> RetrainWindows:
    """Holdout = ``(cutoff - holdout_days, cutoff]``; the challenger trains on every usable label
    before it. Raises ``WindowOverlapError`` if either training window overlaps the holdout."""
    holdout = Window("holdout", cutoff - days(holdout_days) + 1, cutoff)
    challenger = Window("challenger_train", train_dt_min, holdout.start - 1)
    windows = RetrainWindows(champion_train, challenger, holdout)
    check_no_overlap(windows)
    return windows


def check_no_overlap(windows: RetrainWindows) -> None:
    for train in (windows.champion_train, windows.challenger_train):
        if train.overlaps(windows.holdout):
            raise WindowOverlapError(
                f"{train.describe()} overlaps {windows.holdout.describe()}; the champion/"
                "challenger comparison would use training rows. Wait for more labels or "
                "shorten retrain.holdout_days."
            )
