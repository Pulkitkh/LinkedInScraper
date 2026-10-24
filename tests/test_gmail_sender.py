import smtplib
from email import message_from_bytes, policy

import pytest

import csv_store
import gmail_sender as gs
import settings
from resume_customizer import CustomizationResult

JD = ("Job Title: Senior Java Developer\nWe need Java, Spring Boot, Kafka and PostgreSQL. Spring Boot and Kafka "
      "experience required. AWS nice to have. C2C Dallas, TX")


def recruiter(email="john@techcorp.com", post="https://www.linkedin.com/feed/update/urn:li:activity:1/"):
    return {"recruiter_name": "John Smith", "recruiter_email": email, "role": "Java Developer",
            "post_url": post, "job_description": JD, "email_sent_status": "pending", "timestamp": csv_store.now()}


class FakeSMTP:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for, self.closed = [], fail_for, False

    def send_message(self, msg):
        if msg["To"] in self.fail_for:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        self.sent.append(msg)

    def quit(self):
        self.closed = True


@pytest.fixture
def fake_resume(workspace):
    pdf = workspace / "tailored.pdf"
    pdf.write_bytes(b"%PDF-1.4 tailored")
    calls = []

    def customize(candidate, jd, role):
        calls.append((candidate["name"], role))
        return CustomizationResult(path=pdf, customized=True, note="3 sections rewritten")

    customize.calls = calls
    return customize


def test_keywords_and_hook():
    assert gs.extract_jd_keywords(JD) == ["Java", "Spring Boot", "Kafka"]
    assert gs.build_hook(["Java", "Spring Boot", "PostgreSQL"], "Java Developer") == (
        "My hands-on experience with Java, Spring Boot, and PostgreSQL maps directly to what you are looking for.")
    assert gs.build_hook([], "Java Developer").startswith("My background as a Java Developer")
    assert gs.extract_jd_keywords("JavaScript and React only") == ["React", "JavaScript"]
    assert len(gs.JD_KEYWORDS) >= 60


def test_attachment_filename_is_ascii_and_timestamped():
    assert gs.attachment_filename("José Núñez", ".PDF", ts=1672531199) == "Jose_Nunez_Resume_1672531199.pdf"


def test_template_missing_placeholder():
    with pytest.raises(gs.TemplateError, match="nope"):
        gs.render("Hi {nope}", {})


def test_live_send_records_history_and_dedupes(workspace, fake_resume):
    csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, [recruiter(), recruiter("fail@x.com", "p2")])
    server, sleeps = FakeSMTP(fail_for=("fail@x.com",)), []
    report = gs.send_outreach("jane", mode="live", smtp_factory=lambda: server, sleep=sleeps.append,
                              customize=fake_resume)
    assert (report.sent, report.failed) == (1, 1)
    assert sleeps == [3]
    msg = server.sent[0]
    assert msg["To"] == "john@techcorp.com"
    assert msg["Cc"] == "jane@candidate.com, lead@agency.com" and msg["Bcc"] == "admin@agency.com"
    assert msg["From"] == "Jane Doe <operator@gmail.com>"
    assert msg["Subject"] == "Senior Java Developer (C2C) - Jane Doe | 8+ Years | H1B"
    body = msg.get_body(("plain",)).get_content()
    assert body.startswith("Hi John,")
    assert "My hands-on experience with Java, Spring Boot, and Kafka maps directly" in body
    att = next(msg.iter_attachments())
    assert att.get_filename().startswith("Jane_Doe_Resume_") and att.get_filename().endswith(".pdf")
    assert att.get_content_type() == "application/pdf"

    history = csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
    by_email = {h["recruiter_email"]: h for h in history}
    assert by_email["john@techcorp.com"]["email_sent_status"] == "sent"
    assert by_email["john@techcorp.com"]["message_id"] == msg["Message-ID"]
    assert by_email["john@techcorp.com"]["candidate"] == "Jane Doe"
    assert by_email["john@techcorp.com"]["followup_count"] == "0" and by_email["john@techcorp.com"]["replied"] == "0"
    assert by_email["fail@x.com"]["email_sent_status"] == "failed"
    statuses = {r["recruiter_email"]: r["email_sent_status"]
                for r in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)}
    assert statuses == {"john@techcorp.com": "sent", "fail@x.com": "failed"}
    assert gs.sent_today() == 1

    # second run: john is skipped (already contacted by Jane); the failed one is retried
    again = gs.send_outreach("jane", mode="live", smtp_factory=lambda: FakeSMTP(), sleep=sleeps.append,
                             customize=fake_resume)
    assert [d[0] for d in again.details] == ["fail@x.com"] and again.sent == 1


def test_test_mode_redirects_to_operator_and_records_nothing(workspace, fake_resume):
    server = FakeSMTP()
    report = gs.send_outreach("jane", mode="test", recruiters=[recruiter()], smtp_factory=lambda: server,
                              sleep=lambda s: None, customize=fake_resume)
    msg = server.sent[0]
    assert report.sent == 1 and msg["To"] == "operator@gmail.com"
    assert msg["Subject"].startswith("[TEST -> john@techcorp.com]") and msg["Cc"] is None
    assert csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS) == []


def test_limit(workspace, fake_resume):
    rows = [recruiter(f"r{i}@x.com", f"p{i}") for i in range(3)]
    report = gs.send_outreach("jane", mode="live", limit=2, recruiters=rows, smtp_factory=FakeSMTP,
                              sleep=lambda s: None, customize=fake_resume)
    assert (report.sent, report.skipped) == (2, 1)


def test_smtp_falls_back_to_ssl_465(monkeypatch, workspace):
    events = []

    class Blocked:
        def __init__(self, host, port, timeout=None):
            events.append(("smtp", host, port))
            raise TimeoutError("port 587 blocked")

    class SSL:
        def __init__(self, host, port, timeout=None):
            events.append(("ssl", host, port))

        def login(self, user, password):
            events.append(("login", user, password))

    monkeypatch.setattr(smtplib, "SMTP", Blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", SSL)
    gs.smtp_connect()
    assert events == [("smtp", "smtp.gmail.com", 587), ("ssl", "smtp.gmail.com", 465),
                      ("login", "operator@gmail.com", "abcdefghijklmnop")]


def test_real_smtp_protocol_roundtrip(workspace, fake_resume):
    """Send through an actual SMTP server (aiosmtpd on localhost) and parse what it received."""
    aiosmtpd = pytest.importorskip("aiosmtpd.controller")

    received = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope)
            return "250 OK"

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = aiosmtpd.Controller(Handler(), hostname="127.0.0.1", port=port)
    controller.start()
    try:
        report = gs.send_outreach("jane", mode="live", recruiters=[recruiter()], sleep=lambda s: None,
                                  smtp_factory=lambda: smtplib.SMTP("127.0.0.1", port), customize=fake_resume)
    finally:
        controller.stop()
    assert report.sent == 1
    env = received[0]
    assert sorted(env.rcpt_tos) == ["admin@agency.com", "jane@candidate.com", "john@techcorp.com", "lead@agency.com"]
    msg = message_from_bytes(env.content, policy=policy.default)
    assert msg["Bcc"] is None  # BCC is in the envelope only, never in the delivered headers
    assert next(msg.iter_attachments()).get_content() == b"%PDF-1.4 tailored"
