import json

import pytest
from tinydb import TinyDB

from agent import settings
from agent.db import Database
from agent.etl import process_file
from agent.loop import scan

CSV = "lot_id,origin,price,price_unit\nLOT-0000-00,ETH,5.80,$/lb\nLOT-0000-01,Columbia,10.14,USD/kg\n"


@pytest.fixture
def env(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return inbox, Database(tmp_path / "analytics.sqlite"), TinyDB(tmp_path / "raw.json"), tmp_path


def run_log(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM run_log ORDER BY rowid")]


def test_duplicate_file_is_skipped_and_logged(env):
    inbox, db, raw, out = env
    (inbox / "coffee_20261013-0900.csv").write_text(CSV)
    (inbox / "coffee_20261013-0900_resend.csv").write_text(CSV)

    assert process_file(inbox / "coffee_20261013-0900.csv", db, raw, out) == "landed"
    assert process_file(inbox / "coffee_20261013-0900_resend.csv", db, raw, out) == "duplicate"

    assert len(raw) == 2  # the copy added nothing
    last = run_log(db)[-1]
    assert last["step"] == "detect" and last["status"] == "warning"
    assert last["file_name"] == "coffee_20261013-0900_resend.csv"
    assert "duplicate of coffee_20261013-0900.csv" in last["message"]
    assert (out / "processed" / "coffee_20261013-0900_resend.csv").exists()


def test_landed_records_are_raw_and_tagged(env):
    inbox, db, raw, out = env
    doc = {"exported_at": "x", "lots": [{"lotId": "LOT-0001-00", "price": "6.87 USD/lb", "score": None}]}
    (inbox / "coffee_20261013-0901.json").write_text(json.dumps(doc))
    process_file(inbox / "coffee_20261013-0901.json", db, raw, out)

    rec = raw.all()[0]
    assert rec["price"] == "6.87 USD/lb" and rec["score"] is None  # unchanged
    assert rec["_file_name"] == "coffee_20261013-0901.json"
    assert rec["_run_id"] == db.run_id and len(rec["_file_sha256"]) == 64
    land = run_log(db)[-1]
    assert (land["step"], land["status"], land["rows_in"]) == ("land", "ok", 1)


def test_unparseable_file_is_quarantined(env):
    inbox, db, raw, out = env
    (inbox / "coffee_20261013-0902.json").write_text("{not json")
    assert process_file(inbox / "coffee_20261013-0902.json", db, raw, out) == "quarantined"
    assert (out / "quarantine" / "coffee_20261013-0902.json").exists()
    assert run_log(db)[-1]["status"] == "failed"
    assert len(raw) == 0


def test_sql_log_uses_placeholders_and_file_name(env):
    inbox, db, raw, out = env
    (inbox / "coffee_20261013-0900.csv").write_text(CSV)
    process_file(inbox / "coffee_20261013-0900.csv", db, raw, out)

    row = db.conn.execute("SELECT * FROM sql_log").fetchone()
    assert "?" in row["sql_text"] and db.file_sha256 not in row["sql_text"]
    assert json.loads(row["params_json"]) == ["land", "ok", db.file_sha256]
    assert row["file_name"] == "coffee_20261013-0900.csv" and row["step"] == "detect"


def test_scan_ignores_dot_files(env, monkeypatch):
    inbox, db, raw, out = env
    monkeypatch.setattr(settings, "INBOX", inbox)
    monkeypatch.setattr(settings, "OUT", out)
    (inbox / ".coffee_20261013-0903.csv.tmp").write_text(CSV)
    (inbox / ".gitkeep").write_text("")
    scan(db, raw)
    assert run_log(db) == []
    assert (inbox / ".coffee_20261013-0903.csv.tmp").exists()
