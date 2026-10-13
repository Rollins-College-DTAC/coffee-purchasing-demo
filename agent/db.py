"""SQLite layer: log tables, step logging, and run_sql, the one door every query goes through."""

import json
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_log (
  run_id TEXT, step TEXT, file_name TEXT, file_sha256 TEXT,
  started_at TEXT, finished_at TEXT,
  rows_in INTEGER, rows_out INTEGER, rows_rejected INTEGER,
  status TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS sql_log (
  run_id TEXT, step TEXT, file_name TEXT, sql_text TEXT, params_json TEXT,
  rows_affected INTEGER, duration_ms INTEGER, executed_at TEXT
);
CREATE TABLE IF NOT EXISTS model_usage_log (
  call_id TEXT, run_id TEXT, step TEXT, task TEXT, expert TEXT, route_reason TEXT,
  prompt_version TEXT, input_tokens INTEGER, output_tokens INTEGER, latency_ms INTEGER,
  confidence REAL, estimated_cost_usd REAL, pii_redacted INTEGER, outcome TEXT, called_at TEXT
);
CREATE TABLE IF NOT EXISTS stats_log (
  run_id TEXT, step TEXT, method TEXT, formula_latex TEXT, inputs_json TEXT,
  result REAL, threshold REAL, interpretation TEXT, computed_at TEXT
);
CREATE TABLE IF NOT EXISTS decision_log (
  run_id TEXT, rule TEXT, statistic REAL, threshold REAL, p_value REAL,
  fired INTEGER, action TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS incident_log (
  incident_id TEXT, run_id TEXT, kind TEXT, detail TEXT, status TEXT,
  decided_by TEXT, opened_at TEXT, closed_at TEXT
);
"""

MAX_PARAM_CHARS = 80


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shorten(value):
    if isinstance(value, str) and len(value) > MAX_PARAM_CHARS:
        return value[:MAX_PARAM_CHARS] + "…"
    return value


class Database:
    """One SQLite connection plus the current run context (run ID, file name, hash),
    so every log row says which file it belongs to."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The scheduler runs jobs on a worker thread; max_instances=1 keeps them serial.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")  # dashboard can read while the agent writes
        self.conn.executescript(SCHEMA)
        self.run_id = self.file_name = self.file_sha256 = None

    def run_sql(self, step, sql, params=()):
        """Run a query with ? placeholders and record it in sql_log. Returns fetched rows."""
        started = time.perf_counter()
        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()
        self.conn.commit()
        affected = len(rows) if cur.description else cur.rowcount
        self.conn.execute(
            "INSERT INTO sql_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, step, self.file_name, sql,
             json.dumps([shorten(p) for p in params]), affected,
             round((time.perf_counter() - started) * 1000), now()),
        )
        self.conn.commit()
        return rows

    def start_step(self, step):
        """Write a 'running' row to run_log; returns its rowid for finish_step."""
        cur = self.conn.execute(
            "INSERT INTO run_log (run_id, step, file_name, file_sha256, started_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'running')",
            (self.run_id, step, self.file_name, self.file_sha256, now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_step(self, rowid, status, message="", rows_in=None, rows_out=None, rows_rejected=None):
        self.conn.execute(
            "UPDATE run_log SET finished_at = ?, status = ?, message = ?, file_sha256 = ?, "
            "rows_in = ?, rows_out = ?, rows_rejected = ? WHERE rowid = ?",
            (now(), status, message, self.file_sha256, rows_in, rows_out, rows_rejected, rowid),
        )
        self.conn.commit()
