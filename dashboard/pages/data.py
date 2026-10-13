"""Data: the clean purchase lots, data quality, and lots per import."""

import altair as alt
import streamlit as st

from dashboard.data import query, source_note

st.title("Data")
source_note()

SQL = ("SELECT p.*, i.status AS import_status FROM purchases p JOIN imports i USING (run_id) ORDER BY p.loaded_at DESC")
lots = query(SQL)
if lots.empty:
    st.info("No clean data yet.")
    st.stop()

quality = query("SELECT SUM(rows_in) AS seen, SUM(rows_out) AS loaded, SUM(rows_rejected) AS dropped "
                "FROM run_log WHERE step = 'load'")
quarantined = query("SELECT COUNT(*) AS n FROM record_enrichment WHERE status = 'quarantined'")["n"][0]
dupes = query("SELECT COUNT(*) AS n FROM run_log WHERE step = 'detect' AND status = 'warning'")["n"][0]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Lots loaded", f"{int(quality.loaded[0] or 0):,}")
c2.metric("Rejected by validation", f"{int((quality.dropped[0] or 0) - quarantined):,}")
c3.metric("Quarantined (injection)", f"{quarantined:,}")
c4.metric("Duplicate files skipped", f"{dupes:,}")

f1, f2, f3 = st.columns(3)
origins = f1.multiselect("Origin", sorted(lots.origin.unique()))
statuses = f2.multiselect("Import status", sorted(lots.import_status.unique()))
categories = f3.multiselect("Note category", sorted(lots.category.dropna().unique()))
view = lots
if origins:
    view = view[view.origin.isin(origins)]
if statuses:
    view = view[view.import_status.isin(statuses)]
if categories:
    view = view[view.category.isin(categories)]
st.dataframe(view[["file_name", "lot_id", "origin", "supplier", "quantity_lbs", "price_per_lb", "cupping_score",
                   "harvest_date", "category", "urgency", "freight_premium_usd_per_lb", "discount_pct",
                   "delay_weeks", "note", "import_status"]],
             hide_index=True, width="stretch",
             column_config={"price_per_lb": st.column_config.NumberColumn(format="$%.2f"),
                            "quantity_lbs": st.column_config.NumberColumn(format="%,.0f")})
st.caption(f"{len(view):,} of {len(lots):,} lots")

st.subheader("Lots per import")
per = query("SELECT file_name, loaded_at, n_lots, status FROM imports ORDER BY loaded_at")
chart = alt.Chart(per).mark_bar(color="#2a78d6", cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
    x=alt.X("loaded_at:T", title="import time"), y=alt.Y("n_lots:Q", title="lots loaded"),
    tooltip=["file_name", "n_lots", "status"]).properties(height=200)
st.altair_chart(chart, width="stretch")

with st.expander("How this was calculated"):
    st.code(SQL, language="sql")
    st.caption("Rejected = rows dropped at load minus quarantined injection notes. Duplicates are files whose "
               "SHA-256 matched one already landed.")
