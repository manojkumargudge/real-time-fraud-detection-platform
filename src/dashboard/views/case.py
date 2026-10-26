"""Case detail: transaction, score, model, linked ring, status change, review history."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import ui, workflow
from src.dashboard.data import AlertNotFoundError, StaleAlertError

HISTORY_COLUMNS = [
    "created_at",
    "analyst",
    "from_status",
    "to_status",
    "label",
    "notes",
    "alert_version",
]

FORM_SEEN = "case_form_seen"  # the alert version/status the status form was last rendered with


def save_status(alert_id: int) -> None:
    """Form callback: apply the analyst's choice against the version they were looking at."""
    seen = st.session_state.get(FORM_SEEN)
    if not seen or seen["alert_id"] != alert_id:
        st.session_state["case_message"] = ("error", "The form is out of date; reload the case.")
        return
    to_status = str(st.session_state.get("case_to_status"))
    try:
        version = ui.repo().change_status(
            alert_id,
            seen["version"],
            to_status,
            ui.analyst_name(),
            st.session_state.get("case_notes", ""),
        )
    except StaleAlertError as exc:
        st.session_state["case_message"] = ("error", str(exc))
    except (workflow.InvalidTransitionError, AlertNotFoundError, ValueError) as exc:
        st.session_state["case_message"] = ("error", str(exc))
    else:
        st.session_state["case_message"] = (
            "success",
            f"Saved: {seen['status']} -> {to_status} (version {version}).",
        )
        st.session_state["case_notes"] = ""
    ui.invalidate()  # re-read the case either way: after a conflict it shows the new state


ui.require_schema()
st.title("Case detail")

default_id = st.session_state.get("alert_id")
alert_id = st.number_input("Alert id", min_value=1, value=default_id, step=1)
if alert_id is None:
    st.info("Pick an alert in the queue, or enter an alert id.")
    st.stop()
st.session_state["alert_id"] = int(alert_id)

case = ui.load("get_case", int(alert_id))
if case is None:
    st.warning(f"Alert {alert_id} does not exist.")
    st.stop()

message = st.session_state.pop("case_message", None)
if message is not None:
    kind, text = message
    (st.success if kind == "success" else st.error)(text)

m = st.columns(5)
m[0].metric("Fraud score", f"{case['score']:.4f}")
m[1].metric("Threshold", f"{case['threshold']:.4f}")
m[2].metric("Status", ui.status_label(case["status"]))
m[3].metric("Version", case["version"])
m[4].metric("Model version", case["model_version"])
st.caption(
    f"Transaction {case['transaction_id']}, transaction day {case['transaction_day']}; alert "
    f"created {case['created_at']:%Y-%m-%d %H:%M}, updated {case['updated_at']:%Y-%m-%d %H:%M}."
)

left, right = st.columns([3, 2])
with left:
    st.subheader("Transaction")
    fields = pd.DataFrame(
        [(k, "" if v is None else str(v)) for k, v in case["event"].items()],
        columns=["field", "value"],
    )
    st.dataframe(ui.labelled(fields), hide_index=True, height=min(38 * len(fields) + 38, 520))
    st.caption(f"card_id {case['card_id'] or '-'}; uid {case['uid'] or '-'}")
    ring = ui.load("ring_for_uid", case["uid"])
    if ring is not None:
        st.subheader("Linked fraud ring")
        st.write(
            f"Ring #{ring['rank']} (id {ring['ring_id']}): {ring['uid_count']} users, "
            f"{ring['card_count']} cards, {ring['tx_count']} transactions, labelled fraud rate "
            f"{ring['fraud_rate']:.1%}; shared devices: " + ", ".join(ring["shared_devices"])
        )

with right:
    st.subheader("Change status")
    options = workflow.next_statuses(case["status"])
    if not options:
        st.info(f"{ui.status_label(case['status'])} is final: no further changes.")
    else:
        analyst = ui.analyst_name()
        with st.form("change_status"):
            st.selectbox("New status", options, key="case_to_status", format_func=ui.status_label)
            st.text_area("Notes", key="case_notes")
            # The save runs in the callback, before the page re-reads the case: it must use the
            # version and status this form was rendered with (FORM_SEEN), not a fresh read.
            st.form_submit_button(
                "Save", disabled=not analyst, on_click=save_status, args=(int(alert_id),)
            )
        st.session_state[FORM_SEEN] = {
            "alert_id": int(alert_id),
            "version": int(case["version"]),
            "status": case["status"],
        }
        st.caption(
            "Feedback label: confirmed_fraud records 'fraud', false_positive records "
            "'not_fraud', other statuses record no label."
        )
        if not analyst:
            st.caption("Enter your analyst name in the sidebar to change this case.")

st.subheader("Review history")
history = ui.load("review_history", int(alert_id))
if history:
    st.dataframe(ui.labelled(pd.DataFrame(history)[HISTORY_COLUMNS]), hide_index=True)
else:
    st.caption("No reviews yet.")
