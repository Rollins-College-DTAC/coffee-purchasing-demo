"""Classify and Extract steps (Milestone 3).

For each landed record's buyer note: the router picks an expert, the answer is cached by input
hash, and every call (cached or not) writes one row to model_usage_log. Injection attempts are
quarantined with an incident; uncertain answers go to the review queue for a person.
"""

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import anthropic
import pydantic
from tinydb import Query
from typesafe_sdk import TypeSafeError

from agent import experts, settings
from agent.db import now
from agent.router import route

# Errors that mean "this provider is down or misconfigured": stop calling it this run
CLAUDE_DOWN = (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
               anthropic.APIConnectionError, anthropic.NotFoundError)


# Redact: personal data never reaches a model. Masking is idempotent, so a note can be redacted twice.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(\+\d{1,3}[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
MASKS = ("[EMAIL]", "[PHONE]")


def redact(text):
    return PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", text))


def has_mask(text):
    return any(m in text for m in MASKS)


def note_of(record):
    return (record.get("notes") or record.get("buyer_notes") or "").strip()


def lot_of(record):
    return record.get("lot_id") or record.get("lotId")


class Enricher:
    """Routes tasks to experts and logs each call. One per agent process."""

    def __init__(self, db, config=settings.CONFIG):
        self.db, self.cfg = db, config
        self.questions = experts.load_questions()
        self.extract_template = (settings.PROMPTS / config["prompts"]["extract"]).read_text()
        self.versions = {"classify": config["prompts"]["jev_questions"], "extract": config["prompts"]["extract"]}
        self.jev_available = bool(os.environ.get("TYPESAFE_API_KEY"))
        self.claude_available = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.spent = 0.0

    def start_run(self):
        """Read today's estimated spend once per run, so the budget check survives restarts."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.db.run_sql(
            "budget",
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) AS usd FROM model_usage_log WHERE called_at >= ?",
            (today,),
        )
        self.spent = rows[0]["usd"]
        self.budget_incident_logged = False

    def context(self, escalate=None):
        return {
            "kill_switch": self.cfg["budget"]["kill_switch"] or os.environ.get("KILL_SWITCH") == "1",
            "over_budget": self.spent >= self.cfg["budget"]["daily_usd"],
            "jev_available": self.jev_available,
            "claude_available": self.claude_available,
            "escalate": escalate,
        }

    # ---------- one routed, cached, logged call ----------

    def call(self, task, note, escalate=None, forced=None):
        """Returns (Result or None, expert, call_id). None means the call failed (already logged)."""
        note = redact(note)
        ctx = self.context(escalate)
        expert, reason = forced or route(task, note, ctx)
        if ctx["over_budget"] and expert == "rules" and not self.budget_incident_logged:
            self.incident("budget", f"estimated spend ${self.spent:.4f} reached daily cap "
                                    f"${self.cfg['budget']['daily_usd']:.2f}; tasks go to rules")
            self.budget_incident_logged = True

        version = "rules_v1" if expert == "rules" else self.versions[task]
        model = self.cfg["models"]["jev"] if expert == "jev" else expert
        key = hashlib.sha256(json.dumps([task, model, version, note]).encode()).hexdigest()
        started = time.perf_counter()

        if expert != "rules":
            hit = self.db.run_sql(task, "SELECT * FROM model_cache WHERE input_hash = ?", (key,))
            if hit:
                res = experts.Result(json.loads(hit[0]["output_json"]), hit[0]["confidence"],
                                     model=hit[0]["model"])
                return res, expert, self.log(task, expert, f"cache hit; {reason}", version, res,
                                             started, cost=0.0, tokens=(0, 0), pii=has_mask(note))
        try:
            res = self.run_expert(task, expert, note)
        except TypeSafeError as err:
            self.jev_available = False
            self.log(task, expert, f"{reason}; failed: {type(err).__name__}", version, None, started,
                     outcome="failed", pii=has_mask(note))
            return self.call(task, note, escalate)  # re-route without Jev
        except (anthropic.APIError, pydantic.ValidationError, ValueError) as err:
            if isinstance(err, CLAUDE_DOWN):
                self.claude_available = False
            self.log(task, expert, f"{reason}; failed: {type(err).__name__}", version, None, started,
                     outcome="failed", pii=has_mask(note))
            return None, expert, None

        price = self.cfg["pricing"].get(expert, {"input": 0, "output": 0})
        cost = (res.input_tokens * price["input"] + res.output_tokens * price["output"]) / 1e6
        self.spent += cost
        if expert != "rules":
            self.db.run_sql(
                task, "INSERT OR REPLACE INTO model_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, task, expert, res.model, version, reason, json.dumps(res.output), res.confidence, now()),
            )
        return res, expert, self.log(task, expert, reason, version, res, started, cost=cost,
                                     tokens=(res.input_tokens, res.output_tokens), pii=has_mask(note))

    def run_expert(self, task, expert, note):
        models, budget = self.cfg["models"], self.cfg["budget"]
        if expert == "rules":
            return experts.rules_classify(note) if task == "classify" else experts.rules_extract(note)
        if task == "classify":
            if expert == "jev":
                return experts.jev_classify(note, self.questions, models["jev"])
            return experts.claude_classify(note, self.questions, expert, models.get("extract_effort"))
        if expert == models["escalate"]:
            return experts.claude_extract(note, self.extract_template, expert, budget["escalate_max_tokens"])
        return experts.claude_extract(note, self.extract_template, expert, budget["extract_max_tokens"],
                                      models.get("extract_effort"))

    def outcome(self, task, expert, res):
        if task == "classify" and res.output["injection_p"] >= self.cfg["thresholds"]["injection"]:
            return "quarantined"
        if task == "extract" and expert == self.cfg["models"]["extract"] and res.confidence == 0.0:
            return "escalated"
        return "valid"

    def log(self, task, expert, reason, version, res, started, cost=0.0, tokens=(0, 0), outcome=None, pii=False):
        call_id = uuid.uuid4().hex[:12]
        self.db.run_sql(
            task,
            "INSERT INTO model_usage_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (call_id, self.db.run_id, task, task, expert, reason, version,
             tokens[0], tokens[1], round((time.perf_counter() - started) * 1000),
             res.confidence if res else None, round(cost, 6), int(pii),
             outcome or self.outcome(task, expert, res), now()),
        )
        return call_id

    def incident(self, kind, detail):
        self.db.run_sql(
            "incident",
            "INSERT INTO incident_log (incident_id, run_id, kind, detail, status, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], self.db.run_id, kind, detail, "open", now()),
        )

    def review(self, row, reason, detail):
        self.db.run_sql(
            "review",
            "INSERT INTO review_queue (review_id, run_id, file_name, lot_id, reason, detail, status, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], self.db.run_id, self.db.file_name, row["lot_id"], reason, detail, "open", now()),
        )


# ---------- the two pipeline steps for one landed file ----------

def classify_records(db, records, en):
    step = db.start_step("classify")
    threshold = en.cfg["thresholds"]
    rows = []
    for rec in records:
        note = redact(note_of(rec))
        res, expert, call_id = en.call("classify", note)
        if res is None:  # the model failed: fall back to rules so the record is still classified
            res, expert, call_id = en.call("classify", note, forced=("rules", "model call failed"))
        out = res.output
        row = {"lot_id": lot_of(rec), "note": note, **{k: out[k] for k in (
            "category", "category_confidence", "urgency", "urgency_confidence", "injection_p")},
            "classify_expert": expert, "classify_call_id": call_id, "status": "ok"}

        if out["injection_p"] >= threshold["injection"]:
            row["status"] = "quarantined"
            en.incident("injection", f"{db.file_name} {row['lot_id']}: note tries to instruct the "
                                     f"system (p={out['injection_p']:.2f}); record quarantined, not obeyed")
        elif out["category_confidence"] is not None and out["category_confidence"] < threshold["review_confidence"]:
            row["status"] = "review"
            en.review(row, "low_confidence", f"category {out['category']} at confidence "
                                             f"{out['category_confidence']:.2f} < {threshold['review_confidence']}")
        elif out["category_confidence"] is None and expert == "rules" and note:
            row["status"] = "review"
            en.review(row, "rules_fallback", f"category {out['category']} guessed by keyword rules")
        rows.append(row)

    counts = {s: sum(r["status"] == s for r in rows) for s in ("ok", "review", "quarantined")}
    db.finish_step(step, "ok", f"{len(rows)} notes: {counts['ok']} ok, {counts['review']} to review, "
                               f"{counts['quarantined']} quarantined",
                   rows_in=len(rows), rows_out=len(rows) - counts["quarantined"],
                   rows_rejected=counts["quarantined"])
    return rows


def extract_records(db, rows, en):
    step = db.start_step("extract")
    extracted = escalated = 0
    for row in rows:
        row["extract_json"] = row["extract_expert"] = row["extract_call_id"] = None
        if row["status"] == "quarantined" or not row["note"]:
            continue  # never send injected text onward; nothing to extract from an empty note

        res, expert, call_id = en.call("extract", row["note"])
        if res is None and expert == en.cfg["models"]["extract"]:  # validation failed: retry once
            res, expert, call_id = en.call("extract", row["note"])
        if expert == en.cfg["models"]["extract"] and (res is None or res.confidence == 0.0):
            why = "validation failed twice" if res is None else "low confidence"
            res, expert, call_id = en.call("extract", row["note"], escalate=why)
            escalated += 1
        if res is None:
            res, expert, call_id = en.call("extract", row["note"], forced=("rules", "model calls failed"))
        if res.confidence == 0.0 and row["status"] == "ok":
            row["status"] = "review"
            en.review(row, "extract_low_confidence", f"{expert} not sure of extracted values")

        row.update(extract_json=json.dumps(res.output), extract_expert=expert, extract_call_id=call_id)
        extracted += 1

    for row in rows:
        db.run_sql(
            "extract",
            "INSERT INTO record_enrichment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (db.run_id, db.file_name, row["lot_id"], row["note"], row["category"],
             row["category_confidence"], row["urgency"], row["urgency_confidence"], row["injection_p"],
             row["classify_expert"], row["classify_call_id"], row["extract_json"],
             row["extract_expert"], row["extract_call_id"], row["status"], now()),
        )
    db.finish_step(step, "ok", f"{extracted} notes extracted, {escalated} escalated",
                   rows_in=len(rows), rows_out=extracted)
    return rows


def enrich_file(db, raw, en):
    """Run Classify then Extract on the records landed by db.run_id."""
    records = raw.search(Query()._run_id == db.run_id)
    en.start_run()

    step = db.start_step("redact")
    masked = sum(has_mask(redact(note_of(r))) for r in records)
    db.finish_step(step, "ok", f"{masked} of {len(records)} notes had personal data masked before any model call",
                   rows_in=len(records), rows_out=len(records), rows_rejected=0)

    rows = classify_records(db, records, en)
    return extract_records(db, rows, en)
