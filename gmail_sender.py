"""Personalized C2C outreach over Gmail SMTP: JD keyword hooks, tailored resume attachments, de-duplication."""

from __future__ import annotations

import logging
import mimetypes
import re
import smtplib
import string
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import client_config
import csv_store
import resume_customizer
import settings
from linkedin_scraper import detect_job_title, pending_recruiters

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SEND_DELAY_SECONDS = 3
TEMPLATES = settings.ROOT / "templates"

# 60+ technology keywords scanned in each job description; the top 3 build the opening hook.
JD_KEYWORDS = [
    "Java", "Spring Boot", "Spring", "Spring MVC", "Spring Security", "Spring Cloud", "Hibernate", "JPA",
    "Microservices", "REST", "RESTful APIs", "GraphQL", "SOAP", "J2EE", "JSP", "Servlets", "Struts", "Maven",
    "Gradle", "JUnit", "Mockito", "Kafka", "RabbitMQ", "JMS", "Redis", "AWS", "Azure", "GCP", "Lambda", "EC2",
    "S3", "Docker", "Kubernetes", "OpenShift", "Terraform", "Jenkins", "CI/CD", "GitHub Actions", "Git",
    "PostgreSQL", "MySQL", "Oracle", "SQL Server", "MongoDB", "Cassandra", "DynamoDB", "Elasticsearch",
    "Angular", "React", "Node.js", "JavaScript", "TypeScript", "HTML", "CSS", "Python", "Kotlin", "Scala",
    "Splunk", "Grafana", "Prometheus", "Linux", "Agile", "Scrum", "JIRA", "OAuth", "JWT", "Camel", "Drools",
    "Apache Spark", "Snowflake", "Selenium", "Cucumber", "Webflux", "Reactive", "Quarkus", "Micronaut",
]
_KEYWORD_PATTERNS = {
    kw: re.compile(r"(?<![\w.#+-])" + re.escape(kw).replace(r"\ ", r"[\s-]?") + r"(?![\w+#-])", re.I)
    for kw in JD_KEYWORDS
}


class TemplateError(ValueError):
    pass


# ------------------------------------------------------------------ personalization

def extract_jd_keywords(job_description: str, top: int = 3) -> list[str]:
    """Most frequent JD_KEYWORDS in the post; ties keep JD_KEYWORDS order. 'Spring' is dropped if 'Spring Boot' hits."""
    counts = {kw: len(p.findall(job_description)) for kw, p in _KEYWORD_PATTERNS.items()}
    hits = [kw for kw in JD_KEYWORDS if counts[kw]]
    hits = [kw for kw in hits if not any(o.lower().startswith(kw.lower() + " ") for o in hits)]
    hits.sort(key=lambda kw: -counts[kw])
    return hits[:top]


def build_hook(keywords: list[str], role: str) -> str:
    if not keywords:
        return f"My background as a {role} aligns closely with the requirements in your post."
    if len(keywords) == 1:
        joined = keywords[0]
    elif len(keywords) == 2:
        joined = f"{keywords[0]} and {keywords[1]}"
    else:
        joined = f"{', '.join(keywords[:-1])}, and {keywords[-1]}"
    return f"My hands-on experience with {joined} maps directly to what you are looking for."


def first_name(name: str) -> str:
    parts = name.split()
    return parts[0].capitalize() if parts else "there"


def load_template(name: str) -> tuple[str | None, str]:
    """Return (subject or None, body). A template's first line may be 'Subject: ...'."""
    raw = (TEMPLATES / name).read_text(encoding="utf-8")
    first, _, rest = raw.partition("\n")
    if first.lower().startswith("subject:"):
        return first.split(":", 1)[1].strip(), rest.lstrip("\n")
    return None, raw


def render(template: str, values: dict) -> str:
    missing = {f for _, f, _, _ in string.Formatter().parse(template) if f} - values.keys()
    if missing:
        raise TemplateError(f"Template uses undefined placeholders: {', '.join(sorted(missing))}")
    return template.format_map(values)


def template_values(recruiter: dict, candidate: dict, **extra) -> dict:
    values = {
        "recruiter_name": recruiter.get("recruiter_name", ""),
        "recruiter_first_name": first_name(recruiter.get("recruiter_name", "")),
        "role": recruiter.get("role") or candidate["primary_role"],
        "job_title": detect_job_title(recruiter.get("job_description", "")) or recruiter.get("role")
                     or candidate["primary_role"],
        "post_url": recruiter.get("post_url", ""),
    }
    values.update({f"candidate_{k}": str(v) for k, v in candidate.items() if isinstance(v, (str, int, float))})
    values.update(extra)
    return values


# ------------------------------------------------------------------ MIME + SMTP

def attachment_filename(candidate_name: str, suffix: str, ts: int | None = None) -> str:
    """'José Núñez' -> 'Jose_Nunez_Resume_1672531199.pdf' (ASCII only, timestamped to dodge preview caching)."""
    ascii_name = unicodedata.normalize("NFKD", candidate_name).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"[^A-Za-z0-9]+", "_", ascii_name).strip("_") or "Candidate"
    return f"{ascii_name}_Resume_{ts or int(time.time())}{suffix.lower()}"


def recipients_for(candidate: dict) -> tuple[list[str], list[str]]:
    cc = [e for e in (candidate.get("email"), client_config.TEAM_LEAD_EMAIL) if e]
    bcc = [client_config.ADMIN_EMAIL] if client_config.ADMIN_EMAIL else []
    return cc, bcc


def build_message(*, sender: str, sender_name: str, to: str, subject: str, body: str,
                  attachment: Path | None, attachment_name: str | None = None, cc: list[str] = (),
                  bcc: list[str] = (), in_reply_to: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    if attachment:
        ctype, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=attachment_name or attachment.name)
    return msg


class SendAborted(RuntimeError):
    """Stops a whole sending run: bad Gmail credentials, unreachable server, or daily quota exhausted."""


def smtp_connect(timeout: float = 30) -> smtplib.SMTP:
    """STARTTLS on 587; if blocked or timed out, SSL on 465. Authenticates with the Gmail app password."""
    user, password = settings.env("GMAIL_EMAIL"), settings.env("GMAIL_APP_PASSWORD").replace(" ", "")
    if not (user and password):
        raise SendAborted("Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env")
    try:
        server = smtplib.SMTP(SMTP_HOST, 587, timeout=timeout)
        server.starttls()
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("SMTP 587/STARTTLS failed (%s); falling back to 465/SSL", exc)
        try:
            server = smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=timeout)
        except (OSError, smtplib.SMTPException) as exc2:
            raise SendAborted(f"Cannot reach {SMTP_HOST} on 587 or 465: {exc2}") from exc2
    try:
        server.login(user, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise SendAborted("Gmail rejected the login. Use a 16-character App Password (2-Step Verification "
                          "must be on) in GMAIL_APP_PASSWORD.") from exc
    return server


def is_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "quota" in text or "limit exceeded" in text or "5.4.5" in text


class SmtpSession:
    """Lazy SMTP connection that reconnects once if Gmail drops it between messages."""

    def __init__(self, factory):
        self.factory = factory
        self.server = None

    def send(self, msg: EmailMessage) -> None:
        for attempt in (1, 2):
            if self.server is None:
                self.server = self.factory()  # SendAborted propagates
            try:
                self.server.send_message(msg)
                return
            except smtplib.SMTPServerDisconnected:
                self.server = None
                if attempt == 2:
                    raise

    def close(self) -> None:
        if self.server:
            try:
                self.server.quit()
            except (smtplib.SMTPException, OSError):
                pass
            self.server = None


def deliver(session: SmtpSession, msg: EmailMessage) -> tuple[str, str, bool]:
    """Send one message. Returns (status, note, stop_run)."""
    try:
        session.send(msg)
        return "sent", "", False
    except SendAborted:
        raise
    except (smtplib.SMTPException, OSError) as exc:
        if isinstance(exc, OSError):  # network dropped: reconnect on the next message
            session.server = None
        quota = is_quota_error(exc)
        log.error("Send to %s failed: %s", msg["To"], exc)
        return "failed", ("Gmail sending quota reached: " if quota else "") + str(exc), quota


# ------------------------------------------------------------------ resume dispatch

def _default_customizer(candidate, resume_path, job_description, role):
    return resume_customizer.customize(resume_path, job_description, role, candidate)


# Register client-specific customizers here and select one with CLIENTS[key]["customizer"], e.g.
#   import safi_customizer;  CUSTOMIZERS["safi"] = safi_customizer.customize
# A customizer takes (candidate, resume_path, job_description, role) and returns a CustomizationResult;
# resume_customizer.customize(..., parser=my_parser) lets it reuse the standard pipeline with its own parsing.
CUSTOMIZERS = {
    "default": _default_customizer,
}


def customize_resume(candidate: dict, job_description: str, role: str):
    resume = client_config.resolve_resume(candidate)
    if resume is None:
        raise FileNotFoundError(f"No resume for {candidate['key']}: {candidate.get('resume_candidates')}")
    customizer = CUSTOMIZERS.get(candidate.get("customizer", "default"), _default_customizer)
    return customizer(candidate, resume, job_description, role)


# ------------------------------------------------------------------ outreach

@dataclass
class OutreachReport:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    details: list[tuple[str, str, str]] = field(default_factory=list)  # (email, status, note)
    aborted: str = ""  # why the run stopped early, if it did


def sent_today() -> int:
    today = datetime.now().date()
    return sum(1 for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
               if r["email_sent_status"] == "sent" and (t := csv_store.parse_time(r["timestamp"])) and t.date() == today)


def already_contacted(recruiter_email: str, candidate_name: str) -> bool:
    return any(r["recruiter_email"].lower() == recruiter_email.lower() and r["candidate"] == candidate_name
               and r["email_sent_status"] == "sent"
               for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS))


def send_outreach(candidate_key: str, mode: str = "test", limit: int | None = None,
                  recruiters: list[dict] | None = None, smtp_factory=None, sleep=time.sleep,
                  customize=customize_resume) -> OutreachReport:
    """Email pending recruiters for one candidate. mode='test' sends everything to GMAIL_EMAIL instead."""
    candidate = client_config.get_candidate(candidate_key)
    smtp_factory = smtp_factory or smtp_connect
    csv_store.migrate(settings.history_csv(), csv_store.HISTORY_FIELDS)
    recruiters = pending_recruiters(candidate_key) if recruiters is None else recruiters
    sender = settings.env("GMAIL_EMAIL")
    subject_tpl, body_tpl = load_template("outreach_email.txt")
    cc, bcc = recipients_for(candidate)
    report = OutreachReport()
    session = SmtpSession(smtp_factory)
    live = mode == "live"
    try:
        for rec in recruiters:
            email = rec["recruiter_email"].strip().lower()
            if limit is not None and report.sent >= limit:
                report.skipped += 1
                report.details.append((email, "skipped", "limit reached"))
                continue
            if live and already_contacted(email, candidate["name"]):
                report.skipped += 1
                report.details.append((email, "skipped", "already contacted by this candidate"))
                continue

            keywords = extract_jd_keywords(rec.get("job_description", ""))
            values = template_values(rec, candidate, hook=build_hook(keywords, candidate["primary_role"]),
                                     top_skills=", ".join(keywords))
            subject = render(subject_tpl or "{job_title} (C2C) - {candidate_name}", values)
            body = render(body_tpl, values)
            attachment, resume_note = _resume_for(customize, candidate, rec)
            msg = build_message(
                sender=sender, sender_name=candidate["name"], to=email if live else sender,
                subject=subject if live else f"[TEST -> {email}] {subject}", body=body,
                attachment=attachment, attachment_name=attachment_filename(candidate["name"], attachment.suffix),
                cc=cc if live else [], bcc=bcc if live else [],
            )

            if report.sent or report.failed:
                sleep(SEND_DELAY_SECONDS)
            status, error, stop = deliver(session, msg)
            note = error or resume_note
            report.details.append((email, status, note))
            if status == "sent":
                report.sent += 1
            else:
                report.failed += 1
            if live:
                _record(rec, candidate, status, subject, msg["Message-ID"])
            log.info("%s %s -> %s (%s)", mode.upper(), status, email, note)
            if stop:
                report.aborted = note
                break
    except SendAborted as exc:
        report.aborted = str(exc)
        log.error("Sending stopped: %s", exc)
    finally:
        session.close()
    return report


def _resume_for(customize, candidate: dict, rec: dict) -> tuple[Path, str]:
    """Tailored resume, or the original one if tailoring/conversion fails for any reason."""
    try:
        result = customize(candidate, rec.get("job_description", ""), rec.get("role") or candidate["primary_role"])
        return result.path, result.note
    except Exception as exc:  # never let one resume problem stop the outreach run
        log.exception("Resume customization failed; attaching the original resume")
        original = client_config.resolve_resume(candidate)
        if original is None:
            raise SendAborted(f"No resume file for {candidate['name']}") from exc
        try:
            return resume_customizer.ensure_pdf(original), f"original resume attached ({exc})"
        except Exception:
            return original, f"original resume attached ({exc})"


def _record(rec: dict, candidate: dict, status: str, subject: str, message_id: str) -> None:
    csv_store.append_rows(settings.history_csv(), csv_store.HISTORY_FIELDS, [{
        "recruiter_name": rec.get("recruiter_name", ""),
        "recruiter_email": rec["recruiter_email"].lower(),
        "role": rec.get("role", ""),
        "post_url": rec.get("post_url", ""),
        "candidate": candidate["name"],
        "email_sent_status": status,
        "timestamp": csv_store.now(),
        "subject": subject,
        "message_id": message_id,
        "followup_count": "0",
        "last_followup_at": "",
        "replied": "0",
    }])
    csv_store.update_rows(
        settings.recruiters_csv(), csv_store.RECRUITER_FIELDS,
        lambda r: r["recruiter_email"].lower() == rec["recruiter_email"].lower() and r["post_url"] == rec.get("post_url", ""),
        {"email_sent_status": status},
    )
