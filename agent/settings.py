"""Paths and settings for the agent. Override paths with environment variables so the same
code runs on a laptop, in Docker and on a GitHub Actions runner. Model names, thresholds and
the budget live in config.toml; keys come from .env (locally) or the environment (cloud)."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = Path(os.environ.get("INBOX_DIR", ROOT / "inbox"))
OUT = Path(os.environ.get("OUT_DIR", ROOT))  # processed/ and quarantine/ live here
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
SQLITE_PATH = DATA / "analytics.sqlite"
RAW_PATH = DATA / "raw.json"
PROMPTS = ROOT / "prompts"


def load_env(path=ROOT / ".env"):
    """Copy KEY=value lines from .env into os.environ without overriding real env vars.
    Values are never printed or logged."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


load_env()
CONFIG = tomllib.loads((ROOT / "config.toml").read_text())
