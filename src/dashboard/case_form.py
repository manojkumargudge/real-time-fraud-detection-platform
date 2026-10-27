"""The case page's status form: one-time submit tokens and the save (docs/dashboard.md).

A token is created when the form is first rendered for an alert. It records the alert id, version
and status the analyst was looking at, names the form's widgets and is carried by the Save
callback. The database stores it with the review (UNIQUE), so one submit writes at most once.
After a save (or a rejected one), or when the alert id changes, the token is discarded with the
form's contents: the next render gets a new token and an empty form, and a submit that still
carries an old token is rejected (docs/incidents/2026-10-26-unintended-reviews.md).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from src.dashboard import workflow
from src.dashboard.data import AlertNotFoundError, DuplicateSubmitError, StaleAlertError

logger = logging.getLogger(__name__)
State = MutableMapping[Any, Any]  # st.session_state, or a dict in tests

TOKENS = "case_form_tokens"  # session key: {token: FormToken}
FIELDS = ("case_to_status", "case_notes")  # widget keys are f"{field}_{token}"
KEEP_USED = 50  # used tokens remembered per session, for a clear "no longer current" message


@dataclass(frozen=True)
class FormToken:
    token: str
    alert_id: int
    version: int  # the alert version the form was rendered with
    status: str
    used: bool = False


class Saver(Protocol):
    def change_status(
        self,
        alert_id: int,
        expected_version: int,
        to_status: str,
        analyst: str,
        notes: str,
        *,
        submit_token: str,
    ) -> int: ...


def widget_key(field: str, token: str) -> str:
    return f"{field}_{token}"


def _tokens(state: State) -> dict[str, FormToken]:
    if TOKENS not in state:
        state[TOKENS] = {}
    tokens: dict[str, FormToken] = state[TOKENS]
    return tokens


def form_token(state: State, alert_id: int, version: int, status: str) -> FormToken:
    """The live token of this alert's form, or a new one (an empty form) if there is none.

    A live token keeps the version it was created with even if the case has changed since, so a
    save is rejected rather than checked against a version the analyst may not have seen. Live
    tokens of other alerts are discarded together with their form contents.
    """
    tokens = _tokens(state)
    for other in [t for t in tokens.values() if not t.used and t.alert_id != alert_id]:
        discard(state, other.token)
    live = next((t for t in tokens.values() if not t.used and t.alert_id == alert_id), None)
    if live is None:
        live = FormToken(uuid.uuid4().hex, alert_id, version, status)
        tokens[live.token] = live
    return live


def discard(state: State, token: str) -> None:
    """Retire a token and clear its form; the next render starts an empty form."""
    tokens = _tokens(state)
    if token in tokens:
        tokens[token] = replace(tokens[token], used=True)
    for field in FIELDS:
        state.pop(widget_key(field, token), None)
    used = [k for k, t in tokens.items() if t.used]
    for k in used[:-KEEP_USED]:
        del tokens[k]


def disabled_reason(analyst: str, to_status: str | None) -> str | None:
    """Why Save is disabled (shown under the button), or None when it is enabled."""
    if not analyst:
        return "Enter your analyst name in the sidebar."
    if not to_status:
        return "Choose a new status."
    return None


def save(state: State, repo: Saver, token: str, analyst: str) -> tuple[str, str]:
    """The Save callback: ("success" | "error", message). Writes at most once per token."""
    seen = _tokens(state).get(token)
    to_status = state.get(widget_key("case_to_status", token))
    notes = str(state.get(widget_key("case_notes", token)) or "")
    new_version: int | None = None
    if seen is None or seen.used:
        result = (
            "error",
            "This form is no longer current (it was saved, or the alert or case changed); "
            "nothing was saved. Reload the case.",
        )
    elif not to_status:
        result = ("error", "Choose a new status; nothing was saved.")
    elif not analyst:
        result = ("error", "Enter your analyst name in the sidebar; nothing was saved.")
    else:
        try:
            new_version = repo.change_status(
                seen.alert_id, seen.version, str(to_status), analyst, notes, submit_token=token
            )
        except (StaleAlertError, DuplicateSubmitError) as exc:
            discard(state, token)  # the case moved on: start again from its current state
            result = ("error", str(exc))
        except (workflow.InvalidTransitionError, AlertNotFoundError, ValueError) as exc:
            result = ("error", str(exc))
        else:
            discard(state, token)
            result = ("success", f"Saved: {seen.status} -> {to_status} (version {new_version}).")
    logger.log(
        logging.INFO if result[0] == "success" else logging.WARNING,
        "case save %s: alert=%s from=%s to=%s analyst=%r token=%s version=%s->%s%s",
        "ok" if result[0] == "success" else "rejected",
        seen.alert_id if seen else None,
        seen.status if seen else None,
        to_status,
        analyst,
        token,
        seen.version if seen else None,
        new_version,
        "" if result[0] == "success" else f" reason={result[1]!r}",
    )
    return result
