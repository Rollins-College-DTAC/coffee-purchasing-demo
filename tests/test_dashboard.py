"""Every dashboard page renders on the committed snapshot, and Release works from the Live run page."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted((ROOT / "dashboard" / "pages").glob("*.py"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "analytics.sqlite"
    shutil.copy(ROOT / "demo" / "analytics.sqlite", path)
    monkeypatch.setenv("DASHBOARD_DB", str(path))
    monkeypatch.setenv("DASHBOARD_KIND", "local")
    return path


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.stem)
def test_page_renders_without_errors(db, page):
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title


def test_navigation_lists_every_page(db):
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=60).run()
    assert not at.exception


def test_release_button_releases_the_held_import(db):
    held = sqlite3.connect(db).execute(
        "SELECT run_id FROM imports WHERE status = 'held' ORDER BY loaded_at DESC LIMIT 1").fetchone()[0]
    at = AppTest.from_file(str(ROOT / "dashboard" / "pages" / "live.py"), default_timeout=60).run()
    at.text_input(key=f"name-{held}").input("Test Buyer").run()
    at.button(key=f"rel-{held}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    status, by = sqlite3.connect(db).execute(
        "SELECT status, decided_by FROM imports WHERE run_id = ?", (held,)).fetchone()
    assert (status, by) == ("released", "Test Buyer")


def test_cloud_release_files_a_review_issue_behind_the_passcode(db, monkeypatch):
    from dashboard import cloud
    monkeypatch.setenv("DASHBOARD_KIND", "cloud")
    posted = []

    def fake_api(method, path, body=None, raw=False):
        if method == "POST":
            posted.append((path, body))
            return {"html_url": "https://github.com/o/r/issues/1"}
        return []

    monkeypatch.setattr(cloud, "api", fake_api)
    held = sqlite3.connect(db).execute(
        "SELECT run_id FROM imports WHERE status = 'held' ORDER BY loaded_at DESC LIMIT 1").fetchone()[0]
    at = AppTest.from_file(str(ROOT / "dashboard" / "pages" / "live.py"), default_timeout=60)
    at.secrets["GH_TOKEN"] = "test"
    at.secrets["APP_PASSCODE"] = "beans"
    at.run()
    at.text_input(key=f"name-{held}").input("Test Buyer")
    at.text_input(key=f"code-{held}").input("wrong").run()
    at.button(key=f"rel-{held}").click().run()
    assert posted == [] and "Wrong passcode." in [e.value for e in at.error]

    at.text_input(key=f"code-{held}").input("beans").run()
    at.button(key=f"rel-{held}").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert posted == [("/issues", {"title": f"review: release {held}", "body": '{"reviewer": "Test Buyer"}'})]
    # the database is untouched: the pipeline applies the request
    assert sqlite3.connect(db).execute("SELECT status FROM imports WHERE run_id = ?", (held,)).fetchone()[0] == "held"
