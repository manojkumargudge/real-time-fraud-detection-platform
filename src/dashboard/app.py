"""Analyst dashboard entry point (docs/dashboard.md).

Run (repo root): ``PYTHONPATH=. DATABASE_URL=... streamlit run src/dashboard/app.py``; in Compose,
the ``dashboard`` service on port 8501.
"""

from __future__ import annotations

import streamlit as st

from src.dashboard import ui

st.set_page_config(page_title="Fraud analyst dashboard", layout="wide")
ui.sidebar()
st.navigation(
    [
        st.Page("views/overview.py", title="Overview", default=True),
        st.Page("views/alerts.py", title="Alert queue"),
        st.Page("views/case.py", title="Case detail"),
        st.Page("views/rings.py", title="Fraud rings"),
        st.Page("views/drift.py", title="Drift"),
    ]
).run()
