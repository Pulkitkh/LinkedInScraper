"""Scan the Gmail inbox over IMAP for replies from contacted recruiters and mark them replied=1."""

from __future__ import annotations

import email
import imaplib
import logging
from datetime import datetime, timedelta
from email.utils import getaddresses

import csv_store
import settings

log = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
BATCH_SIZE = 100
DEFAULT_LOOKBACK_DAYS = 30


def search_since(now: datetime | None = None) -> datetime:
    """Timestamp of the oldest sent-but-unreplied email, or 30 days ago when there is none."""
    pending = [csv_store.parse_time(r["timestamp"])
               for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
               if r["email_sent_status"] == "sent" and str(r["replied"] or "0") == "0"]
    pending = [t for t in pending if t]
    return min(pending) if pending else (now or datetime.now()) - timedelta(days=DEFAULT_LOOKBACK_DAYS)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_date(dt: datetime) -> str:
    """SINCE 12-Jun-2026. Month names are fixed English: strftime('%b') is locale-dependent on Windows."""
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def fetch_sender_addresses(conn, since: datetime) -> set[str]:
    conn.select("INBOX", readonly=True)
    status, data = conn.search(None, "SINCE", imap_date(since))
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    ids = data[0].split() if data and data[0] else []
    senders: set[str] = set()
    for i in range(0, len(ids), BATCH_SIZE):
        batch = b",".join(ids[i:i + BATCH_SIZE]).decode()
        status, parts = conn.fetch(batch, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if status != "OK":
            continue
        for part in parts:
            if isinstance(part, tuple) and len(part) > 1:
                msg = email.message_from_bytes(part[1])
                for _, addr in getaddresses(msg.get_all("From", [])):
                    if addr:
                        senders.add(addr.strip().lower())
    return senders


def connect() -> imaplib.IMAP4_SSL:
    user, password = settings.env("GMAIL_EMAIL"), settings.env("GMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and password):
        raise RuntimeError("Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env")
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    conn.login(user, password)
    return conn


def check_replies(imap_factory=None) -> int:
    """Return how many history rows were newly marked as replied."""
    since = search_since()
    conn = (imap_factory or connect)()
    try:
        senders = fetch_sender_addresses(conn, since)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    log.info("Scanned inbox since %s: %d distinct senders", imap_date(since), len(senders))
    updated = csv_store.update_rows(
        settings.history_csv(), csv_store.HISTORY_FIELDS,
        lambda r: str(r["replied"] or "0") == "0" and r["email_sent_status"] == "sent"
        and r["recruiter_email"].lower() in senders,
        {"replied": "1"},
    )
    log.info("Marked %d outreach records as replied", updated)
    return updated
