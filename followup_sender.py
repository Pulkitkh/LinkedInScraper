"""Threaded follow-ups for unanswered outreach (In-Reply-To / References so Gmail keeps one thread)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import client_config
import csv_store
import gmail_sender
import llm_client
import resume_customizer
import settings
from gmail_sender import (
    SEND_DELAY_SECONDS, OutreachReport, attachment_filename, build_message, extract_jd_keywords,
    load_template, recipients_for, render, template_values,
)

log = logging.getLogger(__name__)

FOLLOWUP_AFTER_DAYS = 7
VALUE_PROP_SYSTEM = (
    "You write one or two concise, specific sentences for a contractor's follow-up email to a recruiter, "
    "linking the candidate's core skills to the job description. No greeting, no sign-off, no placeholders."
)


def eligible_followups(days: int = FOLLOWUP_AFTER_DAYS, now: datetime | None = None) -> list[dict]:
    """Sent, never followed up, no reply, and sent at least `days` days ago."""
    cutoff = (now or datetime.now()) - timedelta(days=days)
    rows = []
    for row in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS):
        sent_at = csv_store.parse_time(row["timestamp"])
        if (row["email_sent_status"] == "sent" and str(row["followup_count"] or "0") == "0"
                and str(row["replied"] or "0") == "0" and row["message_id"] and sent_at and sent_at <= cutoff):
            rows.append(row)
    return rows


def _job_description(post_url: str) -> str:
    for row in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS):
        if row["post_url"] == post_url:
            return row["job_description"]
    return ""


def value_proposition(candidate: dict, job_description: str, generate=llm_client.generate) -> str:
    keywords = extract_jd_keywords(job_description)
    fallback = (f"With {candidate.get('experience', 'solid')} experience"
                + (f" across {', '.join(keywords)}" if keywords else "")
                + f", I'm confident I can make an immediate impact as your {candidate['primary_role']}.")
    if not job_description:
        return fallback
    prompt = (f"Candidate: {candidate['name']}, {candidate['primary_role']}, {candidate.get('experience', '')}.\n"
              f"Job description:\n{job_description[:4000]}")
    try:
        text = generate(VALUE_PROP_SYSTEM, prompt, llm_client.VALUE_PROP_CHAIN)
        return " ".join(text.split())[:400] or fallback
    except llm_client.LLMError as exc:
        log.warning("Value proposition fallback used: %s", exc)
        return fallback


def _resume_pdf(candidate: dict):
    resume = client_config.resolve_resume(candidate)
    if resume is None:
        return None
    try:
        return resume_customizer.ensure_pdf(resume)
    except Exception:  # conversion problems must not block the follow-up
        log.exception("PDF conversion failed; attaching %s as-is", resume.name)
        return resume


def _candidate_by_name(name: str) -> dict | None:
    for key in client_config.candidate_keys():
        c = client_config.get_candidate(key)
        if c["name"] == name:
            return c
    return None


def send_followups(mode: str = "test", days: int = FOLLOWUP_AFTER_DAYS, limit: int | None = None,
                   smtp_factory=None, sleep=time.sleep, generate=llm_client.generate,
                   now: datetime | None = None) -> OutreachReport:
    report = OutreachReport()
    smtp_factory = smtp_factory or gmail_sender.smtp_connect
    sender = settings.env("GMAIL_EMAIL")
    _, body_tpl = load_template("followup_email.txt")
    session = gmail_sender.SmtpSession(smtp_factory)
    live = mode == "live"
    try:
        for row in eligible_followups(days, now):
            email = row["recruiter_email"]
            candidate = _candidate_by_name(row["candidate"])
            if candidate is None:
                report.skipped += 1
                report.details.append((email, "skipped", f"unknown candidate {row['candidate']!r}"))
                continue
            if limit is not None and report.sent >= limit:
                report.skipped += 1
                report.details.append((email, "skipped", "limit reached"))
                continue
            jd = _job_description(row["post_url"])
            values = template_values({**row, "job_description": jd}, candidate,
                                     value_prop=value_proposition(candidate, jd, generate))
            body = render(body_tpl, values)
            original = row["subject"] or f"{values['job_title']} (C2C) - {candidate['name']}"
            subject = original if original.lower().startswith("re:") else f"Re: {original}"
            attachment = _resume_pdf(candidate)
            cc, bcc = recipients_for(candidate)
            msg = build_message(
                sender=sender, sender_name=candidate["name"], to=email if live else sender,
                subject=subject if live else f"[TEST -> {email}] {subject}", body=body,
                attachment=attachment,
                attachment_name=attachment_filename(candidate["name"], attachment.suffix) if attachment else None,
                cc=cc if live else [], bcc=bcc if live else [], in_reply_to=row["message_id"],
            )
            if report.sent or report.failed:
                sleep(SEND_DELAY_SECONDS)
            status, error, stop = gmail_sender.deliver(session, msg)
            report.details.append((email, status, error or "follow-up"))
            if status != "sent":
                report.failed += 1
                if stop:
                    report.aborted = error
                    break
                continue
            report.sent += 1
            if live:
                csv_store.update_rows(
                    settings.history_csv(), csv_store.HISTORY_FIELDS,
                    lambda r, row=row: r["message_id"] == row["message_id"],
                    lambda r: {"followup_count": str(int(r["followup_count"] or 0) + 1),
                               "last_followup_at": csv_store.now()},
                )
            log.info("%s follow-up sent -> %s", mode.upper(), email)
    except gmail_sender.SendAborted as exc:
        report.aborted = str(exc)
        log.error("Follow-ups stopped: %s", exc)
    finally:
        session.close()
    return report
