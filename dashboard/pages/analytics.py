"""Analytics: the statistical methods, the forecast and backtest, and how well the trigger works.
Every chart has a 'How this was calculated' expander with its SQL, formula and values."""

import json
import math

import altair as alt
import pandas as pd
import streamlit as st

from agent import settings
from agent.stats import CI_FORMULA, PI_FORMULA, Z_FORMULA, forecast
from dashboard.data import md, query, source_note

BLUE, ORANGE, CRITICAL, MUTED = "#2a78d6", "#eb6834", "#d03b3b", "#8a8985"

st.title("Analytics")
source_note()


def latest(method_like):
    return query("SELECT * FROM stats_log WHERE method LIKE ? AND computed_at = "
                 "(SELECT MAX(computed_at) FROM stats_log WHERE method LIKE ?)", (method_like, method_like))


def how(sql, formula, rows=None, note=None):
    with st.expander("How this was calculated"):
        st.code(sql, language="sql")
        st.latex(formula)
        if rows is not None:
            for r in rows.itertuples():
                st.code(f"{r.method}: " + ", ".join(f"{k} = {v}" for k, v in json.loads(r.inputs_json).items()),
                        language=None)
        if note:
            st.markdown(md(note))


# ---------- 1. Mean $/lb by origin with 95% CIs ----------

st.subheader("Mean price by origin, with 95% confidence intervals")
st.caption("If two origins' intervals don't overlap, their average prices really differ, not just by chance.")
cis = latest("mean_ci_95:%")
if len(cis):
    rows = []
    for r in cis.itertuples():
        i = json.loads(r.inputs_json)
        mean, s, n, t = i[r"\bar{x}"], i["s"], i["n"], i["t_{0.975,n-1}"]
        half = t * s / math.sqrt(n) if t else 0
        rows.append({"origin": r.method.split(":", 1)[1], "mean": mean, "lo": mean - half, "hi": mean + half, "n": n})
    ci = pd.DataFrame(rows).sort_values("mean")
    base = alt.Chart(ci).encode(y=alt.Y("origin:N", sort=list(ci.origin), title=None))
    chart = (base.mark_rule(strokeWidth=2, color=BLUE).encode(x=alt.X("lo:Q", title="$ per lb"), x2="hi:Q")
             + base.mark_circle(size=90, color=BLUE, opacity=1).encode(
                 x="mean:Q", tooltip=["origin", alt.Tooltip("mean:Q", format="$.2f"), alt.Tooltip("lo:Q", format="$.2f"),
                                      alt.Tooltip("hi:Q", format="$.2f"), "n"])
             + base.mark_text(dx=10, align="left", fontSize=11).encode(x="hi:Q", text=alt.Text("mean:Q", format="$.2f"))
             ).properties(height=40 * len(ci) + 30)
    st.altair_chart(chart, width="stretch")
    how("SELECT origin, price_per_lb FROM gold_purchases", CI_FORMULA, cis)
else:
    st.info("No published data yet.")


# ---------- 2. The trigger: z-score of each import ----------

st.subheader("Anomaly trigger: z-score of each import's mean $/lb")
thr = settings.CONFIG["trigger"]["z_threshold"]
st.caption(f"Each dot is one import, compared with the last {settings.CONFIG['trigger']['window']} published imports. "
           f"Beyond ±{thr:g} the import is held for a person.")
Z_SQL = ("SELECT i.file_name, i.loaded_at, i.mean_price_per_lb, i.status, d.statistic, d.fired "
         "FROM decision_log d JOIN imports i USING (run_id) WHERE d.statistic IS NOT NULL ORDER BY i.loaded_at")
z = query(Z_SQL)
if len(z):
    z["decision"] = z.fired.map({1: "held", 0: "published"})
    dots = alt.Chart(z).mark_circle(size=70, opacity=0.9, stroke="white", strokeWidth=1).encode(
        x=alt.X("loaded_at:T", title="import time"),
        y=alt.Y("statistic:Q", title="z"),
        color=alt.Color("decision:N", scale=alt.Scale(domain=["published", "held"], range=[BLUE, CRITICAL]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["file_name", alt.Tooltip("mean_price_per_lb:Q", format="$.2f"), alt.Tooltip("statistic:Q", format=".2f"),
                 "status"],
    )
    lines = alt.Chart(pd.DataFrame({"y": [thr, -thr]})).mark_rule(strokeDash=[4, 4], color=MUTED).encode(y="y:Q")
    st.altair_chart((lines + dots).properties(height=260), width="stretch")
    how(Z_SQL, Z_FORMULA, latest("z_score"))

score = query("SELECT method, result, inputs_json, interpretation FROM stats_log WHERE method IN "
              "('trigger_precision', 'trigger_recall') ORDER BY computed_at DESC LIMIT 2")
if len(score):
    c1, c2 = st.columns(2)
    vals = dict(zip(score.method, score.result))
    c1.metric("Trigger precision", f"{vals.get('trigger_precision', float('nan')):.2f}",
              help="Share of held imports that were real (seeded) anomalies")
    c2.metric("Trigger recall", f"{vals.get('trigger_recall', float('nan')):.2f}",
              help="Share of real anomalies that were held")
    st.caption(md(score.interpretation.iloc[0]) + f" Counts: {score.inputs_json.iloc[0]}")
    how("-- scripts/score_trigger.py: decisions joined to the generator's answer key (generator_log.csv)",
        r"\mathrm{precision} = \frac{TP}{TP + FP}, \qquad \mathrm{recall} = \frac{TP}{TP + FN}")


# ---------- 3. Forecast with a 95% prediction interval ----------

st.subheader("Forecast: mean $/lb of the next imports")
SERIES_SQL = "SELECT file_name, loaded_at, mean_price_per_lb FROM gold_import_stats ORDER BY loaded_at"
series = query(SERIES_SQL)
if len(series) >= 20:
    points, nxt = forecast(list(series.mean_price_per_lb), horizon=5)
    hist = series.assign(import_no=range(1, len(series) + 1), kind="actual")
    fc = pd.DataFrame(points).assign(import_no=lambda d: len(series) + d.h)
    band = alt.Chart(fc).mark_area(opacity=0.25, color=ORANGE).encode(
        x="import_no:Q", y=alt.Y("lo:Q", title="$ per lb"), y2="hi:Q",
        tooltip=[alt.Tooltip("yhat:Q", format="$.2f", title="forecast"), alt.Tooltip("lo:Q", format="$.2f"),
                 alt.Tooltip("hi:Q", format="$.2f")])
    fline = alt.Chart(fc).mark_line(strokeWidth=2, strokeDash=[5, 3], color=ORANGE).encode(x="import_no:Q", y="yhat:Q")
    actual = alt.Chart(hist).mark_line(strokeWidth=2, color=BLUE, point=alt.OverlayMarkDef(size=20)).encode(
        x=alt.X("import_no:Q", title="published import #"),
        y=alt.Y("mean_price_per_lb:Q", title="$ per lb", scale=alt.Scale(zero=False)),
        tooltip=["file_name", alt.Tooltip("mean_price_per_lb:Q", format="$.2f")])
    st.altair_chart((band + fline + actual).properties(height=280), width="stretch")
    st.caption("Blue: published imports. Orange: forecast (dashed) with its 95% prediction interval (shaded).")
    st.markdown(md(nxt["interpretation"]))
    how(SERIES_SQL, PI_FORMULA, pd.DataFrame([{"method": nxt["method"], "inputs_json": json.dumps(nxt["inputs"])}]),
        "Exponential smoothing (statsmodels ETSModel, additive error, additive damped trend), refit on every page load.")

    bt = latest("backtest_mae")
    if len(bt):
        i = json.loads(bt.inputs_json.iloc[0])
        c1, c2, c3 = st.columns(3)
        c1.metric("Forecast MAE", f"${i['MAE_{ETS}']:.2f}/lb")
        c2.metric("Naive MAE ('same as last import')", f"${i['MAE_{naive}']:.2f}/lb")
        c3.metric("Forecast MAPE", f"{i['MAPE_{ETS}']:.1f}%")
        st.markdown(md(bt.interpretation.iloc[0]))
        how(SERIES_SQL + "  -- last 10 held out", r"\mathrm{MAE} = \frac{1}{n}\sum_t \lvert y_t - \hat{y}_t \rvert, \qquad "
            r"\mathrm{MAPE} = \frac{100}{n}\sum_t \left\lvert \frac{y_t - \hat{y}_t}{y_t} \right\rvert", bt)
else:
    st.info(f"The forecast needs 20 published imports; there are {len(series)}.")
