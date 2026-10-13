"""Fake raw (green) coffee purchase imports: one file into inbox/ every minute.

Simulated data only. Every file is messy on purpose so the ETL agent has real work:
origin aliases, $/lb vs $/kg prices, lbs/kg/bags quantities, mixed date formats,
missing values, free-text notes, duplicate rows and renamed copies of files.

Run:  uv run python generator/generate.py --minutes 5
"""

import argparse
import csv
import json
import math
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from faker import Faker

ROOT = Path(__file__).resolve().parent.parent
INBOX = Path(os.environ.get("INBOX_DIR", ROOT / "inbox"))
LOG = Path(os.environ.get("GENERATOR_LOG", ROOT / "generator_log.csv"))

LB_PER_KG = 2.20462
BAG_KG = 69  # standard green-coffee export bag

# Canonical origin -> (base $/lb, the messy ways it gets written)
ORIGINS = {
    "Ethiopia": (5.80, ["Ethiopia", "ETH", "ethiopian yirgacheffe", "Ethiopia - Guji"]),
    "Colombia": (4.60, ["Colombia", "COL", "colombia huila", "Columbia"]),
    "Brazil": (3.40, ["Brazil", "BRA", "brasil cerrado", "Brazil Santos"]),
    "Guatemala": (4.90, ["Guatemala", "GUA", "guatemala antigua"]),
    "Kenya": (6.50, ["Kenya", "KEN", "kenya AA", "Kenya Nyeri"]),
    "Sumatra": (4.20, ["Sumatra", "Indonesia", "sumatra mandheling", "IDN"]),
}

NOTES = [
    "Washed, floral, jasmine and bergamot on the cupping table",
    "Natural process, heavy blueberry, a bit fermenty",
    "Container delayed at port, ETA +2 weeks",
    "price includes $0.25/lb freight premium",
    "Second lot from this farm, consistent with last year",
    "Moisture 11.2%, within spec",
    "Some quakers in sample roast, ask for replacement sample",
    "Fair Trade + organic certified",
    "Supplier offered 3% discount if paid within 10 days",
    "chocolate, nutty, low acidity - good for espresso blend",
    "",
]

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%b %Y"]
ANOMALY_RATE = 0.10
RESEND_RATE = 0.05
LOG_FIELDS = ["file_name", "written_at", "format", "n_records", "import_index",
              "anomaly", "anomaly_kind", "true_mean_price_per_lb", "copy_of"]


def make_suppliers(fake):
    """A small fixed pool of fake importers; casing varies per record."""
    return [f"{fake.last_name()} Green Coffee Importers" for _ in range(5)]


def price_level(t):
    """Market multiplier for import t: upward trend x 15-minute cycle."""
    return (1 + 0.002 * t) * (1 + 0.06 * math.sin(2 * math.pi * t / 15))


def build_import(t, now, rng, suppliers, anomaly=None):
    """Return (records, true_mean_price_per_lb) for import number t.

    anomaly: None, "spike", "drop" or "mix_shift" (every lot is pricey Kenya).
    """
    is_csv = t % 2 == 0
    mult = {"spike": 1.6, "drop": 0.55}.get(anomaly, 1.0)
    records, true_prices = [], []

    for i in range(rng.randint(5, 10)):
        origin = "Kenya" if anomaly == "mix_shift" else rng.choice(list(ORIGINS))
        base, aliases = ORIGINS[origin]
        price_lb = round(base * price_level(t) * rng.gauss(1, 0.04) * mult, 2)
        true_prices.append(price_lb)

        bags = rng.randint(5, 250)
        qty_style = rng.choice(["bags", "kg", "lbs"])
        qty_value = {"bags": bags, "kg": bags * BAG_KG, "lbs": round(bags * BAG_KG * LB_PER_KG)}[qty_style]

        per_kg = rng.random() < 0.3
        price = round(price_lb * LB_PER_KG, 2) if per_kg else price_lb
        price_unit = rng.choice(["USD/kg", "$/kg"]) if per_kg else rng.choice(["USD/lb", "$/lb", "per lb"])

        harvest = None
        if rng.random() > 0.08:
            harvest = (now - timedelta(days=rng.randint(60, 300))).strftime(rng.choice(DATE_FORMATS))
        score = round(rng.uniform(80, 89.5), 2) if rng.random() > 0.10 else None

        supplier = rng.choice(suppliers)
        supplier = rng.choice([supplier, supplier.upper(), supplier.lower()])
        lot_id = f"LOT-{t:04d}-{i:02d}"

        if is_csv:
            records.append({
                "lot_id": lot_id, "origin": rng.choice(aliases), "supplier": supplier,
                "quantity": qty_value, "quantity_unit": qty_style,
                "price": price, "price_unit": price_unit,
                "cupping_score": score, "harvest_date": harvest, "notes": rng.choice(NOTES),
            })
        else:  # JSON uses different field names and packs units into strings
            records.append({
                "lotId": lot_id, "Origin": rng.choice(aliases), "Vendor": supplier,
                "qty": f"{qty_value:,} {qty_style}", "price": f"{price} {price_unit}",
                "score": score, "harvest": harvest, "buyer_notes": rng.choice(NOTES),
            })

    if rng.random() < 0.15:  # duplicate row
        records.append(dict(rng.choice(records)))

    return records, round(sum(true_prices) / len(true_prices), 3)


def write_file(path, records):
    """Write atomically: a dot-prefixed temp file is renamed into place, so the
    agent never reads a half-written import (it ignores dot files)."""
    tmp = path.with_name(f".{path.name}.tmp")
    if path.suffix == ".csv":
        with tmp.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    else:
        tmp.write_text(json.dumps({"exported_at": datetime.now().isoformat(), "lots": records}, indent=2))
    os.replace(tmp, path)


def next_import_index(log_path):
    """Continue the trend across restarts: count original imports already logged."""
    if not log_path.exists():
        return 0
    with log_path.open() as f:
        return sum(1 for row in csv.DictReader(f) if not row["copy_of"])


def append_log(log_path, row):
    new = not log_path.exists()
    with log_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def write_import(inbox, log_path, now, rng, suppliers, anomaly="random", resend="random"):
    """Write one import into inbox and log it. Returns the list of file names written."""
    inbox.mkdir(parents=True, exist_ok=True)
    t = next_import_index(log_path)
    if anomaly == "random":  # first 5 imports are a clean baseline
        anomaly = rng.choice(["spike", "drop", "mix_shift"]) if t >= 5 and rng.random() < ANOMALY_RATE else None
    if resend == "random":
        resend = rng.random() < RESEND_RATE

    records, true_mean = build_import(t, now, rng, suppliers, anomaly)
    ext = "csv" if t % 2 == 0 else "json"
    path = inbox / f"coffee_{now:%Y%m%d-%H%M}.{ext}"
    if path.exists():  # restarted within the same minute
        path = inbox / f"coffee_{now:%Y%m%d-%H%M%S}.{ext}"
    write_file(path, records)

    row = {"file_name": path.name, "written_at": now.isoformat(timespec="seconds"),
           "format": ext, "n_records": len(records), "import_index": t,
           "anomaly": int(anomaly is not None), "anomaly_kind": anomaly or "",
           "true_mean_price_per_lb": true_mean, "copy_of": ""}
    append_log(log_path, row)
    names = [path.name]

    if resend:  # supplier re-sends the same file under a new name; the ETL must skip it by hash
        copy = path.with_name(f"{path.stem}_resend{path.suffix}")
        copy.write_bytes(path.read_bytes())
        append_log(log_path, {**row, "file_name": copy.name, "copy_of": path.name})
        names.append(copy.name)

    return names


def main():
    parser = argparse.ArgumentParser(description="Write one fake coffee purchase import per minute.")
    parser.add_argument("--minutes", type=int, default=10, help="stop after N imports (default 10)")
    parser.add_argument("--seed", type=int, help="random seed for a repeatable run")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    fake = Faker()
    fake.seed_instance(args.seed)
    suppliers = make_suppliers(fake)

    for n in range(args.minutes):
        started = time.monotonic()
        for name in write_import(INBOX, LOG, datetime.now(), rng, suppliers):
            print(f"[generator] wrote {name}", flush=True)
        if n < args.minutes - 1:
            time.sleep(max(0.0, 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
