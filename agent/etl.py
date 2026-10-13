"""ETL steps for one import file. Milestone 2: Detect and Land."""

import csv
import hashlib
import io
import json
import uuid
from datetime import datetime

from agent.db import now


def move(path, folder, run_id):
    """Move a file into folder without ever overwriting an earlier file of the same name."""
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / path.name
    if dest.exists():
        dest = folder / f"{run_id}_{path.name}"
    path.rename(dest)
    return dest


def parse(path, data):
    """Return the records in a CSV or JSON import, exactly as they arrived."""
    if path.suffix == ".csv":
        return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    if path.suffix == ".json":
        doc = json.loads(data)
        return doc["lots"] if isinstance(doc, dict) else doc
    raise ValueError(f"unsupported file type {path.suffix!r}")


def process_file(path, db, raw, out_dir):
    """Run Detect and Land for one file. Returns 'landed', 'duplicate' or 'quarantined'."""
    db.run_id = f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    db.file_name, db.file_sha256 = path.name, None

    # 1. Detect: file name, size and SHA-256; skip any content already landed
    step = db.start_step("detect")
    data = path.read_bytes()
    db.file_sha256 = hashlib.sha256(data).hexdigest()
    seen = db.run_sql(
        "detect",
        "SELECT file_name FROM run_log WHERE step = ? AND status = ? AND file_sha256 = ? LIMIT 1",
        ("land", "ok", db.file_sha256),
    )
    if seen:
        dest = move(path, out_dir / "processed", db.run_id)
        db.finish_step(step, "warning",
                       f"duplicate of {seen[0]['file_name']}, skipped; moved to {dest.parent.name}/")
        return "duplicate"
    db.finish_step(step, "ok", f"{len(data)} bytes, sha256 {db.file_sha256[:12]}…")

    # 2. Land: store every record as-is in TinyDB, tagged with where it came from
    step = db.start_step("land")
    try:
        records = parse(path, data)
    except (ValueError, KeyError, csv.Error, UnicodeDecodeError) as err:
        dest = move(path, out_dir / "quarantine", db.run_id)
        db.finish_step(step, "failed", f"could not parse ({err}); moved to {dest.parent.name}/")
        return "quarantined"

    landed_at = now()
    raw.insert_multiple(
        {**r, "_run_id": db.run_id, "_file_name": path.name,
         "_file_sha256": db.file_sha256, "_landed_at": landed_at}
        for r in records
    )
    dest = move(path, out_dir / "processed", db.run_id)
    db.finish_step(step, "ok", f"{len(records)} records to TinyDB; moved to {dest.parent.name}/",
                   rows_in=len(records), rows_out=len(records), rows_rejected=0)
    return "landed"
