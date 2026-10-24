"""Drive main.py exactly like the demo: every menu option in order, with only the network boundaries faked."""

import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

import csv_store
import gmail_sender
import imap_reply_detector
import linkedin_login
import linkedin_scraper
import llm_client
import main
import settings
from conftest import ROOT
from test_e2e_linkedin import FakeLinkedIn, _browser_available
from test_followup_imap_analytics import FakeIMAP
from test_gmail_sender import FakeSMTP

needs_browser = pytest.mark.skipif(not _browser_available(), reason="Chromium not installed")


@pytest.fixture
def demo(workspace, monkeypatch):
    fake, real_context, server = FakeLinkedIn(), linkedin_login.browser_context, FakeSMTP()

    @contextmanager
    def routed(headless=True, use_session=True):
        with real_context(headless=True, use_session=use_session) as (context, page):
            context.route("https://www.linkedin.com/**", fake.handle)
            yield context, page

    monkeypatch.setattr(linkedin_login, "browser_context", routed)
    monkeypatch.setattr(linkedin_scraper, "browser_context", routed)
    monkeypatch.setattr(linkedin_scraper, "human_pause", lambda *a: None)
    monkeypatch.setattr(gmail_sender, "smtp_connect", lambda *a, **k: server)
    monkeypatch.setattr(gmail_sender.time, "sleep", lambda s: None)
    return server


def script(monkeypatch, answers):
    """Feed menu answers in order; a callable in the list runs (e.g. to move time forward) and is skipped."""
    queue = list(answers)

    def ask(*args, **kwargs):
        while callable(queue[0]):
            queue.pop(0)()
        return queue.pop(0)

    monkeypatch.setattr(main.Prompt, "ask", ask)
    monkeypatch.setattr(main.IntPrompt, "ask", ask)
    return queue


def age_history(days=8):
    old = (datetime.now() - timedelta(days=days)).strftime(csv_store.TIME_FORMAT)
    csv_store.update_rows(settings.history_csv(), csv_store.HISTORY_FIELDS, lambda r: True, {"timestamp": old})


@needs_browser
def test_full_demo_walkthrough(demo, monkeypatch):
    server = demo
    monkeypatch.setattr(imap_reply_detector, "connect", lambda: FakeIMAP(["John <john.smith@techcorp.com>"]))
    monkeypatch.setattr(llm_client, "generate", lambda *a, **k: "OK")
    queue = script(monkeypatch, [
        "2",                      # LinkedIn login -> session.json
        "3", 1, "y",              # scrape for candidate 1, headless
        "4", 1,                   # view pending
        "5", 1, "test", 20,       # outreach in TEST mode (to my own inbox)
        "5", 1, "live", 20, "SEND",  # outreach LIVE after typing SEND
        age_history,              # pretend a week passed
        "6", "live", "SEND",      # threaded follow-ups
        "7",                      # IMAP reply sync
        "8", "test", 40,          # batch mode (test)
        "9",                      # analytics
        "1", "n",                 # validate setup (no live checks)
        "0",
    ])
    main.interactive()
    assert queue == []

    test_mails = [m for m in server.sent if m["Subject"].startswith("[TEST")]
    live_mails = [m for m in server.sent if not m["Subject"].startswith(("[TEST", "Re:"))]
    followups = [m for m in server.sent if m["Subject"].startswith("Re:")]
    assert len(test_mails) >= 2 and all(m["To"] == "operator@gmail.com" for m in test_mails)
    assert sorted(m["To"] for m in live_mails) == ["john.smith@techcorp.com", "maria@staffhub.com"]
    assert sorted(m["To"] for m in followups) == ["john.smith@techcorp.com", "maria@staffhub.com"]
    assert all(m["In-Reply-To"] for m in followups)

    history = {h["recruiter_email"]: h for h in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)}
    assert history["john.smith@techcorp.com"]["replied"] == "1"
    assert history["maria@staffhub.com"]["replied"] == "0"
    assert all(h["followup_count"] == "1" for h in history.values())
    assert settings.analytics_report().exists()


@needs_browser
def test_live_send_cancelled_unless_send_typed(demo, monkeypatch):
    server = demo
    script(monkeypatch, ["3", 1, "y", "5", 1, "live", 20, "send please", "0"])
    main.interactive()
    assert server.sent == [] and csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS) == []


def test_menu_survives_an_action_error(workspace, monkeypatch):
    def boom():
        raise RuntimeError("LinkedIn is down")

    monkeypatch.setattr(main.imap_reply_detector, "check_replies", boom)
    queue = script(monkeypatch, ["7", "9", "0"])
    main.interactive()
    assert queue == []


@needs_browser
def test_check_command_all_pass(demo, monkeypatch, workspace):
    class Conn:
        def select(self, *a, **k):
            return "OK", [b"0"]

        def logout(self):
            pass

    monkeypatch.setattr(imap_reply_detector, "connect", lambda: Conn())
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setattr(llm_client, "generate", lambda *a, **k: "OK")
    with linkedin_login.browser_context() as (context, page):
        linkedin_login.login(context, page, headless=True)
    results = main.check_connections()
    by_name = {name: ok for name, ok, _ in results}
    assert by_name["Gmail SMTP login"] and by_name["Gmail IMAP login"] and by_name["AI: gemini"]
    assert by_name["LinkedIn session"] and by_name["Candidate jane"]
    assert not by_name["AI: groq"]  # no key: reported, but one working provider is enough
    assert main.show_checks(results)


def test_check_command_reports_failures(workspace, monkeypatch):
    def fail(*a, **k):
        raise gmail_sender.SendAborted("Gmail rejected the login")

    monkeypatch.setattr(gmail_sender, "smtp_connect", fail)
    monkeypatch.setattr(linkedin_login, "browser_context", fail)
    results = main.check_connections()
    assert not main.show_checks(results)
    assert dict((n, d) for n, _, d in results)["Gmail SMTP login"] == "SendAborted: Gmail rejected the login"


def _run_cli(args, stdin="", tmp=None):
    env = {**os.environ, "DATA_DIR": str(tmp), "COLUMNS": "120", "LINKEDIN_EMAIL": "", "GMAIL_EMAIL": "",
           "GEMINI_API_KEY": "", "GROQ_API_KEY": "", "OPENROUTER_API_KEY": ""}
    return subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, input=stdin, capture_output=True,
                          text=True, timeout=120, env=env)


def test_cli_help(tmp_path):
    result = _run_cli(["--help"], tmp=tmp_path)
    assert result.returncode == 0 and "--batch" in result.stdout and "--check" in result.stdout


def test_cli_real_prompts(tmp_path):
    """Real keyboard input through rich prompts, with the shipped client_config and sample resume."""
    result = _run_cli([], stdin="4\n1\n9\n1\nn\n0\n", tmp=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 pending recruiter(s)" in result.stdout
    assert "Dashboard:" in result.stdout
    assert "LINKEDIN_EMAIL is not set" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert (tmp_path / "recruiters.csv").exists() and (tmp_path / "analytics_report.html").exists()


def test_cli_invalid_menu_choice_is_reprompted(tmp_path):
    result = _run_cli([], stdin="42\nabc\n0\n", tmp=tmp_path)
    assert result.returncode == 0 and "Please select one of the available options" in result.stdout


def test_cli_batch_without_session_fails_cleanly(tmp_path):
    result = _run_cli(["--batch", "--mode", "test"], tmp=tmp_path)
    assert result.returncode == 1 and "Traceback" not in result.stdout + result.stderr
