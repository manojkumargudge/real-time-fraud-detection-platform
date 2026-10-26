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

flash = st.session_state.pop("case_flash", None)
if flash is not None:
    st.success(flash)

m = st.columns(5)
m[0].metric("Fraud score", f"{case['score']:.4f}")
m[1].metric("Threshold", f"{case['threshold']:.4f}")
m[2].metric("Status", case["status"])
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
    st.dataframe(fields, hide_index=True, height=min(38 * len(fields) + 38, 520))
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
        st.info(f"{case['status']} is final: no further changes.")
    else:
        analyst = ui.analyst_name()
        with st.form("change_status"):
            to_status = st.selectbox("New status", options)
            notes = st.text_area("Notes")
            submitted = st.form_submit_button("Save", disabled=not analyst)
        st.caption(
            "Feedback label: confirmed_fraud records 'fraud', false_positive records "
            "'not_fraud', other statuses record no label."
        )
        if not analyst:
            st.caption("Enter your analyst name in the sidebar to change this case.")
        if submitted and analyst:
            try:
                version = ui.repo().change_status(
                    int(alert_id), int(case["version"]), str(to_status), analyst, notes
                )
            except StaleAlertError as exc:
                ui.invalidate()
                st.error(str(exc))
            except (workflow.InvalidTransitionError, AlertNotFoundError, ValueError) as exc:
                st.error(str(exc))
            else:
                ui.invalidate()
                st.session_state["case_flash"] = (
                    f"Saved: {case['status']} -> {to_status} (version {version})."
                )
                st.rerun()

st.subheader("Review history")
history = ui.load("review_history", int(alert_id))
if history:
    st.dataframe(pd.DataFrame(history)[HISTORY_COLUMNS], hide_index=True)
else:
    st.caption("No reviews yet.")
