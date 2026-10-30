"""Display formatting for the analyst dashboard: pure functions, no Streamlit, no database.

One place for how values look on every page: times in IST, IDs and codes without a trailing
".0", "missing" for blank transaction fields, "—" for no review label, amounts in dollars.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

# India Standard Time has no daylight saving, so a fixed offset is exact (and needs no tzdata in
# the slim image).
IST = timezone(timedelta(hours=5, minutes=30), "IST")
MISSING = "missing"
NO_LABEL = "—"


def ist(when: datetime | None, seconds: bool = False) -> str:
    """``when`` in IST, e.g. ``2026-10-26 16:45 IST``; naive datetimes are taken as UTC."""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    pattern = "%Y-%m-%d %H:%M:%S IST" if seconds else "%Y-%m-%d %H:%M IST"
    return when.astimezone(IST).strftime(pattern)


def value(v: Any) -> str:
    """A transaction field for display: whole numbers without ".0", blanks as "missing"."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return MISSING
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return MISSING
        if v.is_integer():
            return str(int(v))
        return repr(v)
    return str(v)


def feature_value(v: Any) -> str:
    """A model input for display: like ``value``, but fractions rounded to 4 decimals."""
    if isinstance(v, float) and not math.isnan(v) and not v.is_integer():
        return f"{v:.4f}"
    return value(v)


def amount(v: Any) -> str:
    """A transaction amount in dollars with 2 decimals, e.g. ``$994.00``."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return MISSING
    return f"${float(v):,.2f}"


def label(v: Any) -> str:
    """A review label for display: ``fraud`` / ``not_fraud``, or "—" when the review has none
    (None, or NaN once pandas has seen the column)."""
    return v if isinstance(v, str) and v else NO_LABEL
