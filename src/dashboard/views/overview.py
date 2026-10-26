"""Overview: alert counts by status, alerts per transaction day, review precision, model."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import ui
from src.serving.store import ALERT_STATUSES

ui.require_schema()
ov = ui.load("overview")
st.title("Overview")

cols = st.columns(4)
cols[0].metric("Alerts", f"{ov['total']:,}")
cols[1].metric("Reviewed (labelled)", f"{ov['reviewed']:,}")
precision = ov["precision"]
cols[2].metric(
    "Precision of reviewed alerts",
    "n/a" if precision is None else f"{precision:.1%}",
    help="Confirmed fraud / reviewed alerts, using each transaction's latest labelled review.",
)
cols[3].metric("Production model (newest scored row)", ov["model_version"] or "n/a")

left, right = st.columns(2)
with left:
    st.subheader("Alerts by status")
    by_status = pd.DataFrame(
        {
            "status": list(ALERT_STATUSES),
            "alerts": [ov["by_status"].get(s, 0) for s in ALERT_STATUSES],
        }
    )
    st.bar_chart(by_status, x="status", y="alerts", horizontal=True)
    st.dataframe(by_status, hide_index=True)
with right:
    st.subheader("Alerts per transaction day")
    per_day = pd.DataFrame(ov["per_day"], columns=["transaction_day", "alerts"])
    st.bar_chart(per_day, x="transaction_day", y="alerts")
    st.caption(
        "Transaction day = TransactionDT // 86400 (event time). Alerts were created when the "
        "validation period was replayed, so creation dates would put them all on two days."
    )
