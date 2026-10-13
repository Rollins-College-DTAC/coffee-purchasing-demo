"""Read-only data access for the dashboard. Only the agent writes the database.

Source order: DASHBOARD_DB env var, then the local agent's data/analytics.sqlite,
then the committed snapshot demo/analytics.sqlite (what Streamlit Cloud shows until Milestone 6).
"""

import os
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from agent import settings

SNAPSHOT = settings.ROOT / "demo" / "analytics.sqlite"


def source():
    """(path, kind) where kind is 'local' (live agent database) or 'snapshot'."""
    if os.environ.get("DASHBOARD_DB"):
        return Path(os.environ["DASHBOARD_DB"]), os.environ.get("DASHBOARD_KIND", "local")
    if settings.SQLITE_PATH.exists():
        return settings.SQLITE_PATH, "local"
    return SNAPSHOT, "snapshot"


@st.cache_data(ttl=3, show_spinner=False)
def _query(path, sql, params):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def query(sql, params=()):
    """Run a read-only query and return a DataFrame. Cached 3 s (per database) so live pages stay fresh."""
    return _query(str(source()[0]), sql, tuple(params))


def md(text):
    """Markdown-safe text: a bare $ would start LaTeX math in st.markdown."""
    return "" if text is None else str(text).replace("$", "\\$")


def source_note():
    path, kind = source()
    if kind == "snapshot":
        last = query("SELECT MAX(finished_at) AS t FROM run_log")["t"].iloc[0]
        st.caption(f"Showing a saved snapshot of a local run (last import {last} UTC). "
                   "Live cloud data arrives in Milestone 6. Simulated data only.")
    else:
        st.caption(f"Live: reading `{path.name}` read-only. Simulated data only.")
    return kind
