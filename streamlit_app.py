"""Streamlit Community Cloud entrypoint: navigation for the dashboard pages in dashboard/pages/.

Run:  uv run streamlit run streamlit_app.py
"""

import streamlit as st

from dashboard import cloud

st.set_page_config(page_title="Raw Coffee Purchasing", page_icon="☕", layout="wide")

pages = [
    st.Page("dashboard/pages/live.py", title="Live run", icon="🔴", default=True),
    st.Page("dashboard/pages/models.py", title="Models (MoE)", icon="🧠"),
    st.Page("dashboard/pages/analytics.py", title="Analytics", icon="📈"),
    st.Page("dashboard/pages/data.py", title="Data", icon="🗂️"),
    st.Page("dashboard/pages/history.py", title="Pipeline history", icon="🕒"),
    st.Page("dashboard/pages/governance.py", title="Governance", icon="🛡️"),
]
nav = st.navigation(pages)
if cloud.enabled():
    cloud.demo_controls()
nav.run()
