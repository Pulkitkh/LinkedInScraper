"""Environment variables and workspace paths shared by every module."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def show_browser() -> bool:
    """SHOW_BROWSER=true in .env: scraping runs in a visible browser window (useful for demos)."""
    return env("SHOW_BROWSER").lower() in ("1", "true", "yes", "y", "on")


def data_dir() -> Path:
    """Where session.json and the CSV databases live. Override with DATA_DIR (used by the tests)."""
    return Path(env("DATA_DIR", str(ROOT)))


def session_file() -> Path:
    return data_dir() / "session.json"


def recruiters_csv() -> Path:
    return data_dir() / "recruiters.csv"


def history_csv() -> Path:
    return data_dir() / "email_sent_history.csv"


def scratch_dir() -> Path:
    path = data_dir() / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def analytics_report() -> Path:
    return data_dir() / "analytics_report.html"
