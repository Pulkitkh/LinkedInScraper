"""End-to-end login + scrape against a fake LinkedIn served through Playwright routing (no network)."""

import os
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import sync_playwright

import csv_store
import linkedin_login
import linkedin_scraper
import settings
from conftest import FIXTURES

SIGN_IN_WALL = "<h1>Sign in to view</h1><form><input name='session_key'></form>"


def _browser_available() -> bool:
    try:
        with sync_playwright() as p:
            exe = os.environ.get("CHROMIUM_EXECUTABLE")
            p.chromium.launch(**({"executable_path": exe} if exe else {})).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_available(), reason="Chromium not installed (playwright install chromium)")


LOGIN_SUBMIT = ("<p>Signing in...</p><script>document.cookie = 'li_at=fake-session; path=/';"
                "setTimeout(() => location.href = '/feed/', 800);</script>")


class FakeLinkedIn:
    def __init__(self, search_fixture="search_results.html"):
        self.search_urls = []
        self.search_fixture = search_fixture

    def handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        logged_in = "li_at=" in (req.headers.get("cookie") or "")
        if path.startswith("/checkpoint/lg/login-submit"):
            route.fulfill(content_type="text/html", body=LOGIN_SUBMIT)
        elif path.startswith("/login"):
            route.fulfill(content_type="text/html", body=(FIXTURES / "login.html").read_text())
        elif path.startswith("/checkpoint"):
            route.fulfill(content_type="text/html", body="<h1>Verify it's you</h1>")
        elif not logged_in:
            # Playwright doesn't re-route fulfilled redirects, so serve LinkedIn's in-place sign-in wall.
            route.fulfill(content_type="text/html", body=SIGN_IN_WALL)
        elif path.startswith("/feed"):
            route.fulfill(content_type="text/html", body="<header id='global-nav'>nav</header><main>Feed</main>")
        elif path.startswith("/search/results/content"):
            self.search_urls.append(req.url)
            route.fulfill(content_type="text/html", body=(FIXTURES / self.search_fixture).read_text())
        else:
            route.fulfill(status=404, body="")


@pytest.fixture
def browser(workspace):
    fake = FakeLinkedIn()
    with linkedin_login.browser_context(headless=True) as (context, page):
        context.route("https://www.linkedin.com/**", fake.handle)
        yield context, page, fake


def test_login_saves_session_json(browser):
    context, page, _ = browser
    assert not linkedin_login.is_logged_in(page)
    linkedin_login.login(context, page, headless=True)
    assert linkedin_login.is_logged_in(page)
    assert "li_at" in settings.session_file().read_text()


def test_wrong_password(browser, monkeypatch):
    context, page, _ = browser
    monkeypatch.setenv("LINKEDIN_PASSWORD", "nope")
    with pytest.raises(linkedin_login.LoginError, match="Wrong email or password"):
        linkedin_login.login(context, page, headless=True)


def test_checkpoint_headless_points_to_option_2(browser, monkeypatch):
    context, page, _ = browser
    monkeypatch.setenv("LINKEDIN_EMAIL", "needs2fa@example.com")
    with pytest.raises(linkedin_login.LoginError, match="option 2"):
        linkedin_login.login(context, page, headless=True)


def test_scrape_filters_and_saves_recruiters(browser):
    context, page, fake = browser
    linkedin_login.login(context, page, headless=True)
    stats = linkedin_scraper.scrape_candidate("jane", page=page, max_scrolls=1, pause=(0, 0))

    params = parse_qs(urlparse(fake.search_urls[0]).query)
    assert params["keywords"] == ['"Java Developer" C2C -hotlist -fulltime -"available consultants" -Bench']
    assert params["datePosted"] == ['"past-24h"'] and params["sortBy"] == ['"date_posted"']
    assert params["geoUrn"] == ['["103644278"]']

    assert stats["cards"] == 5  # the post without an email is ignored
    assert stats["dropped"] == {"bench marketing": 1, "non-USA": 1}
    assert stats["saved"] == 2 and stats["mismatched_role"] == 1

    rows = {r["recruiter_email"]: r for r in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)}
    assert set(rows) == {"john.smith@techcorp.com", "maria@staffhub.com", "amy@qaworks.com"}
    john, maria, amy = rows["john.smith@techcorp.com"], rows["maria@staffhub.com"], rows["amy@qaworks.com"]
    assert john["recruiter_name"] == "John Smith"
    assert john["post_url"] == "https://www.linkedin.com/feed/update/urn:li:activity:7100000000000000001/"
    assert "Senior Java Developer" in john["job_description"] and john["email_sent_status"] == "pending"
    assert "Send resumes" in john["job_description"]  # text revealed by clicking "…more"
    assert page.evaluate("window.toggleCount") == 1  # expanded once, never collapsed again
    # resolved through the three-dots menu + clipboard, query string stripped
    assert maria["post_url"] == "https://www.linkedin.com/posts/maria-garcia-77_java-c2c-activity-7100000000000000002-AbCd"
    assert amy["email_sent_status"] == "mismatched_role"
    assert csv_store.parse_time(john["timestamp"]) is not None

    again = linkedin_scraper.scrape_candidate("jane", page=page, max_scrolls=0, pause=(0, 0))
    assert again["duplicates"] == 3 and again["saved"] == 0


def test_full_batch_pipeline(workspace, monkeypatch):
    """Option 8 end to end: login -> scrape -> filter -> tailor resume (real DOCX->PDF) -> send -> log -> analytics."""
    import analytics
    import gmail_sender
    import main
    from test_gmail_sender import FakeSMTP

    fake, real_context = FakeLinkedIn(), linkedin_login.browser_context

    from contextlib import contextmanager

    @contextmanager
    def routed(**kwargs):
        with real_context(**kwargs) as (context, page):
            context.route("https://www.linkedin.com/**", fake.handle)
            yield context, page

    server = FakeSMTP()
    monkeypatch.setattr(linkedin_login, "browser_context", routed)
    monkeypatch.setattr(linkedin_scraper, "human_pause", lambda *a: None)
    monkeypatch.setattr(gmail_sender, "smtp_connect", lambda: server)
    monkeypatch.setattr(gmail_sender.time, "sleep", lambda s: None)

    main.run_batch("live", daily_limit=10, assume_yes=True)

    assert sorted(m["To"] for m in server.sent) == ["john.smith@techcorp.com", "maria@staffhub.com"]
    john = next(m for m in server.sent if m["To"] == "john.smith@techcorp.com")
    assert "Spring Boot" in john.get_body(("plain",)).get_content()
    attachment = next(john.iter_attachments())
    assert attachment.get_filename().startswith("Jane_Doe_Resume_")
    history = csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
    assert {h["email_sent_status"] for h in history} == {"sent"} and len(history) == 2
    assert analytics.compute_stats()["emails_sent"] == 2
    assert settings.analytics_report().exists()

    # a second batch the same day sends nothing new
    main.run_batch("live", daily_limit=10, assume_yes=True)
    assert len(server.sent) == 2


def test_session_is_reused_without_logging_in_again(workspace, monkeypatch):
    fake = FakeLinkedIn()
    with linkedin_login.browser_context(headless=True) as (context, page):
        context.route("https://www.linkedin.com/**", fake.handle)
        linkedin_login.login(context, page, headless=True)
    monkeypatch.setenv("LINKEDIN_PASSWORD", "wrong")  # would fail if it tried to log in again
    with linkedin_login.browser_context(headless=True) as (context, page):  # loads session.json
        context.route("https://www.linkedin.com/**", fake.handle)
        linkedin_login.login(context, page, headless=True)
        assert linkedin_login.is_logged_in(page)


def test_expired_session_falls_back_to_credentials(workspace):
    settings.session_file().write_text('{"cookies": [], "origins": []}')
    fake = FakeLinkedIn()
    with linkedin_login.browser_context(headless=True) as (context, page):
        context.route("https://www.linkedin.com/**", fake.handle)
        linkedin_login.login(context, page, headless=True)
        assert linkedin_login.has_session_cookie(page)
    assert "li_at" in settings.session_file().read_text()


def test_unknown_layout_uses_generic_result_items(workspace, monkeypatch):
    monkeypatch.setattr(linkedin_scraper, "CARD_WAIT_MS", 1500)
    fake = FakeLinkedIn("search_results_new_layout.html")
    with linkedin_login.browser_context(headless=True) as (context, page):
        context.route("https://www.linkedin.com/**", fake.handle)
        linkedin_login.login(context, page, headless=True)
        stats = linkedin_scraper.scrape_candidate("jane", page=page, max_scrolls=0, pause=(0, 0))
    assert stats["saved"] == 1
    row = csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)[0]
    assert row["recruiter_email"] == "kate@nexgen.io" and row["recruiter_name"] == "Kate Wu"
