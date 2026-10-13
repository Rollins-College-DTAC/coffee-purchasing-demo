"""Live run: held-import alerts, the live flow chart, and one panel per pipeline step."""

import json
import time
from datetime import datetime

import pandas as pd
import streamlit as st

from dashboard import cloud
from dashboard.data import md, query, source, source_note
from dashboard.flow import build_dot

STEP_TEXT = {
    "detect": "Logs the file name, size and SHA-256 hash; skips content already landed.",
    "land": "Stores every record exactly as it arrived in TinyDB, tagged with run ID, file name and hash.",
    "redact": "Masks emails and phone numbers before any model sees a note.",
    "classify": "The router sends each note to an expert (Jev first) for category, urgency and injection risk.",
    "extract": "Claude pulls freight premium, discount, delay and moisture out of the note into a fixed schema.",
    "load": "Cleans each record (origin, supplier, lbs, $/lb, ISO date) and writes typed rows to SQLite.",
    "decide": "Tests this import's mean $/lb against recent published imports with a z-score.",
    "act": "Publishes the import to the gold views, or holds it and opens an incident for a person.",
    "statistics": "Recomputes 95% confidence intervals by origin, the forecast and its backtest.",
    "warehouse": "Copies the run's rows to Snowflake when it is configured.",
    "review": "A person released or rejected the held import.",
}

def count(v):
    return 0 if pd.isna(v) else int(v)


st.title("Live run")
kind = source_note()


# ---------- Held imports: a person decides ----------

def release_or_reject(run_id, release, name):
    from agent.db import Database
    from agent.review import decide
    return decide(Database(source()[0]), run_id, release, name)


held = query("SELECT i.run_id, i.file_name, i.n_lots, i.mean_price_per_lb, d.statistic, r.detail "
             "FROM imports i LEFT JOIN decision_log d USING (run_id) "
             "LEFT JOIN review_queue r ON r.run_id = i.run_id AND r.reason = 'trigger_hold' "
             "WHERE i.status = 'held' ORDER BY i.loaded_at DESC")
if len(held):
    st.error(f"⚠ {len(held)} import(s) held for review. The agent never releases them on its own.")
    for i, h in enumerate(held.itertuples()):
        z = f"z = {h.statistic:.2f}" if pd.notna(h.statistic) else "no z-score"
        with st.expander(f"⏸️ {h.file_name} — {h.n_lots} lots, mean \\${h.mean_price_per_lb:.2f}/lb, {z}", expanded=i == 0):
            st.markdown(md(h.detail))
            if kind == "local":
                c1, c2, c3 = st.columns([3, 1, 1])
                name = c1.text_input("Your name", key=f"name-{h.run_id}", placeholder="recorded in the logs")
                if c2.button("Release", key=f"rel-{h.run_id}", disabled=not name, type="primary"):
                    st.success(f"{h.file_name}: {release_or_reject(h.run_id, True, name)}")
                    st.cache_data.clear()
                    st.rerun()
                if c3.button("Reject", key=f"rej-{h.run_id}", disabled=not name):
                    st.warning(f"{h.file_name}: {release_or_reject(h.run_id, False, name)}")
                    st.cache_data.clear()
                    st.rerun()
            elif kind == "cloud":
                c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
                name = c1.text_input("Your name", key=f"name-{h.run_id}", placeholder="recorded in the logs")
                code = c2.text_input("Passcode", key=f"code-{h.run_id}", type="password")
                for col, label, release in ((c3, "Release", True), (c4, "Reject", False)):
                    if col.button(label, key=f"{label[:3].lower()}-{h.run_id}", disabled=not (name and code),
                                  type="primary" if release else "secondary"):
                        if not cloud.passcode_ok(code):
                            st.error("Wrong passcode.")
                        elif not cloud.reviewer_ok(name):
                            st.error("Use letters, spaces and . , ' - only (up to 60).")
                        else:
                            url = cloud.request_review(h.run_id, release, name)
                            cloud.pending_reviews.clear()
                            st.success(f"Requested ({url}). The running demo applies it within a minute; "
                                       "if no demo is running, it is applied when the next one starts.")
            else:
                st.caption("Release / Reject works in the local app, and in the cloud app once it has live data.")
    if kind == "cloud":
        for title in cloud.pending_reviews():
            st.info(f"Waiting for the pipeline: {title}")


# ---------- Which run ----------

runs = query("SELECT run_id, file_name, MIN(started_at) AS started FROM run_log "
             "WHERE run_id LIKE 'run-%' AND file_name IS NOT NULL GROUP BY run_id ORDER BY started DESC LIMIT 200")
if runs.empty:
    st.info("No runs yet. Start the agent: `uv run python -m agent.loop`")
    st.stop()
follow = st.toggle("Follow the latest run", value=True)
labels = [f"{r.file_name}  ·  {r.run_id}" for r in runs.itertuples()]
choice = 0 if follow else st.selectbox("Run", range(len(labels)), format_func=lambda i: labels[i])


@st.fragment(run_every=5 if follow else None)
def flow_and_panels():
    run_id = query("SELECT run_id FROM run_log WHERE run_id LIKE 'run-%' ORDER BY rowid DESC LIMIT 1")["run_id"][0] \
        if follow else runs["run_id"][choice]
    log = query("SELECT rowid, * FROM run_log WHERE run_id = ? ORDER BY rowid", (run_id,))
    usage = query("SELECT step, expert, COUNT(*) AS n FROM model_usage_log WHERE run_id = ? GROUP BY step, expert",
                  (run_id,))
    decision = query("SELECT * FROM decision_log WHERE run_id = ?", (run_id,))
    file_name = log["file_name"].dropna().iloc[0] if log["file_name"].notna().any() else ""

    steps = dict(zip(log["step"], log["status"]))
    replay = kind == "cloud" and follow
    if replay:  # cloud data lands once a minute: replay the latest run one step every 2 s
        order = list(steps)
        k = int(time.time() // 2) % (len(order) + 3)
        steps = {st_: (status if i < k else "running" if i == k else "waiting")
                 for i, (st_, status) in enumerate(steps.items())}
    experts = {s: ", ".join(f"{g.expert} ×{g.n}" for g in usage[usage.step == s].itertuples())
               for s in ("classify", "extract")}
    details = {"detect": file_name, "classify": experts["classify"], "extract": experts["extract"]}
    if len(decision) and pd.notna(decision["statistic"][0]):
        details["decide"] = f"|z| {abs(decision['statistic'][0]):.2f} vs {decision['threshold'][0]:g}"
    rows = dict(zip(log["step"], log["rows_out"]))
    edges = {("load", "decide"): int(rows["load"])} if pd.notna(rows.get("load")) else {}
    if pd.notna(rows.get("land")):
        edges[("land", "redact")] = int(rows["land"])

    finished = log["finished_at"].dropna().max()
    ago = ""
    if isinstance(finished, str):
        secs = (datetime.now().astimezone() - datetime.fromisoformat(finished)).total_seconds()
        ago = f" · last step finished {secs:,.0f} s ago"
    st.subheader(f"{file_name}")
    st.caption(f"{run_id}{ago}" + (" · **replay** of the latest run (cloud data arrives once a minute)"
                                    if replay else ""))
    st.graphviz_chart(build_dot(steps, details, edges), width="stretch")

    sql = query("SELECT step, sql_text, params_json, rows_affected, duration_ms FROM sql_log WHERE run_id = ?", (run_id,))
    stats = query("SELECT * FROM stats_log WHERE run_id = ?", (run_id,))
    for r in log.itertuples():
        dur = ""
        if isinstance(r.finished_at, str) and isinstance(r.started_at, str):
            dur = f" · {(datetime.fromisoformat(r.finished_at) - datetime.fromisoformat(r.started_at)).total_seconds():.0f} s"
        icon = {"ok": "✅", "warning": "⚠️", "held": "⏸️", "failed": "❌", "running": "⏳"}.get(r.status, "•")
        with st.expander(f"{icon} {r.step} — {r.status}{dur}", expanded=r.step in ("decide", "act") and r.status == "held"):
            st.markdown(f"*{STEP_TEXT.get(r.step, '')}*  \nFile: `{r.file_name}`")
            st.markdown(md(r.message))
            if pd.notna(r.rows_in):
                st.caption(f"rows in {count(r.rows_in)} · out {count(r.rows_out)} · rejected {count(r.rows_rejected)}")
            if r.step in ("classify", "extract"):
                calls = query("SELECT expert, route_reason, prompt_version, input_tokens, output_tokens, latency_ms, "
                              "confidence, estimated_cost_usd, pii_redacted, outcome FROM model_usage_log "
                              "WHERE run_id = ? AND step = ?", (run_id, r.step))
                st.dataframe(calls, hide_index=True, width="stretch")
                if r.step == "classify":
                    st.latex(r"\mathrm{confidence} = \frac{p_{\max} - 1/n}{1 - 1/n}")
                    st.dataframe(query("SELECT lot_id, note, category, category_confidence, urgency, injection_p, "
                                       "classify_expert, status FROM record_enrichment WHERE run_id = ?", (run_id,)),
                                 hide_index=True, width="stretch")
            for s in stats[stats.step == r.step].itertuples():
                st.markdown(f"**{s.method}**")
                st.latex(s.formula_latex)
                st.code(", ".join(f"{k} = {v}" for k, v in json.loads(s.inputs_json).items()), language=None)
                st.markdown(md(s.interpretation))
            step_sql = sql[sql.step == r.step]
            for q in step_sql.head(12).itertuples():
                c1, c2 = st.columns([3, 2])
                c1.code(q.sql_text, language="sql")
                c2.code(f"params: {q.params_json}\nrows: {q.rows_affected} · {q.duration_ms} ms", language=None)
            if len(step_sql) > 12:
                st.caption(f"…and {len(step_sql) - 12} more statements (Pipeline history page).")


flow_and_panels()
