"""Alert case workflow: the allowed status transitions and the feedback label (pure functions).

::

    open            -> investigating | confirmed_fraud | false_positive
    investigating   -> confirmed_fraud | false_positive
    confirmed_fraud -> false_positive | closed      (correction allowed)
    false_positive  -> confirmed_fraud | closed      (correction allowed)
    closed          -> (terminal)

The label of a review follows from its target status (``confirmed_fraud`` -> ``fraud``,
``false_positive`` -> ``not_fraud``, anything else -> no label), so an analyst cannot record a
label that contradicts the decision. A correction is a new review with the new label; the latest
labelled review of a transaction is its label (``data.latest_labels``).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from src.serving.store import ALERT_STATUSES

Label = Literal["fraud", "not_fraud"]

TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "open": frozenset({"investigating", "confirmed_fraud", "false_positive"}),
        "investigating": frozenset({"confirmed_fraud", "false_positive"}),
        "confirmed_fraud": frozenset({"false_positive", "closed"}),
        "false_positive": frozenset({"confirmed_fraud", "closed"}),
        "closed": frozenset(),
    }
)
LABELS: Mapping[str, Label] = MappingProxyType(
    {"confirmed_fraud": "fraud", "false_positive": "not_fraud"}
)

if set(TRANSITIONS) != set(ALERT_STATUSES):  # the database CHECK and the workflow must agree
    raise RuntimeError("TRANSITIONS and store.ALERT_STATUSES list different statuses")


class InvalidTransitionError(ValueError):
    """A status change the workflow does not allow (or an unknown status)."""


def next_statuses(status: str) -> tuple[str, ...]:
    """Statuses reachable from ``status``, in workflow order."""
    _known(status)
    return tuple(s for s in ALERT_STATUSES if s in TRANSITIONS[status])


def label_for(to_status: str) -> Label | None:
    _known(to_status)
    return LABELS.get(to_status)


def validate_transition(from_status: str, to_status: str) -> Label | None:
    """The review label for ``from_status -> to_status``; raises if the move is not allowed."""
    _known(from_status)
    _known(to_status)
    if to_status not in TRANSITIONS[from_status]:
        allowed = ", ".join(next_statuses(from_status)) or "none (terminal)"
        raise InvalidTransitionError(
            f"cannot move an alert from {from_status} to {to_status}; allowed: {allowed}"
        )
    return LABELS.get(to_status)


def _known(status: str) -> None:
    if status not in TRANSITIONS:
        raise InvalidTransitionError(f"unknown alert status {status!r}")
