"""The agent loop: scan inbox/ every 15 seconds and process each new import.

Run:  uv run python -m agent.loop          (keeps running)
      uv run python -m agent.loop --once   (process what is waiting, then exit; used by GitHub Actions)
"""

import argparse
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from tinydb import TinyDB

from agent import settings
from agent.db import Database
from agent.enrich import Enricher, enrich_file
from agent.etl import process_file

SCAN_SECONDS = 15


def scan(db, raw, enricher=None):
    if not settings.INBOX.exists():
        return
    # Dot files are the generator's half-written temp files (and .gitkeep)
    for path in sorted(p for p in settings.INBOX.iterdir() if p.is_file() and not p.name.startswith(".")):
        status = process_file(path, db, raw, settings.OUT)
        if status == "landed" and enricher:
            rows = enrich_file(db, raw, enricher)
            counts = {s: sum(r["status"] == s for r in rows) for s in ("ok", "review", "quarantined")}
            status += f", enriched ({counts['ok']} ok, {counts['review']} review, {counts['quarantined']} quarantined)"
        print(f"[agent] {db.run_id} {path.name}: {status}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Governed data agent loop.")
    parser.add_argument("--once", action="store_true", help="process the inbox once and exit")
    args = parser.parse_args()

    db = Database(settings.SQLITE_PATH)
    raw = TinyDB(settings.RAW_PATH)
    enricher = Enricher(db)

    if args.once:
        scan(db, raw, enricher)
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(scan, "interval", seconds=SCAN_SECONDS, args=[db, raw, enricher],
                      max_instances=1, next_run_time=datetime.now())
    print(f"[agent] watching {settings.INBOX} every {SCAN_SECONDS}s (Ctrl+C to stop)", flush=True)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
