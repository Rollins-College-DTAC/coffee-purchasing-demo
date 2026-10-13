"""Paths for the agent. Override with environment variables so the same code runs
on a laptop, in Docker and on a GitHub Actions runner."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = Path(os.environ.get("INBOX_DIR", ROOT / "inbox"))
OUT = Path(os.environ.get("OUT_DIR", ROOT))  # processed/ and quarantine/ live here
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
SQLITE_PATH = DATA / "analytics.sqlite"
RAW_PATH = DATA / "raw.json"
