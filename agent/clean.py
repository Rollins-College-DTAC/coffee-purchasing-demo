"""Clean and Load (Milestone 4): raw TinyDB records -> typed rows in the SQLite `purchases` table.

The raw layer keeps every record as it arrived. Here each one is mapped to one shape:
canonical origin, supplier casing, quantity in lbs, price in $/lb, ISO harvest date,
plus the Jev/Claude answers from record_enrichment. Records that fail validation are
rejected (counted and logged); quarantined injection records are never loaded.
"""

import json
import re
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError
from tinydb import Query

from agent.db import now
from agent.enrich import lot_of, note_of, redact

LB_PER_KG = 2.20462
BAG_KG = 69

# The agent's own alias table: first matching keyword wins
ORIGIN_KEYWORDS = [
    ("Ethiopia", ("ethiop", "eth")), ("Colombia", ("colomb", "columb", "col")),
    ("Brazil", ("brazil", "brasil", "bra")), ("Guatemala", ("guatemala", "gua")),
    ("Kenya", ("kenya", "ken")), ("Sumatra", ("sumatra", "indonesia", "idn")),
]
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%b %Y"]
NUMBER_UNIT = re.compile(r"([\d,]*\.?\d+)\s*(.*)")


class Purchase(BaseModel):
    lot_id: str
    origin: str
    supplier: str
    quantity_lbs: float = Field(gt=0)
    price_per_lb: float = Field(ge=0.5, le=30)
    cupping_score: float | None = Field(default=None, ge=60, le=100)
    harvest_date: str | None = None


def origin_of(text):
    t = (text or "").lower()
    first = t.split()[0] if t.split() else ""
    for name, keys in ORIGIN_KEYWORDS:
        if first in keys or any(k in t for k in keys if len(k) > 3):
            return name
    raise ValueError(f"unknown origin {text!r}")


def number_unit(value, unit=None):
    """'9,279 lbs' -> (9279.0, 'lbs'); (4.85, '$/lb') -> (4.85, '$/lb')."""
    if unit is not None:
        return float(str(value).replace(",", "")), unit.lower()
    m = NUMBER_UNIT.match(str(value).strip())
    if not m:
        raise ValueError(f"no number in {value!r}")
    return float(m.group(1).replace(",", "")), m.group(2).strip().lower()


def to_lbs(value, unit):
    if "bag" in unit:
        return value * BAG_KG * LB_PER_KG
    if "kg" in unit:
        return value * LB_PER_KG
    if "lb" in unit:
        return value
    raise ValueError(f"unknown quantity unit {unit!r}")


def to_price_per_lb(value, unit):
    if "kg" in unit:
        return value / LB_PER_KG
    if "lb" in unit:
        return value
    raise ValueError(f"unknown price unit {unit!r}")


def to_iso_date(text):
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"unknown date format {text!r}")


def blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def clean_record(r):
    """Map one raw CSV or JSON record to a validated Purchase. Raises ValueError/ValidationError."""
    if "lot_id" in r:  # CSV shape
        qty, qty_unit = number_unit(r["quantity"], r["quantity_unit"])
        price, price_unit = number_unit(r["price"], r["price_unit"])
        origin, supplier, score, harvest = r["origin"], r["supplier"], r["cupping_score"], r["harvest_date"]
    else:  # JSON shape
        qty, qty_unit = number_unit(r["qty"])
        price, price_unit = number_unit(r["price"])
        origin, supplier, score, harvest = r["Origin"], r["Vendor"], r["score"], r["harvest"]
    return Purchase(
        lot_id=lot_of(r), origin=origin_of(origin), supplier=" ".join(supplier.split()).title(),
        quantity_lbs=round(to_lbs(qty, qty_unit), 1),
        price_per_lb=round(to_price_per_lb(price, price_unit), 3),
        cupping_score=None if blank(score) else float(score),
        harvest_date=None if blank(harvest) else to_iso_date(harvest),
    )


def load_file(db, raw):
    """Clean the records landed by db.run_id and load them into SQLite. Returns the import's row."""
    step = db.start_step("load")
    enrichment = {
        r["lot_id"]: dict(r) for r in db.run_sql(
            "load", "SELECT * FROM record_enrichment WHERE run_id = ?", (db.run_id,))
    }
    seen, rejected, quarantined, loaded = set(), [], 0, []
    for rec in raw.search(Query()._run_id == db.run_id):
        lot = lot_of(rec)
        if lot in seen:  # duplicate row inside the file
            continue
        seen.add(lot)
        enr = enrichment.get(lot, {})
        if enr.get("status") == "quarantined":
            quarantined += 1
            continue
        try:
            p = clean_record(rec)
        except (ValueError, KeyError, ValidationError) as err:
            rejected.append(f"{lot}: {str(err).splitlines()[0]}")
            continue
        extract = json.loads(enr["extract_json"]) if enr.get("extract_json") else {}
        db.run_sql(
            "load",
            "INSERT OR REPLACE INTO purchases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (db.run_id, db.file_name, p.lot_id, p.origin, p.supplier, p.quantity_lbs, p.price_per_lb,
             p.cupping_score, p.harvest_date, redact(note_of(rec)), enr.get("category"), enr.get("urgency"),
             extract.get("freight_premium_usd_per_lb"), extract.get("discount_pct"),
             extract.get("delay_weeks"), enr.get("status", "ok"), now()),
        )
        loaded.append(p.price_per_lb)

    mean = round(sum(loaded) / len(loaded), 4) if loaded else None
    db.run_sql(
        "load",
        "INSERT OR REPLACE INTO imports (run_id, file_name, n_lots, mean_price_per_lb, status, loaded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (db.run_id, db.file_name, len(loaded), mean, "pending", now()),
    )
    message = f"{len(loaded)} lots loaded to SQLite, {len(rejected)} rejected, {quarantined} quarantined"
    if rejected:
        message += "; " + "; ".join(rejected[:3])
    db.finish_step(step, "warning" if rejected else "ok", message,
                   rows_in=len(seen), rows_out=len(loaded), rows_rejected=len(rejected) + quarantined)
    return {"run_id": db.run_id, "n_lots": len(loaded), "mean_price_per_lb": mean}
