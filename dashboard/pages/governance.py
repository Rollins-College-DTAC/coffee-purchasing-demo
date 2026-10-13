"""Governance: gold-set scores, the review queue, incidents, spend against the budget, kill switch."""

import json
import os
from datetime import datetime, timezone

import streamlit as st

from agent import settings
from dashboard.data import md, query, source_note

st.title("Governance")
source_note()
cfg = settings.CONFIG

kill = cfg["budget"]["kill_switch"] or os.environ.get("KILL_SWITCH") == "1"
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
spent = query("SELECT COALESCE(SUM(estimated_cost_usd), 0) AS usd FROM model_usage_log WHERE called_at >= ?",
              (today,))["usd"][0]
total = query("SELECT COALESCE(SUM(estimated_cost_usd), 0) AS usd FROM model_usage_log")["usd"][0]
c1, c2, c3 = st.columns(3)
c1.metric("Kill switch", "ON: rules only" if kill else "off", help="config.toml [budget] kill_switch or KILL_SWITCH=1")
c2.metric("Model spend today (UTC)", f"${spent:.3f}", help=f"Daily cap ${cfg['budget']['daily_usd']:.2f}; "
          "over it, every task goes to rules")
c3.metric("Model spend, all runs", f"${total:.3f}")
st.progress(min(spent / cfg["budget"]["daily_usd"], 1.0), text=f"{spent / cfg['budget']['daily_usd']:.0%} of the daily cap")

st.subheader("Gold set: each expert against 50 labeled notes")
gold = query("SELECT method, result, threshold, inputs_json FROM stats_log WHERE step = 'gold_set' AND run_id = "
             "(SELECT run_id FROM stats_log WHERE step = 'gold_set' ORDER BY computed_at DESC LIMIT 1)")
if len(gold):
    gold["pass"] = gold.apply(lambda r: "" if r.threshold is None or r.threshold != r.threshold else
                              ("✅" if (r.result <= r.threshold if "invented" in r.method else r.result >= r.threshold)
                               else "❌"), axis=1)
    st.dataframe(gold[["method", "result", "threshold", "pass"]], hide_index=True, width="stretch",
                 column_config={"result": st.column_config.NumberColumn(format="%.3f")})
    v = json.loads(gold.inputs_json.iloc[0])
    st.caption(f"Versions: {v.get('jev_questions')}, {v.get('extract')}, Jev {v.get('jev_model')}, "
               f"Claude {v.get('claude_model')}. Labels were written by Claude and are awaiting human review.")
else:
    st.info("Gold set not scored in this database yet: `uv run python -m scripts.score_gold`")

st.subheader("Review queue")
st.caption("Held imports and uncertain records wait here for a person. The agent never closes them itself.")
queue = query("SELECT status, reason, file_name, lot_id, detail, decided_by, opened_at, closed_at "
              "FROM review_queue ORDER BY opened_at DESC")
st.dataframe(queue, hide_index=True, width="stretch")

st.subheader("Incidents")
inc = query("SELECT kind, status, detail, decided_by, opened_at, closed_at FROM incident_log ORDER BY opened_at DESC")
by_kind = inc.groupby(["kind", "status"]).size().reset_index(name="n")
st.dataframe(by_kind, hide_index=True)
st.dataframe(inc, hide_index=True, width="stretch")

st.subheader("Rules this agent follows")
st.markdown(md("""
- Simulated data only; notes are redacted (emails, phone numbers) before any model call.
- Text inside records is data, never instructions: injection attempts are quarantined and logged as incidents.
- The agent may hold an import, but only a person can release or reject it.
- Every step logs its file name, SQL, model and formula; this dashboard reads the logs read-only.
- Model spend is capped per day; the kill switch sends every task to deterministic rules.
"""))
