"""Cloud mode for the Streamlit Community Cloud app (Milestone 6).

Reads the agent's database from the `data` branch, starts a 30-minute demo run of the
pipeline workflow, and files Release / Reject requests as GitHub issues that the pipeline applies.
Needs Streamlit secrets: GH_TOKEN (fine-grained, this repo only: Contents read, Issues read/write,
Actions read/write) and APP_PASSCODE (protects Start demo, Release and Reject). Optional: GH_REPO.
"""

import hmac
import json
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

DEFAULT_REPO = "Rollins-College-DTAC/coffee-purchasing-demo"
WORKFLOW = "pipeline.yml"
DEMO_MINUTES = 30


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:  # no secrets file (local runs)
        return default


def enabled():
    return bool(secret("GH_TOKEN"))


def api(method, path, body=None, raw=False):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{secret('GH_REPO', DEFAULT_REPO)}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {secret('GH_TOKEN')}", "X-GitHub-Api-Version": "2022-11-28",
                 "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if raw else json.loads(data or b"null")


@st.cache_data(ttl=30, show_spinner="Fetching the latest data from GitHub…")
def _download():
    return api("GET", "/contents/data/analytics.sqlite?ref=data", raw=True)


def database_path():
    """Download the data branch's analytics.sqlite (cached 30 s) and return a local path, or None."""
    try:
        data = _download()
    except (urllib.error.URLError, TimeoutError):
        return None
    path = Path(tempfile.gettempdir()) / f"coffee-cloud-{hash(data) & 0xFFFFFFFF:08x}.sqlite"
    if not path.exists():
        for old in path.parent.glob("coffee-cloud-*.sqlite"):  # keep only the newest copy
            old.unlink(missing_ok=True)
        path.write_bytes(data)
    return path


def reviewer_ok(name):
    """Same rule the pipeline applies to the reviewer name in a review issue."""
    from agent.cloud_loop import REVIEWER
    return bool(REVIEWER.match(name.strip()))


def passcode_ok(entered):
    expected = secret("APP_PASSCODE")
    return bool(expected) and hmac.compare_digest(str(entered), str(expected))


# ---------- Start demo ----------

@st.cache_data(ttl=10, show_spinner=False)
def latest_run():
    runs = api("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page=1").get("workflow_runs", [])
    return runs[0] if runs else None


def demo_controls():
    """Sidebar: demo status and the Start demo button (passcode-protected)."""
    st.sidebar.subheader("Demo")
    try:
        run = latest_run()
    except urllib.error.URLError as err:
        st.sidebar.warning(f"Cannot reach GitHub Actions: {err}")
        return
    active = run and run["status"] in ("queued", "in_progress", "waiting", "pending", "requested")
    if active:
        started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
        left = DEMO_MINUTES - (datetime.now(timezone.utc) - started).total_seconds() / 60
        st.sidebar.success(f"Demo running: about {max(0, left):.0f} min left. New import every minute.")
        st.sidebar.markdown(f"[Watch the workflow run]({run['html_url']})")
        return
    if run:
        st.sidebar.caption(f"Last demo: {run['conclusion'] or run['status']}, {run['created_at'][:16].replace('T', ' ')} UTC")
    code = st.sidebar.text_input("Passcode", type="password", key="demo-passcode")
    if st.sidebar.button(f"Start demo ({DEMO_MINUTES} min)", type="primary", disabled=not code):
        if not passcode_ok(code):
            st.sidebar.error("Wrong passcode.")
            return
        api("POST", f"/actions/workflows/{WORKFLOW}/dispatches",
            {"ref": "main", "inputs": {"minutes": str(DEMO_MINUTES), "backfill": "auto"}})
        latest_run.clear()
        st.sidebar.success("Demo starting. The first import lands in about 2 minutes.")
    st.sidebar.caption("Runs only on request, once a minute for 30 minutes, to save API credits.")


# ---------- Release / Reject ----------

def request_review(run_id, release, reviewer):
    """File a review request; the running pipeline applies it within a minute and closes the issue."""
    issue = api("POST", "/issues", {
        "title": f"review: {'release' if release else 'reject'} {run_id}",
        "body": json.dumps({"reviewer": reviewer}),
    })
    return issue["html_url"]


@st.cache_data(ttl=10, show_spinner=False)
def pending_reviews():
    issues = api("GET", "/issues?state=open&per_page=50")
    return [i["title"] for i in issues if i.get("title", "").startswith("review: ")]
