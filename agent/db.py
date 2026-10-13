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
CREATE TABLE IF NOT EXISTS model_cache (
  input_hash TEXT PRIMARY KEY, task TEXT, expert TEXT, model TEXT, prompt_version TEXT,
  route_reason TEXT, output_json TEXT, confidence REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS record_enrichment (
  run_id TEXT, file_name TEXT, lot_id TEXT, note TEXT,
  category TEXT, category_confidence REAL, urgency REAL, urgency_confidence REAL, injection_p REAL,
  classify_expert TEXT, classify_call_id TEXT,
  extract_json TEXT, extract_expert TEXT, extract_call_id TEXT,
  status TEXT,             -- ok, review, quarantined
  enriched_at TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
  review_id TEXT, run_id TEXT, file_name TEXT, lot_id TEXT, reason TEXT, detail TEXT,
  status TEXT,             -- open, accepted, corrected
  decided_by TEXT, opened_at TEXT, closed_at TEXT
);

-- Silver: typed, validated purchase lots (one row per lot)
CREATE TABLE IF NOT EXISTS purchases (
  run_id TEXT, file_name TEXT, lot_id TEXT, origin TEXT, supplier TEXT,
  quantity_lbs REAL, price_per_lb REAL, cupping_score REAL, harvest_date TEXT,
  note TEXT, category TEXT, urgency REAL,
  freight_premium_usd_per_lb REAL, discount_pct REAL, delay_weeks REAL,
  enrichment_status TEXT, loaded_at TEXT,
  PRIMARY KEY (run_id, lot_id)
);
-- One row per import: its forecast measure and what the Decide step did with it
CREATE TABLE IF NOT EXISTS imports (
  run_id TEXT PRIMARY KEY, file_name TEXT, n_lots INTEGER, mean_price_per_lb REAL,
  status TEXT,             -- pending, published, held, released, rejected
  loaded_at TEXT, decided_at TEXT, decided_by TEXT
);

-- Gold: only imports that were published, or released by a person
CREATE VIEW IF NOT EXISTS gold_import_stats AS
  SELECT run_id, file_name, n_lots, mean_price_per_lb, status, loaded_at
  FROM imports WHERE status IN ('published', 'released');
CREATE VIEW IF NOT EXISTS gold_purchases AS
  SELECT p.* FROM purchases p JOIN gold_import_stats g USING (run_id);
CREATE VIEW IF NOT EXISTS gold_origin_summary AS
  SELECT origin, COUNT(*) AS lots, AVG(price_per_lb) AS avg_price_per_lb,
         AVG(cupping_score) AS avg_cupping_score, SUM(quantity_lbs) AS total_lbs
  FROM gold_purchases GROUP BY origin;
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
