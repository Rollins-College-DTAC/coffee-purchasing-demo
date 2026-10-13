"""Pipeline history: step durations and rows over recent runs, and every log row for one run."""

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.data import query, source_note

st.title("Pipeline history")
source_note()

SQL = ("SELECT run_id, file_name, step, status, started_at, finished_at, rows_in, rows_out, rows_rejected "
       "FROM run_log WHERE run_id IN (SELECT DISTINCT run_id FROM run_log WHERE run_id LIKE 'run-%' "
       "ORDER BY run_id DESC LIMIT 30) ORDER BY rowid")
log = query(SQL)
if log.empty:
    st.info("No runs yet.")
    st.stop()
log["seconds"] = (pd.to_datetime(log.finished_at) - pd.to_datetime(log.started_at)).dt.total_seconds()
order = list(dict.fromkeys(log.step))

st.subheader("Step durations, last 30 runs")
st.caption("Classify and extract take longest: they call the models. Cache hits make them fast.")
dur = alt.Chart(log).mark_rect(stroke="white", strokeWidth=2).encode(
    x=alt.X("file_name:N", title=None, axis=alt.Axis(labelAngle=-60, labelFontSize=9)),
    y=alt.Y("step:N", sort=order, title=None),
    color=alt.Color("seconds:Q", scale=alt.Scale(range=["#cde2fb", "#1c5cab"]), title="seconds"),
    tooltip=["file_name", "step", "status", alt.Tooltip("seconds:Q", format=".1f"), "rows_in", "rows_out"],
).properties(height=300)
st.altair_chart(dur, width="stretch")

st.subheader("Rows in and out per run")
rows = log[log.step == "load"][["file_name", "rows_in", "rows_out", "rows_rejected"]]
st.dataframe(rows, hide_index=True, width="stretch")

st.subheader("Every log row for one run")
runs = list(dict.fromkeys(log.run_id))[::-1]
run_id = st.selectbox("Run", runs, format_func=lambda r: f"{log[log.run_id == r].file_name.iloc[0]} · {r}")
for table in ("run_log", "sql_log", "model_usage_log", "stats_log", "decision_log", "incident_log"):
    df = query(f"SELECT * FROM {table} WHERE run_id = ?", (run_id,))
    with st.expander(f"{table} ({len(df)} rows)"):
        st.dataframe(df, hide_index=True, width="stretch")

with st.expander("How this was calculated"):
    st.code(SQL, language="sql")
    st.caption("Duration = finished_at − started_at for each run_log step.")
