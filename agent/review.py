"""A person releases or rejects a held import. The agent never does this on its own.

Run:  uv run python -m agent.review list
      uv run python -m agent.review release <run_id> --by "Your Name"
      uv run python -m agent.review reject  <run_id> --by "Your Name"
"""

import argparse

from agent import settings, warehouse
from agent.db import Database, now


def held(db):
    return [dict(r) for r in db.run_sql(
        "review", "SELECT run_id, file_name, n_lots, mean_price_per_lb FROM imports WHERE status = ?", ("held",))]


def decide(db, run_id, release, by):
    """Release (publish to gold) or reject a held import, and close its incident and review item."""
    db.run_id = run_id
    rows = db.run_sql("review", "SELECT file_name FROM imports WHERE run_id = ? AND status = ?", (run_id, "held"))
    if not rows:
        raise ValueError(f"{run_id} is not a held import")
    db.file_name = rows[0]["file_name"]
    status = "released" if release else "rejected"
    db.run_sql("review", "UPDATE imports SET status = ?, decided_at = ?, decided_by = ? WHERE run_id = ?",
               (status, now(), by, run_id))
    db.run_sql("review", "UPDATE incident_log SET status = ?, decided_by = ?, closed_at = ? "
                         "WHERE run_id = ? AND kind = ? AND status = ?",
               (status, by, now(), run_id, "trigger", "open"))
    db.run_sql("review", "UPDATE review_queue SET status = ?, decided_by = ?, closed_at = ? "
                         "WHERE run_id = ? AND reason = ? AND status = ?",
               ("accepted" if release else "rejected", by, now(), run_id, "trigger_hold", "open"))
    step = db.start_step("review")
    db.finish_step(step, "ok", f"{status} by {by}")
    warehouse.sync(db)
    return status


def main():
    parser = argparse.ArgumentParser(description="Release or reject held imports.")
    parser.add_argument("action", choices=["list", "release", "reject"])
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--by", help="your name (recorded in the logs)")
    args = parser.parse_args()
    db = Database(settings.SQLITE_PATH)

    if args.action == "list":
        for r in held(db):
            print(f"{r['run_id']}  {r['file_name']}  {r['n_lots']} lots  mean ${r['mean_price_per_lb']}/lb")
        return
    if not (args.run_id and args.by):
        parser.error("release and reject need a run_id and --by")
    print(f"{args.run_id}: {decide(db, args.run_id, args.action == 'release', args.by)}")


if __name__ == "__main__":
    main()
