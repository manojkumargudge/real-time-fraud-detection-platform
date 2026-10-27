"""Alert queue: filters (status, score, transaction day), highest score first, paginated."""

from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from src.dashboard import ui
from src.dashboard.data import AlertFilter
from src.serving.store import ALERT_STATUSES

PAGE_SIZE = 50
COLUMNS = [
    "alert_id",
    "score",
    "status",
    "transaction_day",
    "amount",
    "product",
    "card_id",
    "transaction_id",
]
# Whole numbers without separators or ".0"; amount in dollars; tooltips for anonymised codes.
COLUMN_CONFIG = {
    "Alert": st.column_config.NumberColumn(format="%d"),
    "Score": st.column_config.NumberColumn(format="%.4f"),
    "Day": st.column_config.NumberColumn(format="%d"),
    "Amount": st.column_config.NumberColumn(format="$%.2f"),
    "Product": st.column_config.TextColumn(help=ui.PRODUCT_HELP),
    "Card": st.column_config.TextColumn(help=ui.CARD_HELP),
    "Transaction": st.column_config.NumberColumn(format="%d"),
}

ui.require_schema()
st.title("Alert queue")

f1, f2, f3, f4 = st.columns([2, 2, 1, 1])
statuses = f1.multiselect(
    "Status", ALERT_STATUSES, default=["open", "investigating"], format_func=ui.status_label
)
low, high = f2.slider("Score", 0.0, 1.0, (0.0, 1.0), step=0.01)
day_from = f3.number_input("From day", min_value=0, value=None, step=1)
day_to = f4.number_input("To day", min_value=0, value=None, step=1)
filt = AlertFilter(
    statuses=tuple(statuses),
    min_score=float(low),
    max_score=float(high),
    day_from=None if day_from is None else int(day_from),
    day_to=None if day_to is None else int(day_to),
)
if st.session_state.get("queue_filter", filt) != filt:
    st.session_state["queue_page"] = 1  # a new filter starts at the first page
st.session_state["queue_filter"] = filt
page_no = int(st.session_state.get("queue_page", 1))

page = ui.load("list_alerts", filt, page_no, PAGE_SIZE)
pages = max(1, math.ceil(page.total / PAGE_SIZE))
st.caption(f"{page.total:,} alerts match; page {page.page} of {pages}, highest score first.")

table = pd.DataFrame(page.rows, columns=COLUMNS)
shown = table.assign(status=table["status"].map(ui.status_label))
event = st.dataframe(
    ui.labelled(shown),
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config=COLUMN_CONFIG,
    key="queue_table",
)
st.caption(f"Card: {ui.CARD_HELP} Product: {ui.PRODUCT_HELP}")
selected = event.selection.rows if event is not None else []
if selected:
    ui.open_case(int(table.iloc[selected[0]]["alert_id"]))

n1, n2, n3 = st.columns([1, 1, 4])
if n1.button("Previous", disabled=page.page <= 1):
    st.session_state["queue_page"] = page.page - 1
    st.rerun()
if n2.button("Next", disabled=page.page >= pages):
    st.session_state["queue_page"] = page.page + 1
    st.rerun()
with n3.form("open_by_id", border=False):
    c1, c2 = st.columns([3, 1])
    alert_id = c1.number_input("Open alert id", min_value=1, value=None, step=1)
    if c2.form_submit_button("Open case") and alert_id is not None:
        ui.open_case(int(alert_id))
