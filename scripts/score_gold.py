"""Score the experts against the hand-labeled gold set (gold_set.csv).

Runs Jev and Claude (fast model, same questions) on all 50 notes, plus Claude extraction,
compares field by field, and logs every score to stats_log with the prompt versions.
Model calls go through the Enricher, so they are cached and logged in model_usage_log.

Run:  uv run python -m scripts.score_gold
"""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from agent import settings
from agent.db import Database, now
from agent.enrich import Enricher
from agent.experts import EXTRACT_FIELDS

ROOT = settings.ROOT
GOLD = ROOT / "gold_set.csv"
EXPORTS = ROOT / "exports"
CFG = settings.CONFIG
FAST = CFG["models"]["extract"]


def load_gold():
    if not GOLD.exists():
        sys.exit("gold_set.csv not found: run python -m scripts.make_gold_set, then label it by hand")
    rows = list(csv.DictReader(GOLD.open()))
    unlabeled = [r["gold_id"] for r in rows if not r["labeled_by"].strip()]
    if unlabeled:
        sys.exit(f"{len(unlabeled)} rows have no labeled_by (first: {unlabeled[0]}); label every row first")
    for r in rows:
        r["urgency"], r["injection"] = int(r["urgency"]), int(r["injection"])
        for f in EXTRACT_FIELDS:
            r[f] = float(r[f]) if r[f].strip() else None
    return rows


def accuracy(pairs):
    return sum(p == g for p, g in pairs) / len(pairs)


def per_class(pairs, labels):
    out = {}
    for c in labels:
        tp = sum(p == c and g == c for p, g in pairs)
        predicted, actual = sum(p == c for p, _ in pairs), sum(g == c for _, g in pairs)
        out[c] = {"precision": tp / predicted if predicted else None,
                  "recall": tp / actual if actual else None, "n": actual}
    return out


def brier_multiclass(probs, golds):
    """Mean over records of sum over classes (p_k - y_k)^2. 0 is perfect, 2 is worst."""
    return sum(sum((p.get(k, 0) - (k == g)) ** 2 for k in p) for p, g in zip(probs, golds)) / len(golds)


def same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) < 0.01)


def main():
    gold = load_gold()
    db = Database(settings.SQLITE_PATH)
    db.run_id, db.file_name = f"gold-{datetime.now():%Y%m%d-%H%M%S}", GOLD.name
    en = Enricher(db)
    en.start_run()
    versions = {"jev_questions": CFG["prompts"]["jev_questions"], "extract": CFG["prompts"]["extract"],
                "jev_model": CFG["models"]["jev"], "claude_model": FAST}
    labels = list(en.questions["category"]["criteria"])

    def log_stat(method, formula, result, threshold=None, inputs=None, interpretation=""):
        db.run_sql("gold_set",
                   "INSERT INTO stats_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (db.run_id, "gold_set", method, formula, json.dumps({**versions, **(inputs or {})}),
                    result, threshold, interpretation, now()))
        print(f"  {method:34} {result:.3f}" + (f"  (pass bar {threshold})" if threshold is not None else ""))

    experts = {"jev": ("jev", "gold set: Jev")}
    if en.claude_available:
        experts[FAST] = (FAST, "gold set: Claude answers the same questions as Jev")
    preds = {name: [] for name in experts}
    for row in gold:
        for name, forced in experts.items():
            res, _, _ = en.call("classify", row["note"], forced=forced)
            preds[name].append(res.output if res else None)

    ACC = r"\frac{1}{n}\sum_i \mathbf{1}[\hat{y}_i = y_i]"
    for name, out in preds.items():
        ok = [(p, g) for p, g in zip(out, gold) if p]
        print(f"\n{name}: {len(ok)} of {len(gold)} notes answered")
        cat = [(p["category"], g["category"]) for p, g in ok]
        log_stat(f"{name}_category_accuracy", ACC, accuracy(cat), CFG["gold"]["category_accuracy"],
                 {"n": len(cat), "per_class": per_class(cat, labels)})
        urg = [(round(p["urgency"]), g["urgency"]) for p, g in ok]
        log_stat(f"{name}_urgency_exact", ACC, accuracy(urg), inputs={"n": len(urg)})
        log_stat(f"{name}_urgency_within_1", r"\frac{1}{n}\sum_i \mathbf{1}[|\hat{y}_i - y_i| \le 1]",
                 sum(abs(p - g) <= 1 for p, g in urg) / len(urg), inputs={"n": len(urg)})
        inj = [(p["injection_p"] >= CFG["thresholds"]["injection"], g["injection"] == 1) for p, g in ok]
        caught, actual = sum(p and g for p, g in inj), sum(g for _, g in inj)
        log_stat(f"{name}_injection_recall", r"\frac{TP}{TP + FN}", caught / actual if actual else 1.0,
                 CFG["gold"]["injection_recall"], {"caught": caught, "actual": actual,
                                                   "false_alarms": sum(p and not g for p, g in inj)})
        if name == "jev":  # only Jev returns probabilities, so only Jev gets calibration scores
            log_stat("jev_category_brier", r"\frac{1}{n}\sum_i \sum_k (p_{ik} - y_{ik})^2",
                     brier_multiclass([p["category_probs"] for p, _ in ok], [g["category"] for _, g in ok]),
                     inputs={"n": len(ok)}, interpretation="0 is perfect; lower is better")
            log_stat("jev_injection_brier", r"\frac{1}{n}\sum_i (p_i - y_i)^2",
                     sum((p["injection_p"] - g["injection"]) ** 2 for p, g in ok) / len(ok),
                     inputs={"n": len(ok)}, interpretation="0 is perfect; lower is better")
            bins = {}
            for p, g in ok:
                b = min(int(max(p["category_probs"].values()) * 10), 9) / 10
                bins.setdefault(b, []).append(p["category"] == g["category"])
            EXPORTS.mkdir(exist_ok=True)
            with (EXPORTS / "gold_calibration.csv").open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["confidence_bin", "n", "accuracy"])
                for b in sorted(bins):
                    w.writerow([f"{b:.1f}-{b + 0.1:.1f}", len(bins[b]), sum(bins[b]) / len(bins[b])])

    extractions = []
    if en.claude_available:
        print(f"\n{FAST} extraction:")
        for row in gold:
            res, _, _ = en.call("extract", row["note"], forced=(FAST, "gold set: extraction"))
            extractions.append(res.output if res else None)
        done = [(e, g) for e, g in zip(extractions, gold) if e]
        for f in EXTRACT_FIELDS:
            log_stat(f"extract_{f}_accuracy", ACC, accuracy([(same(e[f], g[f]), True) for e, g in done]),
                     inputs={"n": len(done)})
        invented = [(g["gold_id"], f) for e, g in done for f in EXTRACT_FIELDS if e[f] is not None and g[f] is None]
        log_stat("extract_invented_values", r"\sum_i \sum_f \mathbf{1}[\hat{v}_{if} \ne \varnothing \wedge v_{if} = \varnothing]",
                 float(len(invented)), float(CFG["gold"]["invented_values"]), {"invented": invented})
        print("\nCheck these 10 summaries by hand:")
        for e, g in done[:10]:
            print(f"  {g['gold_id']} {g['note'][:50]!r:54} -> {e['summary']!r}")
    else:
        print("\nANTHROPIC_API_KEY not set: skipped Claude classify and extraction scoring")

    EXPORTS.mkdir(exist_ok=True)
    with (EXPORTS / "gold_predictions.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["gold_id", "note", "gold_category", "expert", "category", "category_confidence",
                    "urgency", "injection_p"])
        for name, out in preds.items():
            for p, g in zip(out, gold):
                if p:
                    w.writerow([g["gold_id"], g["note"], g["category"], name, p["category"],
                                p["category_confidence"], p["urgency"], p["injection_p"]])
    print(f"\nrun {db.run_id}: scores in stats_log; predictions in exports/gold_predictions.csv")


if __name__ == "__main__":
    main()
