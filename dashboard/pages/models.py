"""Models (MoE): which expert handled which task, when, why, and at what cost."""

import altair as alt
import streamlit as st

from agent.router import EXPERTS
from dashboard.data import query, source_note

TASK_COLORS = alt.Scale(domain=["classify", "extract", "explain"], range=["#2a78d6", "#eb6834", "#1baf7a"])

st.title("Models (mixture of experts)")
source_note()
st.markdown("The router sends each task to the expert best suited to it and logs why. "
            "Every call is one row in `model_usage_log`, including cache hits and rules.")
st.dataframe([{"expert": k, "role": v} for k, v in EXPERTS.items()], hide_index=True, width="stretch")

SQL = ("SELECT called_at, run_id, task, expert, route_reason, prompt_version, input_tokens, output_tokens, "
       "latency_ms, confidence, estimated_cost_usd, pii_redacted, outcome, "
       "CASE WHEN input_tokens > 0 THEN 'live call' WHEN expert = 'rules' THEN 'rules' ELSE 'cache hit' END AS served "
       "FROM model_usage_log WHERE run_id LIKE 'run-%' ORDER BY called_at")
calls = query(SQL)
if calls.empty:
    st.info("No model calls yet.")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
live = calls[calls.served == "live call"]
c1.metric("Model calls", f"{len(calls):,}")
c2.metric("Live calls", f"{len(live):,}")
c3.metric("Served from cache", f"{(calls.served == 'cache hit').mean():.0%}")
c4.metric("Estimated cost", f"${calls.estimated_cost_usd.sum():.3f}")

st.subheader("Who handled what, and when")
st.caption("Each dot is one call. Hover for the route reason.")
timeline = alt.Chart(calls).mark_circle(size=60, opacity=0.8, stroke="white", strokeWidth=1).encode(
    x=alt.X("called_at:T", title="time (your timezone)"),
    y=alt.Y("expert:N", title=None),
    color=alt.Color("task:N", scale=TASK_COLORS, legend=alt.Legend(orient="top", title=None)),
    shape=alt.Shape("served:N", legend=alt.Legend(orient="top", title=None)),
    tooltip=["called_at:T", "task", "expert", "route_reason", "served", "latency_ms", "estimated_cost_usd"],
).properties(height=220)
st.altair_chart(timeline, width="stretch")

st.subheader("Task → expert")
flow = (calls.groupby(["task", "expert", "served"]).size().reset_index(name="calls"))
st.dataframe(flow, hide_index=True, width="stretch")

st.subheader("Per expert: latency, confidence and cost (live calls)")
per = (live.groupby("expert").agg(calls=("expert", "size"), median_latency_ms=("latency_ms", "median"),
                                  mean_confidence=("confidence", "mean"), cost_usd=("estimated_cost_usd", "sum"),
                                  tokens_in=("input_tokens", "sum"), tokens_out=("output_tokens", "sum"))
       .reset_index())
st.dataframe(per, hide_index=True, width="stretch",
             column_config={"cost_usd": st.column_config.NumberColumn(format="$%.4f"),
                            "mean_confidence": st.column_config.NumberColumn(format="%.2f")})
latency = alt.Chart(live).mark_boxplot(size=24, color="#2a78d6").encode(
    x=alt.X("latency_ms:Q", title="latency (ms)", scale=alt.Scale(type="log")),
    y=alt.Y("expert:N", title=None),
    tooltip=["expert", "latency_ms"],
).properties(height=160)
st.altair_chart(latency, width="stretch")

st.subheader("Why the router chose each expert")
st.dataframe(calls.groupby(["expert", "route_reason"]).size().reset_index(name="calls")
             .sort_values("calls", ascending=False), hide_index=True, width="stretch")

with st.expander("How this was calculated"):
    st.code(SQL, language="sql")
    st.latex(r"\mathrm{cost} = \frac{\mathrm{tokens_{in}} \cdot p_{in} + \mathrm{tokens_{out}} \cdot p_{out}}{10^6}")
    st.caption("Prices per million tokens come from `config.toml`. Cache hits and rules cost nothing.")
