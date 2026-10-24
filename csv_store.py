"""Flat CSV databases (recruiters.csv, email_sent_history.csv) with runtime header migration."""

from __future__ import annotations

import csv
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

RECRUITER_FIELDS = [
    "recruiter_name", "recruiter_email", "role", "post_url",
    "job_description", "email_sent_status", "timestamp",
]
HISTORY_FIELDS = [
    "recruiter_name", "recruiter_email", "role", "post_url", "candidate", "email_sent_status",
    "timestamp", "subject", "message_id", "followup_count", "last_followup_at", "replied",
]
DEFAULTS = {"followup_count": "0", "replied": "0", "email_sent_status": "pending"}
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()


def now() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def parse_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), TIME_FORMAT)
    except (ValueError, AttributeError):
        return None


def read_rows(path: Path, fields: list[str]) -> list[dict]:
    """Read every row, adding any missing columns with defaults so older files keep working."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for field in fields:
            if row.get(field) is None:
                row[field] = DEFAULTS.get(field, "")
    return rows


def write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Atomically rewrite the file with the canonical header (migrates old headers on save)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = [k for row in rows for k in row if k not in fields and k is not None]
    header = fields + list(dict.fromkeys(extra))
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, DEFAULTS.get(k, "")) for k in header})
    os.replace(tmp, path)


def migrate(path: Path, fields: list[str]) -> bool:
    """Rewrite the file if its header is missing any canonical column. Returns True if it changed."""
    if not path.exists():
        write_rows(path, fields, [])
        return True
    with open(path, newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh), [])
    if all(f in header for f in fields):
        return False
    with _lock:
        write_rows(path, fields, read_rows(path, fields))
    return True


def append_rows(path: Path, fields: list[str], new_rows: list[dict]) -> None:
    with _lock:
        rows = read_rows(path, fields)
        rows.extend(new_rows)
        write_rows(path, fields, rows)


def update_rows(path: Path, fields: list[str], match, changes) -> int:
    """Apply `changes(row)` (a dict of new values) to every row where `match(row)` is true."""
    with _lock:
        rows = read_rows(path, fields)
        count = 0
        for row in rows:
            if match(row):
                row.update(changes(row) if callable(changes) else changes)
                count += 1
        if count:
            write_rows(path, fields, rows)
        return count
