"""Streamlit Community Cloud entrypoint: navigation for the dashboard pages in dashboard/pages/.

Run:  uv run streamlit run streamlit_app.py
"""

import streamlit as st

st.set_page_config(page_title="Raw Coffee Purchasing", page_icon="☕", layout="wide")

pages = [
    st.Page("dashboard/pages/live.py", title="Live run", icon="🔴", default=True),
    st.Page("dashboard/pages/models.py", title="Models (MoE)", icon="🧠"),
    st.Page("dashboard/pages/analytics.py", title="Analytics", icon="📈"),
]
st.navigation(pages).run()
