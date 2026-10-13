"""Score the anomaly trigger against the generator's answer key (generator_log.csv).

A held import is a positive; a seeded anomaly is a true anomaly. Imports decided while the
rule was warming up (no z-score) are left out. Results go to stats_log and the console.

Run:  uv run python -m scripts.score_trigger
"""

import csv
from datetime import datetime

from agent import settings
from agent.db import Database
from agent.stats import log_stat

GEN_LOG = settings.ROOT / "generator_log.csv"
FORMULA = r"\mathrm{precision} = \frac{TP}{TP + FP}, \qquad \mathrm{recall} = \frac{TP}{TP + FN}"


def main():
    with GEN_LOG.open() as f:
        truth = {r["file_name"]: r for r in csv.DictReader(f) if not r["copy_of"]}
    db = Database(settings.SQLITE_PATH)
    db.run_id, db.file_name = f"trigger-{datetime.now():%Y%m%d-%H%M%S}", GEN_LOG.name
    decided = db.run_sql(
        "score_trigger",
        "SELECT i.file_name, d.fired, d.statistic FROM decision_log d JOIN imports i USING (run_id) "
        "WHERE d.rule = ? AND d.statistic IS NOT NULL",
        ("anomaly",),
    )
    tp = fp = fn = tn = 0
    misses = []
    for r in decided:
        actual = truth.get(r["file_name"], {}).get("anomaly") == "1"
        kind = truth.get(r["file_name"], {}).get("anomaly_kind", "")
        if r["fired"] and actual:
            tp += 1
        elif r["fired"]:
            fp += 1
            misses.append(f"false alarm  {r['file_name']}  z={r['statistic']:.2f}")
        elif actual:
            fn += 1
            misses.append(f"missed {kind:9} {r['file_name']}  z={r['statistic']:.2f}")
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    inputs = {"TP": tp, "FP": fp, "FN": fn, "TN": tn}
    for name, value in (("trigger_precision", precision), ("trigger_recall", recall)):
        log_stat(db, "score_trigger", {
            "method": name, "formula_latex": FORMULA, "inputs": inputs, "result": value, "threshold": None,
            "interpretation": f"{tp} of {tp + fp} holds were real anomalies; {tp} of {tp + fn} real anomalies were held."})
    print(f"{len(decided)} tested imports: TP {tp}, FP {fp}, FN {fn}, TN {tn}")
    print(f"precision {precision:.2f}   recall {recall:.2f}")
    for m in misses:
        print("  " + m)


if __name__ == "__main__":
    main()
