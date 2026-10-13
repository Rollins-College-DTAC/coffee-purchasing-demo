"""Cloud mode (Milestone 6): the same pipeline, run once a minute inside one GitHub Actions job.

Each minute: apply review decisions (GitHub issues titled "review: release|reject <run_id>"),
write one fake import, run the pipeline on it, export exports/daily_summary.json, then commit
the state to the `data` branch (one amended commit, force-pushed) for the Streamlit app.

Run (in Actions):  uv run python -m agent.cloud_loop --minutes 30 --state state/   (started by "Start demo")
Run (try locally): uv run python -m agent.cloud_loop --minutes 2 --state /tmp/state --no-push
"""

import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REVIEW_TITLE = re.compile(r"^review: (release|reject) (run-[0-9]{8}-[0-9]{6}-[0-9a-f]{4})$")
REVIEWER = re.compile(r"^[\w .,'-]{1,60}$")
AUTO_HISTORY = 40  # enough published imports for the trigger (min_history 30) and the forecast (20)


def point_paths_at(state):
    """Every path the agent and generator use lives under state/. Must run before agent imports."""
    state.mkdir(parents=True, exist_ok=True)
    os.environ.update(INBOX_DIR=str(state / "inbox"), OUT_DIR=str(state), DATA_DIR=str(state / "data"),
                      GENERATOR_LOG=str(state / "generator_log.csv"))
    (state / ".gitignore").write_text("inbox/\nprocessed/\nquarantine/\n*.sqlite-wal\n*.sqlite-shm\n")


# ---------- review decisions from the cloud app ----------

def github(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or "null")


def parse_review(issue):
    """(run_id, release, reviewer) from a review issue, or None if it is not a valid request.
    The issue text is untrusted: only an exact title pattern and a short plain name are accepted."""
    m = REVIEW_TITLE.match(issue.get("title", "").strip())
    if not m:
        return None
    try:
        reviewer = str(json.loads(issue.get("body") or "{}").get("reviewer", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        return None
    if not REVIEWER.match(reviewer):
        return None
    return m.group(2), m.group(1) == "release", reviewer


def apply_reviews(db):
    """Apply open review issues and close each with a comment. Returns how many were applied."""
    from agent.review import decide
    if not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY")):
        return 0
    applied = 0
    for issue in github("GET", "/issues?state=open&per_page=50"):
        if "pull_request" in issue or not issue.get("title", "").startswith("review: "):
            continue
        req = parse_review(issue)
        if req is None:
            note = "Not applied: the request did not match `review: release|reject <run_id>` with a reviewer name."
        else:
            run_id, release, reviewer = req
            try:
                note = f"Applied: {run_id} {decide(db, run_id, release, f'{reviewer} (cloud app)')} by {reviewer}."
                applied += 1
            except ValueError as err:
                note = f"Not applied: {err}."
        github("POST", f"/issues/{issue['number']}/comments", {"body": note})
        github("PATCH", f"/issues/{issue['number']}", {"state": "closed"})
        print(f"[cloud] issue #{issue['number']}: {note}", flush=True)
    return applied


# ---------- summary export and publishing ----------

def export_summary(db, path):
    """exports/daily_summary.json: what the Claude Routine reads to write the daily report."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    q = lambda sql, p=(): [dict(r) for r in db.conn.execute(sql, p)]
    forecast = q("SELECT interpretation, result, computed_at FROM stats_log WHERE method = 'forecast_ets' "
                 "ORDER BY computed_at DESC LIMIT 1")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "imports_by_status": q("SELECT status, COUNT(*) AS n FROM imports WHERE loaded_at >= ? GROUP BY status", (today,)),
        "held_imports": q("SELECT i.file_name, i.mean_price_per_lb, i.status, i.decided_by, d.statistic AS z "
                          "FROM imports i JOIN decision_log d USING (run_id) WHERE d.fired = 1 AND i.loaded_at >= ?",
                          (today,)),
        "incidents_by_kind": q("SELECT kind, status, COUNT(*) AS n FROM incident_log WHERE opened_at >= ? "
                               "GROUP BY kind, status", (today,)),
        "model_usage": q("SELECT expert, task, COUNT(*) AS calls, SUM(input_tokens > 0) AS live_calls, "
                         "ROUND(SUM(estimated_cost_usd), 4) AS cost_usd FROM model_usage_log WHERE called_at >= ? "
                         "GROUP BY expert, task", (today,)),
        "latest_forecast": forecast[0] if forecast else None,
        "latest_actual": (q("SELECT file_name, mean_price_per_lb FROM gold_import_stats ORDER BY loaded_at DESC "
                            "LIMIT 1") or [None])[0],
        "trigger_rule": "anomaly: |z| > threshold on import mean $/lb vs recent published imports",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, default=str))


def publish(db, state, push):
    """Checkpoint SQLite into one file, then amend-commit state/ and force-push it to `data`."""
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if not push:
        return
    git = lambda *a: subprocess.run(["git", "-C", str(state), *a], check=True, capture_output=True, text=True)
    git("add", "-A")
    has_commit = subprocess.run(["git", "-C", str(state), "rev-parse", "--verify", "HEAD"],
                                capture_output=True).returncode == 0
    msg = f"state {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    git("commit", "--amend", "-m", msg) if has_commit else git("commit", "-m", msg)
    git("push", "--force", "origin", "HEAD:data")


def main():
    parser = argparse.ArgumentParser(description="Run the pipeline once a minute and publish state.")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--backfill", default="auto",
                        help="past imports to write first: a number, or 'auto' to top history up to 40")
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--no-push", action="store_true", help="run without committing (local try-out)")
    args = parser.parse_args()
    state = args.state.resolve()
    point_paths_at(state)

    import random

    from faker import Faker
    from tinydb import TinyDB

    from agent import settings
    from agent.db import Database
    from agent.enrich import Enricher
    from agent.loop import scan
    from generator import generate as gen

    db, raw = Database(settings.SQLITE_PATH), TinyDB(settings.RAW_PATH)
    enricher = Enricher(db)
    rng, fake = random.Random(), Faker()
    suppliers = gen.make_suppliers(fake)
    summary_path = state / "exports" / "daily_summary.json"

    have = db.conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    backfill = max(0, AUTO_HISTORY - have) if args.backfill == "auto" else int(args.backfill)
    print(f"[cloud] {have} imports in history; backfilling {backfill}", flush=True)
    for back in range(backfill, 0, -1):
        gen.write_import(gen.INBOX, gen.LOG, datetime.now() - timedelta(minutes=back), rng, suppliers)
    if backfill:
        scan(db, raw, enricher)
        export_summary(db, summary_path)
        publish(db, state, not args.no_push)

    for n in range(args.minutes):
        started = time.monotonic()
        apply_reviews(db)
        for name in gen.write_import(gen.INBOX, gen.LOG, datetime.now(), rng, suppliers):
            print(f"[cloud] wrote {name}", flush=True)
        scan(db, raw, enricher)
        export_summary(db, summary_path)
        publish(db, state, not args.no_push)
        print(f"[cloud] minute {n + 1}/{args.minutes} published", flush=True)
        if n < args.minutes - 1:
            time.sleep(max(0.0, 60 - (time.monotonic() - started)))
    apply_reviews(db)  # decisions made during the last minute
    publish(db, state, not args.no_push)


if __name__ == "__main__":
    main()
