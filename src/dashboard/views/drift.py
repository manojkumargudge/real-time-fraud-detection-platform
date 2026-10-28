"""Drift reports (src/monitoring): what drifted, whether retraining fired, the Evidently report."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from src.dashboard import fmt, ui


def feature_names(items: Any) -> str:
    if not items:
        return ""
    rows = items if isinstance(items, list) else json.loads(str(items))
    return ", ".join(r["feature"] if isinstance(r, dict) else str(r) for r in rows)


absent = ui.require_schema("drift_reports")
st.title("Drift reports")

reports = [] if "drift_reports" in absent else ui.load("list_drift_reports")
if not reports:
    st.info(
        "No drift reports in this database yet. Docker Compose: python -m "
        "src.monitoring.run_drift. Kubernetes: the drift CronJob runs every 6 hours, or once "
        "now with kubectl -n fraud-k8s create job drift-manual --from=cronjob/drift."
    )
    st.stop()

table = pd.DataFrame(
    [
        {
            "id": r["id"],
            "run_at": fmt.ist(r["run_at"]),
            "window_days": ui.window_label(r["window_start_day"], r["window_end_day"]),
            "drifted_share": r["drifted_feature_share"],
            "top_drifted": feature_names(r["top_drifted_features"])[:80],
            "prediction_psi": r["prediction_drift_score"],
            "retrain": ui.retrain_label(r),
            "retraining": r["promotion_decision"] or "",
            "synthetic": r["synthetic"],
        }
        for r in reports
    ]
)
st.dataframe(ui.labelled(table), hide_index=True)
st.caption(ui.DRIFT_RUNS_NOTE)
st.caption(ui.DRIFT_ROW8_NOTE)

rid = st.selectbox("Report", [r["id"] for r in reports], index=len(reports) - 1)
report = next(r for r in reports if r["id"] == rid)
st.write("**Trigger reasons:** " + ("; ".join(report["trigger_reasons"]) or "none"))
st.write(
    "**Drifted top-gain features:** "
    + (feature_names(report["drifted_top_gain_features"]) or "none")
)
st.write(f"**Retraining / promotion:** {report['promotion_decision'] or 'not run'}")
path = ui.repo().report_file(report["report_path"])
if path is None:
    st.caption(
        "The Evidently HTML report for this row is not in this deployment's reports folder "
        "(it was written by another environment, or removed); the row above is the result."
    )
else:
    with st.expander(f"Evidently report ({path.name})"):
        components.html(path.read_text(encoding="utf-8"), height=900, scrolling=True)
