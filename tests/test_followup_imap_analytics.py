from datetime import datetime, timedelta

import pytest

import analytics
import csv_store
import followup_sender
import imap_reply_detector
import settings
from test_gmail_sender import FakeSMTP

NOW = datetime(2026, 10, 24, 12, 0, 0)


def history_row(email, days_ago, **kw):
    return {"recruiter_name": "John Smith", "recruiter_email": email, "role": "Java Developer",
            "post_url": f"https://www.linkedin.com/feed/update/{email}/", "candidate": "Jane Doe",
            "email_sent_status": "sent", "timestamp": (NOW - timedelta(days=days_ago)).strftime(csv_store.TIME_FORMAT),
            "subject": "Senior Java Developer (C2C) - Jane Doe", "message_id": f"<{email.split('@')[0]}.1700000000@gmail.com>",
            "followup_count": "0", "last_followup_at": "", "replied": "0", **kw}


@pytest.fixture
def history(workspace):
    csv_store.append_rows(settings.history_csv(), csv_store.HISTORY_FIELDS, [
        history_row("due@x.com", 8),
        history_row("recent@x.com", 2),
        history_row("replied@x.com", 9, replied="1"),
        history_row("followed@x.com", 10, followup_count="1"),
        history_row("failed@x.com", 9, email_sent_status="failed"),
    ])
    csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, [{
        "recruiter_name": "John Smith", "recruiter_email": "due@x.com", "role": "Java Developer",
        "post_url": "https://www.linkedin.com/feed/update/due@x.com/", "email_sent_status": "sent",
        "job_description": "Job Title: Senior Java Developer\nSpring Boot, Kafka, AWS", "timestamp": csv_store.now(),
    }])
    return workspace


def test_eligible_followups(history):
    assert [r["recruiter_email"] for r in followup_sender.eligible_followups(7, now=NOW)] == ["due@x.com"]


def test_followup_is_threaded_and_recorded(history):
    server = FakeSMTP()
    prompts = []

    def gen(system, prompt, chain, want_json=False, http=None):
        prompts.append((prompt, chain))
        return "My Spring Boot and Kafka work at Acme Bank matches your payments platform."

    report = followup_sender.send_followups(mode="live", now=NOW, smtp_factory=lambda: server,
                                            sleep=lambda s: None, generate=gen)
    assert report.sent == 1
    msg = server.sent[0]
    assert msg["Subject"] == "Re: Senior Java Developer (C2C) - Jane Doe"
    assert msg["In-Reply-To"] == "<due.1700000000@gmail.com>" and msg["References"] == "<due.1700000000@gmail.com>"
    assert msg["Cc"] == "jane@candidate.com, lead@agency.com" and msg["Bcc"] == "admin@agency.com"
    body = msg.get_body(("plain",)).get_content()
    assert "Hi there," in body and "matches your payments platform" in body  # due@x.com is not John's address
    assert next(msg.iter_attachments()).get_filename().startswith("Jane_Doe_Resume_")
    assert "Spring Boot, Kafka, AWS" in prompts[0][0] and prompts[0][1][0] == "openrouter"

    row = next(r for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
               if r["recruiter_email"] == "due@x.com")
    assert row["followup_count"] == "1" and row["last_followup_at"]
    assert followup_sender.eligible_followups(7, now=NOW) == []


def test_value_prop_fallback_without_ai():
    def broken(*a, **k):
        raise followup_sender.llm_client.LLMError("no keys")

    text = followup_sender.value_proposition({"name": "Jane", "primary_role": "Java Developer", "experience": "8+ Years"},
                                             "Spring Boot and Kafka", generate=broken)
    assert text == "With 8+ Years experience across Spring Boot, Kafka, I'm confident I can make an immediate impact as your Java Developer."


class FakeIMAP:
    def __init__(self, senders):
        self.senders, self.calls = senders, []

    def select(self, box, readonly=False):
        self.calls.append(("select", box, readonly))
        return "OK", [b"3"]

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.senders)))]

    def fetch(self, ids, query):
        self.calls.append(("fetch", ids, query))
        out = []
        for i in ids.split(","):
            out.append((f"{i} (BODY[HEADER.FIELDS (FROM)] {{40}}".encode(),
                        f"From: {self.senders[int(i) - 1]}\r\n\r\n".encode()))
            out.append(b")")
        return "OK", out

    def logout(self):
        self.calls.append(("logout",))


def test_imap_marks_replies(history, monkeypatch):
    imap = FakeIMAP(['"John Smith" <DUE@x.com>', "newsletter@spam.com", "Recent <recent@x.com>"])
    monkeypatch.setattr(imap_reply_detector, "BATCH_SIZE", 2)
    assert imap_reply_detector.check_replies(imap_factory=lambda: imap) == 2
    since = (NOW - timedelta(days=10)).strftime("%d-%b-%Y")  # oldest sent + unreplied (failed/replied ignored)
    assert ("select", "INBOX", True) in imap.calls
    assert ("search", "SINCE", since) in imap.calls
    assert [c[1] for c in imap.calls if c[0] == "fetch"] == ["1,2", "3"]
    replied = {r["recruiter_email"] for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
               if r["replied"] == "1"}
    assert replied == {"due@x.com", "recent@x.com", "replied@x.com"}


def test_imap_default_window(workspace):
    assert imap_reply_detector.search_since(now=NOW) == NOW - timedelta(days=30)


def test_analytics_report(history):
    stats = analytics.compute_stats(now=NOW)
    assert stats["emails_sent"] == 4 and stats["emails_failed"] == 1
    assert stats["replies"] == 1 and stats["reply_rate"] == 25.0 and stats["followups_sent"] == 1
    assert stats["per_candidate"]["Jane Doe"]["sent"] == 4
    path = analytics.export_report()
    html = path.read_text()
    assert path.name == "analytics_report.html" and "--bg:#0f1117" in html and "Jane Doe" in html
