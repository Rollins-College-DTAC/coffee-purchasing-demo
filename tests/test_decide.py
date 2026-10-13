"""Clean, statistics, Decide/Act, review and warehouse tests. No live API or Snowflake calls."""

import json

import pytest
from tinydb import TinyDB

from agent import experts, warehouse
from agent.clean import clean_record, load_file
from agent.db import Database, now
from agent.decide import decide_import, run_statistics
from agent.enrich import Enricher
from agent.etl import process_file
from agent.review import decide as review_decide
from agent.stats import backtest, forecast, mean_ci, z_score

CSV = (
    "lot_id,origin,supplier,quantity,quantity_unit,price,price_unit,cupping_score,harvest_date,notes\n"
    "LOT-0100-00,ETH,DOYLE GREEN COFFEE IMPORTERS,10,bags,13.23,USD/kg,85.5,03/14/2026,\n"
    "LOT-0100-01,Columbia,doyle green coffee importers,1500,lbs,4.60,per lb,,Mar 2026,\n"
    "LOT-0100-01,Columbia,doyle green coffee importers,1500,lbs,4.60,per lb,,Mar 2026,\n"
    "LOT-0100-02,Atlantis,Doyle Green Coffee Importers,100,kg,5.00,$/lb,84,2026-01-02,\n"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    for k in warehouse.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    return Database(tmp_path / "a.sqlite")


STEADY = [5.0, 5.1, 4.9, 5.05, 4.95, 5.0, 5.1, 4.9, 5.0, 5.02, 4.98, 5.0] * 3  # 36 published imports


def add_history(db, means):
    for i, m in enumerate(means):
        db.conn.execute("INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"run-h{i:03d}", f"h{i}.csv", 8, m, "published", f"2026-10-13T08:{i:02d}:00+00:00", now(), "agent"))
    db.conn.commit()


def pending(db, run_id, mean):
    db.run_id, db.file_name = run_id, f"{run_id}.csv"
    db.conn.execute("INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, db.file_name, 1, mean, "pending", now(), None, None))
    db.conn.execute("INSERT INTO purchases (run_id, lot_id, origin, price_per_lb, quantity_lbs) VALUES (?, ?, ?, ?, ?)",
                    (run_id, "LOT-9", "Kenya", mean, 1000))
    db.conn.commit()
    return {"run_id": run_id, "n_lots": 1, "mean_price_per_lb": mean}


@pytest.fixture
def en(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []

    def fake_explain(facts, template, model, max_tokens):
        calls.append(facts)
        return experts.Result({"text": "Prices are far above recent imports. Check the Kenya lot."}, None, 400, 60, model)

    monkeypatch.setattr(experts, "claude_explain", fake_explain)
    e = Enricher(db)
    e.explain_calls = calls
    return e


def rows(db, sql):
    return [dict(r) for r in db.conn.execute(sql)]


# ---------- Clean ----------

def test_clean_converts_units_names_and_dates():
    p = clean_record({"lot_id": "L1", "origin": "ethiopian yirgacheffe", "supplier": "DOYLE  GREEN COFFEE IMPORTERS",
                      "quantity": "10", "quantity_unit": "bags", "price": "13.23", "price_unit": "USD/kg",
                      "cupping_score": "", "harvest_date": "03/14/2026", "notes": ""})
    assert (p.origin, p.supplier) == ("Ethiopia", "Doyle Green Coffee Importers")
    assert p.quantity_lbs == pytest.approx(10 * 69 * 2.20462, abs=0.1)
    assert p.price_per_lb == pytest.approx(6.0, abs=0.01)
    assert p.cupping_score is None and p.harvest_date == "2026-03-14"

    j = clean_record({"lotId": "L2", "Origin": "kenya AA", "Vendor": "x", "qty": "9,279 lbs",
                      "price": "6.87 USD/lb", "score": 84.1, "harvest": "Mar 2026", "buyer_notes": ""})
    assert (j.origin, j.quantity_lbs, j.price_per_lb, j.harvest_date) == ("Kenya", 9279, 6.87, "2026-03-01")


def test_load_drops_duplicates_and_rejects_bad_rows(tmp_path, db):
    (tmp_path / "coffee_20261013-1000.csv").write_text(CSV)
    raw = TinyDB(tmp_path / "raw.json")
    process_file(tmp_path / "coffee_20261013-1000.csv", db, raw, tmp_path)
    imp = load_file(db, raw)
    assert imp["n_lots"] == 2  # duplicate dropped, unknown origin rejected
    assert imp["mean_price_per_lb"] == pytest.approx((6.0 + 4.6) / 2, abs=0.01)
    load = rows(db, "SELECT * FROM run_log WHERE step = 'load'")[0]
    assert load["status"] == "warning" and "Atlantis" in load["message"]


# ---------- Statistics ----------

def test_mean_ci_matches_the_t_formula():
    s = mean_ci([4.0, 5.0, 6.0], "test")
    assert s["result"] == 5.0
    assert s["ci"] == pytest.approx((5 - 4.303 * 1 / 3 ** 0.5, 5 + 4.303 * 1 / 3 ** 0.5), abs=0.01)
    assert "t_{0.975,n-1}" in s["inputs"] and s["formula_latex"].startswith(r"\bar{x}")


def test_z_score_and_forecast_shapes():
    z = z_score(8.0, [5.0, 5.2, 4.8, 5.1, 4.9], 3)
    assert z["result"] > 3 and "hold" in z["interpretation"]
    series = [5 + 0.01 * t + (0.1 if t % 2 else -0.1) for t in range(30)]
    points, nxt = forecast(series)
    assert len(points) == 5 and points[0]["lo"] < points[0]["yhat"] < points[0]["hi"]
    assert backtest(series)["method"] == "backtest_mae"


# ---------- Decide and Act ----------

def test_seeded_spike_fires_trigger_holds_and_opens_incident(db, en):
    add_history(db, STEADY)
    imp = pending(db, "run-spike", 8.0)
    assert decide_import(db, imp, en) == "held"

    assert rows(db, "SELECT status FROM imports WHERE run_id = 'run-spike'")[0]["status"] == "held"
    d = rows(db, "SELECT * FROM decision_log")[0]
    assert d["fired"] == 1 and d["action"] == "hold_and_alert" and d["statistic"] > 3
    assert [i["kind"] for i in rows(db, "SELECT * FROM incident_log")] == ["trigger"]
    review = rows(db, "SELECT * FROM review_queue")[0]
    assert review["status"] == "open" and "Kenya" in review["detail"]
    assert "LOT-9: Kenya, $8.00/lb" in en.explain_calls[0]
    assert rows(db, "SELECT method FROM stats_log")[0]["method"] == "z_score"
    assert rows(db, "SELECT COUNT(*) n FROM gold_import_stats WHERE run_id = 'run-spike'")[0]["n"] == 0


def test_normal_import_is_published(db, en):
    add_history(db, STEADY)
    assert decide_import(db, pending(db, "run-ok", 5.03), en) == "published"
    assert rows(db, "SELECT fired FROM decision_log")[0]["fired"] == 0
    assert rows(db, "SELECT COUNT(*) n FROM incident_log")[0]["n"] == 0
    assert en.explain_calls == []


def test_warming_up_publishes_without_testing(db, en):
    add_history(db, [5.0, 5.1])
    assert decide_import(db, pending(db, "run-early", 9.0), en) == "published"
    assert "warming up" in rows(db, "SELECT message FROM run_log WHERE step = 'decide'")[0]["message"]


def test_statistics_log_cis_and_forecast(db, en):
    add_history(db, [5 + 0.01 * t for t in range(25)])
    for i in range(25):
        db.conn.execute("INSERT INTO purchases (run_id, lot_id, origin, price_per_lb) VALUES (?, ?, ?, ?)",
                        (f"run-h{i:03d}", "L", "Kenya" if i % 2 else "Brazil", 5 + 0.01 * i))
    db.run_id = "run-stats"
    run_statistics(db)
    methods = [r["method"] for r in rows(db, "SELECT method FROM stats_log")]
    assert methods == ["mean_ci_95:Brazil", "mean_ci_95:Kenya", "forecast_ets", "backtest_mae"]


# ---------- A person decides ----------

@pytest.mark.parametrize("release, status", [(True, "released"), (False, "rejected")])
def test_person_releases_or_rejects_a_held_import(db, en, release, status):
    add_history(db, STEADY)
    decide_import(db, pending(db, "run-spike", 8.0), en)
    assert review_decide(db, "run-spike", release, "Test Buyer") == status
    assert rows(db, "SELECT status, decided_by FROM imports WHERE run_id = 'run-spike'")[0] == \
        {"status": status, "decided_by": "Test Buyer"}
    assert rows(db, "SELECT status FROM incident_log")[0]["status"] == status
    in_gold = rows(db, "SELECT COUNT(*) n FROM gold_import_stats WHERE run_id = 'run-spike'")[0]["n"]
    assert in_gold == (1 if release else 0)
    with pytest.raises(ValueError):
        review_decide(db, "run-spike", True, "Test Buyer")  # no longer held


# ---------- Warehouse ----------

def test_warehouse_skips_without_credentials(db):
    db.run_id = "run-x"
    assert warehouse.sync(db) == "skipped"
    assert "skipped" in rows(db, "SELECT message FROM run_log WHERE step = 'warehouse'")[0]["message"]


def test_warehouse_replaces_the_runs_rows(db, monkeypatch):
    for k in warehouse.REQUIRED:
        monkeypatch.setenv(k, "test")
    executed = []

    class Cur:
        def execute(self, sql, params=None):
            executed.append((sql.split()[0], params))

        def executemany(self, sql, seq):
            executed.append((sql.split()[0] + " many", len(seq)))

    class Con:
        def cursor(self):
            return Cur()

        def commit(self):
            executed.append(("COMMIT", None))

        def close(self):
            pass

    monkeypatch.setattr(warehouse.snowflake.connector, "connect", lambda **kw: Con())
    pending(db, "run-sf", 5.0)
    assert warehouse.sync(db) == "synced"
    kinds = [k for k, _ in executed]
    assert kinds.count("DELETE") == 2 and kinds.count("INSERT many") == 2 and kinds[-1] == "COMMIT"
