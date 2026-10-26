"""Fraud rings (batch graph analysis, src/graph/rings.py): list and ring detail."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import ui
from src.dashboard.data import card_of_uid

LIST_COLUMNS = [
    "rank",
    "uid_count",
    "card_count",
    "tx_count",
    "fraud_rate",
    "mean_risk",
    "scored_tx",
    "shared_devices",
]

ui.require_schema()
st.title("Fraud rings")
st.info(
    "Rings come from the batch graph analysis of the training-period data (users linked by "
    "shared devices). Live alerts are on the later validation period, so few of them link to "
    "a ring member."
)

rings = ui.load("list_rings")
if not rings:
    st.caption("No rings stored (python -m src.graph.rings).")
    st.stop()
table = pd.DataFrame(rings)
table["shared_devices"] = table["shared_devices"].map(", ".join)
st.dataframe(
    table[LIST_COLUMNS],
    hide_index=True,
    column_config={"fraud_rate": st.column_config.NumberColumn(format="percent")},
)

rank = st.selectbox("Ring detail", [r["rank"] for r in rings], format_func=lambda r: f"#{r}")
ring = ui.load("get_ring", int(rank))
if ring is not None:
    uids = list(ring["uids"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Users", ring["uid_count"])
    c2.metric("Cards", ring["card_count"])
    c3.metric("Labelled fraud rate", f"{ring['fraud_rate']:.1%}")
    st.write("Shared devices: " + ", ".join(ring["shared_devices"]))
    st.dataframe(
        pd.DataFrame({"uid": uids, "card": [card_of_uid(u) for u in uids]}), hide_index=True
    )
    alerts = ui.load("ring_alerts", uids)
    st.subheader(f"Alerts on ring members ({len(alerts)})")
    if alerts:
        st.dataframe(pd.DataFrame(alerts), hide_index=True)
