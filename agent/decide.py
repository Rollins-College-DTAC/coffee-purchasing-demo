"""Decide and Act (Milestone 4).

Decide: z-score of this import's mean $/lb against the last `window` published imports.
Act:    |z| <= threshold -> publish to the gold views.
        |z| >  threshold -> hold: incident, review-queue entry, and a plain-English explanation
                            from the strong Claude model. Only a person can release or reject it.
Every decision goes to decision_log; every statistic to stats_log.
"""

import uuid

from agent import settings
from agent.db import now
from agent.stats import backtest, forecast, log_stat, mean_ci, z_score

RULE = settings.CONFIG["trigger"]


def decide_import(db, imp, en):
    """Run the anomaly rule on one loaded import and act on it. Returns 'published' or 'held'."""
    step = db.start_step("decide")
    x = imp["mean_price_per_lb"]
    history = [r["mean_price_per_lb"] for r in db.run_sql(
        "decide",
        "SELECT mean_price_per_lb FROM gold_import_stats WHERE run_id != ? ORDER BY loaded_at DESC LIMIT ?",
        (db.run_id, RULE["window"]),
    )]

    if x is None:
        fired, stat, why = True, None, "no valid lots to test"
    elif len(history) < RULE["min_history"]:
        fired, stat, why = False, None, f"warming up: {len(history)} of {RULE['min_history']} imports of history"
    else:
        stat = z_score(x, history, RULE["z_threshold"])
        log_stat(db, "decide", stat)
        fired, why = abs(stat["result"]) > RULE["z_threshold"], stat["interpretation"]

    action = "hold_and_alert" if fired else "publish"
    db.run_sql(
        "decide", "INSERT INTO decision_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (db.run_id, "anomaly", stat["result"] if stat else None, RULE["z_threshold"], None,
         int(fired), action, now()),
    )
    db.finish_step(step, "held" if fired else "ok",
                   f"|z| = {abs(stat['result']):.2f} vs {RULE['z_threshold']}: {action}" if stat else f"{why}: {action}")

    step = db.start_step("act")
    if fired:
        hold(db, imp, stat, why, en)
        db.finish_step(step, "held", "held out of gold; incident opened; waiting for a person to release or reject")
        return "held"
    db.run_sql("act", "UPDATE imports SET status = ?, decided_at = ?, decided_by = ? WHERE run_id = ?",
               ("published", now(), "agent", db.run_id))
    db.finish_step(step, "ok", "published to gold views")
    return "published"


def hold(db, imp, stat, why, en):
    db.run_sql("act", "UPDATE imports SET status = ?, decided_at = ? WHERE run_id = ?", ("held", now(), db.run_id))
    lots = db.run_sql(
        "act", "SELECT lot_id, origin, price_per_lb, quantity_lbs FROM purchases WHERE run_id = ? ORDER BY price_per_lb DESC",
        (db.run_id,),
    )
    facts = "\n".join(
        [f"Import {db.file_name} was held: {why}",
         f"Mean of its {len(lots)} lots (computed by the pipeline): "
         + (f"${imp['mean_price_per_lb']:.2f}/lb" if imp["mean_price_per_lb"] is not None else "none")]
        + [f"{r['lot_id']}: {r['origin']}, ${r['price_per_lb']:.2f}/lb, {r['quantity_lbs']:.0f} lbs" for r in lots]
    )
    res, expert, call_id = en.call("explain", facts)
    explanation = res.output["text"] if res else why

    en.incident("trigger", f"{db.file_name}: {why}")
    db.run_sql(
        "act",
        "INSERT INTO review_queue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex[:12], db.run_id, db.file_name, None, "trigger_hold",
         f"{explanation} [explained by {expert}]", "open", None, now(), None),
    )


def run_statistics(db):
    """Descriptive CIs by origin and the forecast with its backtest, on published data."""
    step = db.start_step("statistics")
    by_origin = {}
    for r in db.run_sql("statistics", "SELECT origin, price_per_lb FROM gold_purchases", ()):
        by_origin.setdefault(r["origin"], []).append(r["price_per_lb"])
    for origin, prices in sorted(by_origin.items()):
        log_stat(db, "statistics", mean_ci(prices, origin))

    series = [r["mean_price_per_lb"] for r in db.run_sql(
        "statistics", "SELECT mean_price_per_lb FROM gold_import_stats ORDER BY loaded_at", ())]
    done = [f"{len(by_origin)} origin CIs"]
    if len(series) >= 20:
        _, nxt = forecast(series)
        log_stat(db, "statistics", nxt)
        log_stat(db, "statistics", backtest(series))
        done.append("forecast and backtest")
    else:
        done.append(f"forecast waits for 20 published imports (have {len(series)})")
    db.finish_step(step, "ok", "; ".join(done))
