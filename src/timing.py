"""Optional per-stage timings of one scored event (benchmarks; off unless a collector is set).

The consumer starts a collector per message when ``STAGE_TIMINGS=1``; ``stage(name)`` blocks
inside the scorer then add their wall time in milliseconds. Without a collector ``stage`` only
does a context-variable lookup, so normal serving is unaffected. A context variable (not a
global) keeps concurrent API requests apart.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar[dict[str, float] | None] = ContextVar("stage_timings", default=None)


def collect() -> dict[str, float]:
    """Start a new collector for the current context and return it."""
    timings: dict[str, float] = {}
    _current.set(timings)
    return timings


def clear() -> None:
    _current.set(None)


@contextmanager
def stage(name: str) -> Iterator[None]:
    timings = _current.get()
    if timings is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started) * 1000
