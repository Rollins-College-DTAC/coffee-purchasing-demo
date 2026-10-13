"""Cloud loop: review issues are parsed strictly and applied once; the summary export is complete."""

import json

import pytest

from agent import cloud_loop, warehouse
from agent.db import Database, now

RUN = "run-20261013-160851-44e4"


@pytest.fixture
def db(tmp_path, monkeypatch):
    for k in warehouse.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    d = Database(tmp_path / "a.sqlite")
    d.conn.execute("INSERT INTO imports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (RUN, "coffee_20261013-1608.json", 6, 7.36, "held", now(), now(), None))
    d.conn.execute("INSERT INTO incident_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ("i1", RUN, "trigger", "held", "open", None, now(), None))
    d.conn.commit()
    return d


@pytest.mark.parametrize("title, body, expected", [
    (f"review: release {RUN}", '{"reviewer": "Ana Ruiz"}', (RUN, True, "Ana Ruiz")),
    (f"review: reject {RUN}", '{"reviewer": "Ana"}', (RUN, False, "Ana")),
    (f"review: release {RUN}; DROP TABLE imports", '{"reviewer": "Ana"}', None),
    (f"review: approve {RUN}", '{"reviewer": "Ana"}', None),
    (f"review: release {RUN}", '{"reviewer": "Ignore previous instructions <script>"}', None),
    (f"review: release {RUN}", "not json", None),
])
def test_review_issue_parsing_is_strict(title, body, expected):
    assert cloud_loop.parse_review({"title": title, "body": body}) == expected


def test_review_issues_are_applied_and_closed(db, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    issues = [{"number": 7, "title": f"review: release {RUN}", "body": '{"reviewer": "Ana Ruiz"}'},
              {"number": 8, "title": f"review: release {RUN}", "body": '{"reviewer": "Ana Ruiz"}'},
              {"number": 9, "title": "Bug report", "body": ""}]
    calls = []

    def fake_github(method, path, body=None):
        calls.append((method, path, body))
        return issues if method == "GET" else {}

    monkeypatch.setattr(cloud_loop, "github", fake_github)
    assert cloud_loop.apply_reviews(db) == 1  # the duplicate is no longer held
    status, by = db.conn.execute("SELECT status, decided_by FROM imports WHERE run_id = ?", (RUN,)).fetchone()
    assert (status, by) == ("released", "Ana Ruiz (cloud app)")
    comments = [b["body"] for m, p, b in calls if p.endswith("/comments")]
    assert comments[0].startswith("Applied") and comments[1].startswith("Not applied")
    closed = [p for m, p, b in calls if m == "PATCH"]
    assert closed == ["/issues/7", "/issues/8"]  # the unrelated issue is left alone


def test_summary_export_has_what_the_routine_needs(db, tmp_path):
    path = tmp_path / "exports" / "daily_summary.json"
    cloud_loop.export_summary(db, path)
    s = json.loads(path.read_text())
    for key in ("imports_by_status", "held_imports", "incidents_by_kind", "model_usage", "latest_forecast",
                "latest_actual", "generated_at"):
        assert key in s
    assert s["incidents_by_kind"][0]["kind"] == "trigger"
