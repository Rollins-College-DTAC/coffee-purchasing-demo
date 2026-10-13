import csv
import json
import random
from datetime import datetime, timedelta

from faker import Faker

from generator.generate import ORIGINS, build_import, write_import

T0 = datetime(2026, 10, 13, 9, 0)
SUPPLIERS = ["Test Green Coffee Importers"]


def read_log(path):
    with path.open() as f:
        return list(csv.DictReader(f))


def test_one_file_per_minute_alternating_and_logged(tmp_path):
    inbox, log = tmp_path / "inbox", tmp_path / "generator_log.csv"
    rng = random.Random(1)
    first = write_import(inbox, log, T0, rng, SUPPLIERS, anomaly=None, resend=False)
    second = write_import(inbox, log, T0 + timedelta(minutes=1), rng, SUPPLIERS, anomaly=None, resend=False)

    assert first == ["coffee_20261013-0900.csv"]
    assert second == ["coffee_20261013-0901.json"]
    assert sorted(p.name for p in inbox.iterdir()) == first + second
    assert [r["file_name"] for r in read_log(log)] == first + second
    assert json.loads((inbox / second[0]).read_text())["lots"]


def test_seeded_anomaly_is_logged_and_moves_the_mean(tmp_path):
    log = tmp_path / "generator_log.csv"
    write_import(tmp_path, log, T0, random.Random(2), SUPPLIERS, anomaly="spike", resend=False)
    row = read_log(log)[0]
    assert row["anomaly"] == "1" and row["anomaly_kind"] == "spike"

    _, normal = build_import(0, T0, random.Random(3), SUPPLIERS)
    _, spike = build_import(0, T0, random.Random(3), SUPPLIERS, anomaly="spike")
    assert spike > normal * 1.5


def test_anomaly_rate_is_about_one_in_ten(tmp_path):
    log, rng = tmp_path / "generator_log.csv", random.Random(4)
    for m in range(200):
        write_import(tmp_path, log, T0 + timedelta(minutes=m), rng, SUPPLIERS, resend=False)
    flagged = sum(int(r["anomaly"]) for r in read_log(log))
    assert 8 <= flagged <= 35


def test_renamed_copy_has_identical_bytes(tmp_path):
    log = tmp_path / "generator_log.csv"
    names = write_import(tmp_path, log, T0, random.Random(5), SUPPLIERS, anomaly=None, resend=True)
    assert names == ["coffee_20261013-0900.csv", "coffee_20261013-0900_resend.csv"]
    assert (tmp_path / names[0]).read_bytes() == (tmp_path / names[1]).read_bytes()
    assert read_log(log)[1]["copy_of"] == names[0]


def test_data_is_messy(tmp_path):
    rng, suppliers = random.Random(6), [f"{Faker().last_name()} Importers"]
    rows = []
    for t in range(30):
        rows += build_import(t, T0, rng, suppliers)[0]
    origins = {r.get("origin") or r.get("Origin") for r in rows}
    assert origins - set(ORIGINS), "expected non-canonical origin spellings"
    assert any("kg" in str(r.get("price_unit") or r.get("price")) for r in rows)
    assert any(r.get("cupping_score", r.get("score")) is None for r in rows)
    lot_ids = [r.get("lot_id") or r.get("lotId") for r in rows]
    assert len(lot_ids) > len(set(lot_ids)), "expected duplicate rows"


def test_price_has_a_trend():
    rng = random.Random(7)
    means = [build_import(t, T0, rng, SUPPLIERS)[1] for t in range(150)]
    assert sum(means[-30:]) > sum(means[:30])


def test_injection_lots_are_logged(tmp_path, monkeypatch):
    import generator.generate as g
    monkeypatch.setattr(g, "INJECTION_RATE", 1.0)
    log = tmp_path / "generator_log.csv"
    write_import(tmp_path, log, T0, random.Random(8), SUPPLIERS, anomaly=None, resend=False)
    lots = read_log(log)[0]["injection_lots"].split(";")
    assert lots and all(lot.startswith("LOT-0000-") for lot in lots)


def test_notes_vary_so_live_runs_are_not_all_cache_hits():
    rng = random.Random(9)
    notes = [r.get("notes") or r.get("buyer_notes") for t in range(20)
             for r in build_import(t, T0, rng, SUPPLIERS)[0]]
    assert len(set(notes)) > 60
