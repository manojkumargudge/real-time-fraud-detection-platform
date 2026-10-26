"""Overview: alerts per status, review precision, production model, alerts per day."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.dashboard import ui
from src.serving.store import ALERT_STATUSES

ui.require_schema()
ov = ui.load("overview")
st.title("Overview")

st.subheader(f"Alerts by status ({ov['total']:,} in total)")
for col, status in zip(st.columns(len(ALERT_STATUSES)), ALERT_STATUSES, strict=True):
    col.metric(ui.status_label(status), f"{ov['by_status'].get(status, 0):,}")

left, right = st.columns(2)
reviewed, fraud = ov["reviewed"], ov["confirmed_fraud"]
if reviewed < ui.MIN_REVIEWS_FOR_PRECISION:
    left.metric(
        "Precision of reviewed alerts",
        f"Too few reviews (n={reviewed})",
        help=(
            f"Shown from {ui.MIN_REVIEWS_FOR_PRECISION} reviewed alerts on: with fewer, one "
            "review moves the percentage by several points, so it would mostly be noise. "
            "Precision = confirmed fraud / reviewed alerts, using each transaction's latest "
            "labelled review."
        ),
    )
else:
    left.metric(
        "Precision of reviewed alerts",
        f"{fraud / reviewed:.1%} ({fraud:,} of {reviewed:,})",
        help="Confirmed fraud / reviewed alerts, using each transaction's latest labelled review.",
    )
model = ov["model_version"]
right.metric(
    "Production model",
    model.rsplit("-", 1)[-1] if model else "n/a",
    help=(
        "Model id = the first 12 hex digits of the model file's SHA-256. The MLflow registry "
        "version is not visible to the dashboard (the registry is a host-only file)."
    ),
)
if model:
    right.caption(f"model_version {model} (newest scored transaction)")

st.subheader("Alerts per day")
per_day = pd.DataFrame(ov["per_day"], columns=["transaction_day", "alerts"])
st.bar_chart(
    per_day,
    x="transaction_day",
    y="alerts",
    x_label="Day (from dataset start)",
    y_label="Alerts",
)
st.caption(
    "IEEE-CIS TransactionDT is seconds from an unspecified start date, so days are relative."
)
