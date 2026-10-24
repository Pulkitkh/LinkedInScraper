"""Regression tests for everything seen in the first live batch run (Gemini overload, titles, greetings...)."""

import sys
import types
from contextlib import contextmanager

import pytest
import requests

import csv_store
import gmail_sender
import linkedin_login
import linkedin_scraper as ls
import llm_client
import main
import resume_customizer as rc
import settings
from test_e2e_linkedin import _browser_available
from test_llm_client import FakeHTTP, Resp, chat_ok, gemini_ok, keys

JAVA = {"allowed_domains": ["java", "fullstack", "backend"]}
OVERLOADED = Resp(503, '{"error": {"code": 503, "message": "This model is currently experiencing high demand. '
                       'Spikes in demand are usually temporary. Please try again later.", "status": "UNAVAILABLE"}}')


# ------------------------------------------------------------------ Gemini overload

def test_overloaded_gemini_falls_back_to_lite_model(monkeypatch):
    keys(monkeypatch, gemini="GEMINI-KEY-123")
    http = FakeHTTP({"gemini-flash-latest:": OVERLOADED, "gemini-flash-lite-latest:": gemini_ok('{"ok": 1}')})
    assert llm_client.generate("s", "p", ["gemini"], want_json=True, http=http) == {"ok": 1}


def test_timeout_falls_back_to_lite_model(monkeypatch):
    keys(monkeypatch, gemini="GEMINI-KEY-123")

    class Slow(FakeHTTP):
        def post(self, url, **kwargs):
            if "gemini-flash-latest:" in url:
                assert kwargs["timeout"] == llm_client.TIMEOUT == 45
                raise requests.ReadTimeout("Read timed out. (read timeout=45)")
            return super().post(url, **kwargs)

    assert llm_client.generate("s", "p", ["gemini"], http=Slow({"lite-latest:": gemini_ok("fast")})) == "fast"


def test_repeated_overload_triggers_cooldown_then_skips_instantly(monkeypatch, caplog):
    keys(monkeypatch, gemini="GEMINI-KEY-123")
    http = FakeHTTP({":generateContent": OVERLOADED})
    for _ in range(2):
        with pytest.raises(llm_client.LLMError):
            llm_client.generate("s", "p", ["gemini"], http=http)
    calls_before = len(http.calls)
    with pytest.raises(llm_client.LLMError, match="skipped for 10 more min"):
        llm_client.generate("s", "p", ["gemini"], http=http)
    assert len(http.calls) == calls_before  # no request, no waiting
    assert "skipping it for 10 minutes" in caplog.text


def test_cooldown_moves_on_to_next_provider(monkeypatch):
    keys(monkeypatch, gemini="GEMINI-KEY-123", groq="GROQ-KEY-123")
    llm_client._cooldown_until["gemini"] = 9e18
    http = FakeHTTP({"groq.com": chat_ok("from groq")})
    assert llm_client.generate("s", "p", llm_client.CUSTOMIZE_CHAIN, http=http) == "from groq"
    assert all("googleapis" not in c[1] for c in http.calls)


def test_success_resets_failure_count(monkeypatch):
    keys(monkeypatch, gemini="GEMINI-KEY-123")
    bad = FakeHTTP({":generateContent": OVERLOADED})
    with pytest.raises(llm_client.LLMError):
        llm_client.generate("s", "p", ["gemini"], http=bad)
    assert llm_client.generate("s", "p", ["gemini"], http=FakeHTTP({":generateContent": gemini_ok("ok")})) == "ok"
    with pytest.raises(llm_client.LLMError):  # one more failure is not enough for a cool-down
        llm_client.generate("s", "p", ["gemini"], http=bad)
    assert "gemini" not in llm_client._cooldown_until


def test_error_messages_are_one_line(monkeypatch):
    keys(monkeypatch, gemini="GEMINI-KEY-123")
    monkeypatch.setattr(llm_client, "GEMINI_FALLBACK_MODELS", [])
    with pytest.raises(llm_client.LLMError) as exc:
        llm_client.generate("s", "p", ["gemini"], http=FakeHTTP({":generateContent": OVERLOADED}))
    message = str(exc.value)
    assert "\n" not in message and "{" not in message
    assert "experiencing high demand" in message


def test_word_progress_bar_is_hidden(tmp_path, monkeypatch, capsys):
    docx = tmp_path / "r.docx"
    docx.write_bytes(b"docx")

    def convert(src, dst):
        sys.stderr.write("100%|##########| 1/1 [00:08<00:00,  8.27s/it]\n")
        open(dst, "wb").write(b"%PDF")

    monkeypatch.setitem(sys.modules, "docx2pdf", types.SimpleNamespace(convert=convert))
    monkeypatch.setattr(rc.sys, "platform", "win32")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert rc.docx_to_pdf(docx)
    assert "100%" not in capsys.readouterr().err


# ------------------------------------------------------------------ titles, roles, greetings (from the real run)

@pytest.mark.parametrize("raw,title", [
    ("3.Sr. Full stack Java Developer – C2C-Charlotte, NC", "Sr. Full stack Java Developer"),
    ("💼 Oracle OTM Solution Architect | Onsite | Bay Area CA", "Oracle OTM Solution Architect"),
    ("1. ReactJS Developer - C2C - New York, NY (Onsite) - Only Locals", "ReactJS Developer"),
    ("🔷 Hiring: Java Full stack Developer", "Java Full stack Developer"),
    (".NET/java J2EE full stack developer,", ".NET/java J2EE full stack developer"),
    ("🚀 HIRING: Java AWS Developer", "Java AWS Developer"),
    ("📢 Hiring: Java Developer", "Java Developer"),
    ("🚨 **HIRING: JAVA DEVELOPER | 5 OPEN POSITIONS** 🚨", "Java Developer"),
    ("We are hiring a Java Developer (C2C) in Dallas, TX", "Java Developer"),
    ("Java Full Stack Developer with Angular", "Java Full Stack Developer with Angular"),
    ("Thanks for reading!", ""),
])
def test_clean_job_title(raw, title):
    assert ls.clean_job_title(raw) == title


@pytest.mark.parametrize("post,mismatch", [
    ("1. ReactJS Developer - C2C - New York, NY", True),
    ("💼 Oracle OTM Solution Architect | Onsite | Bay Area CA\nJava integration a plus", True),
    ("Job Title: Software Engineer\nJava 17, Spring Boot, Dallas, TX", False),  # generic title, Java body
    ("Job Title: Software Engineer\nGolang, Kubernetes, Dallas, TX", True),
    ("Java Full Stack Developer with Angular, Charlotte, NC", False),
    (".NET/java J2EE full stack developer, Austin, TX", False),
])
def test_role_mismatch_real_posts(post, mismatch):
    assert ls.role_mismatch(post, JAVA) is mismatch


@pytest.mark.parametrize("name,email,expected", [
    ("Priya Reddy", "priya@vysystems.com", "Priya"),
    ("Aryan Kumar", "aryantinku2002@gmail.com", "Aryan"),
    ("Aptonin Inc", "jobsearch@aptoninc.com", "there"),
    ("Vaishu M", "careers@petadata.co", "Vaishu"),
    ("Nikhil S", "sowmya.m@skillconnectllc.com", "Sowmya"),
    ("Kavitha G", "kavithag@petadata.ai", "Kavitha"),
    ("Pavan T", "indranis@petadata.ai", "there"),
    ("Kumar V", "paul@ventechsoft.com", "there"),
    ("K Satakam", "skatakam@vsoftdigital.com", "there"),
    ("Mohammad Nadeem", "nadeem@vkoresolutions.com", "Mohammad"),
    ("Nexylum", "ratnika@nexylum.com", "Ratnika"),
    ("", "jobs@x.com", "there"),
])
def test_greeting_name_real_recipients(name, email, expected):
    assert gmail_sender.greeting_name(name, email) == expected


def test_saved_rows_are_refiltered_before_sending(workspace):
    base = {"role": "Java Developer", "timestamp": csv_store.now(), "email_sent_status": "pending"}
    csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, [
        {**base, "recruiter_email": "react@x.com", "post_url": "p1",
         "job_description": "1. ReactJS Developer - C2C - New York, NY"},
        {**base, "recruiter_email": "java@x.com", "post_url": "p2",
         "job_description": "Job Title: Java Developer\nC2C, Dallas, TX"},
    ])
    assert [r["recruiter_email"] for r in ls.pending_recruiters("jane")] == ["java@x.com"]
    statuses = {r["recruiter_email"]: r["email_sent_status"]
                for r in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)}
    assert statuses == {"react@x.com": "mismatched_role", "java@x.com": "pending"}


def test_subject_uses_clean_title(workspace):
    rec = {"recruiter_name": "Swarupa D", "recruiter_email": "sreya@foxoneusa.com", "role": "Java Developer",
           "post_url": "u", "job_description": "🚨 **HIRING: JAVA DEVELOPER | 5 OPEN POSITIONS** 🚨\nC2C Dallas, TX"}
    import client_config

    values = gmail_sender.template_values(rec, client_config.get_candidate("jane"))
    subject_tpl, _ = gmail_sender.load_template("outreach_email.txt")
    assert gmail_sender.render(subject_tpl, values) == "Java Developer (C2C) - Jane Doe | 8+ Years | H1B"


# ------------------------------------------------------------------ batch: browser closed before sending

def test_batch_closes_browser_before_sending(workspace, monkeypatch):
    events = []

    @contextmanager
    def fake_context(headless=True, use_session=True):
        events.append("browser open")
        yield object(), object()
        events.append("browser closed")

    monkeypatch.setattr(linkedin_login, "browser_context", fake_context)
    monkeypatch.setattr(linkedin_login, "login", lambda *a, **k: events.append("login"))
    monkeypatch.setattr(ls, "scrape_candidate", lambda key, page=None: events.append("scrape"))
    monkeypatch.setattr(ls, "pending_recruiters", lambda key: [{"recruiter_email": "a@x.com"}])
    monkeypatch.setattr(gmail_sender, "send_outreach",
                        lambda *a, **k: events.append("send") or gmail_sender.OutreachReport(sent=1))
    main.run_batch("test", 10, assume_yes=True)
    assert events == ["browser open", "login", "scrape", "browser closed", "send"]


def test_scrape_error_in_batch_still_sends(workspace, monkeypatch):
    @contextmanager
    def fake_context(headless=True, use_session=True):
        yield object(), object()

    def broken(key, page=None):
        raise RuntimeError("Page.goto: net::ERR_INTERNET_DISCONNECTED")

    sent = []
    monkeypatch.setattr(linkedin_login, "browser_context", fake_context)
    monkeypatch.setattr(linkedin_login, "login", lambda *a, **k: None)
    monkeypatch.setattr(ls, "scrape_candidate", broken)
    monkeypatch.setattr(ls, "pending_recruiters", lambda key: [{"recruiter_email": "a@x.com"}])
    monkeypatch.setattr(gmail_sender, "send_outreach",
                        lambda *a, **k: sent.append(1) or gmail_sender.OutreachReport(sent=1))
    main.run_batch("test", 10, assume_yes=True)
    assert sent == [1]


@pytest.mark.skipif(not _browser_available(), reason="Chromium not installed")
def test_browser_closed_by_hand_does_not_raise(workspace):
    with linkedin_login.browser_context(headless=True) as (context, page):
        context.browser.close()  # like the user closing the window, or the driver dying
    # leaving the block must not raise "Browser.close: Connection closed"
