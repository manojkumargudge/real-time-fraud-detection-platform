"""Case detail: transaction, score, model, linked ring, status change, review history."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import case_form, ui, workflow

HISTORY_COLUMNS = [
    "created_at",
    "analyst",
    "from_status",
    "to_status",
    "label",
    "notes",
    "alert_version",
]


def save_status(token: str) -> None:
    """Save callback: runs before the page re-reads the case, with the form's own token (the
    alert, version and status it was rendered with); see src/dashboard/case_form.py."""
    st.session_state["case_message"] = case_form.save(
        st.session_state, ui.repo(), token, ui.analyst_name()
    )
    ui.invalidate()  # re-read the case either way: after a conflict it shows the new state


def reload_case(token: str) -> None:
    case_form.discard(st.session_state, token)
    ui.invalidate()


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
        form = case_form.form_token(
            st.session_state, int(alert_id), int(case["version"]), case["status"]
        )
        if form.version != case["version"]:
            st.warning(
                f"This case changed since this form was opened (you had version {form.version}, "
                f"it is now version {case['version']}). A save will be rejected: reload the case."
            )
            st.button(
                "Reload the case",
                key=f"case_reload_{form.token}",
                on_click=reload_case,
                args=(form.token,),
            )
        # No st.form: Enter (and Ctrl+Enter in Notes) cannot submit, and Save is enabled as soon
        # as a status is chosen. The widgets are keyed by the form's one-time token, so a new
        # token (after a save, or another alert) starts an empty form.
        to_status = st.selectbox(
            "New status",
            options,
            index=None,
            placeholder="Choose a status",
            key=case_form.widget_key("case_to_status", form.token),
            format_func=ui.status_label,
        )
        st.text_area("Notes", key=case_form.widget_key("case_notes", form.token))
        reason = case_form.disabled_reason(analyst, to_status)
        st.button(
            "Save",
            key=f"case_save_{form.token}",
            disabled=reason is not None,
            on_click=save_status,
            args=(form.token,),
        )
        if reason is not None:
            st.caption(f"Save is disabled. {reason}")
        st.caption(
            "Feedback label: confirmed_fraud records 'fraud', false_positive records "
            "'not_fraud', other statuses record no label."
        )

st.subheader("Review history")
history = ui.load("review_history", int(alert_id))
if history:
    st.dataframe(ui.labelled(pd.DataFrame(history)[HISTORY_COLUMNS]), hide_index=True)
else:
    st.caption("No reviews yet.")
