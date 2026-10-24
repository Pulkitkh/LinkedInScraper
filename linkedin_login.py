"""Headful LinkedIn login that saves cookies/local storage to session.json for headless reuse."""

from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Page, sync_playwright

import settings

log = logging.getLogger(__name__)

LINKEDIN = "https://www.linkedin.com"
GLOBAL_NAV = "#global-nav, header.global-nav, nav.global-nav"
LOGIN_FORM = "input[name=session_key], input#username"
_LOGGED_OUT_MARKERS = ("/login", "/authwall", "/uas/", "/signup", "/checkpoint")
_CHECKPOINT_MARKERS = ("/checkpoint", "/challenge")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    pass


def human_pause(low: float = 0.6, high: float = 1.8) -> None:
    if high > 0:
        time.sleep(random.uniform(low, high))


@contextmanager
def browser_context(headless: bool = True, use_session: bool = True):
    """Yield (context, page). Loads session.json when present and grants clipboard access for 'Copy link'."""
    with sync_playwright() as pw:
        launch = {"headless": headless, "slow_mo": 0 if headless else 50}
        if exe := settings.env("CHROMIUM_EXECUTABLE"):
            launch["executable_path"] = exe
        browser = pw.chromium.launch(**launch)
        opts = {"user_agent": USER_AGENT, "viewport": {"width": 1366, "height": 900}, "locale": "en-US"}
        if use_session and settings.session_file().exists():
            opts["storage_state"] = str(settings.session_file())
        context = browser.new_context(**opts)
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=LINKEDIN)
        page = context.new_page()
        try:
            yield context, page
        finally:
            browser.close()


def is_logged_in(page: Page, navigate: bool = True) -> bool:
    if navigate:
        page.goto(f"{LINKEDIN}/feed/", wait_until="domcontentloaded")
    url = page.url
    if "linkedin.com" not in url or any(m in url for m in _LOGGED_OUT_MARKERS):
        return False
    if page.query_selector(LOGIN_FORM):
        return False
    try:
        page.wait_for_selector(GLOBAL_NAV, timeout=8000)
        return True
    except Exception:
        return False


def save_session(context: BrowserContext) -> None:
    path = settings.session_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    log.info("Session saved to %s", path)


def login(context: BrowserContext, page: Page, headless: bool, checkpoint_timeout: float = 300) -> None:
    """Make sure the page is logged in, auto-filling credentials from .env when available."""
    if is_logged_in(page):
        log.info("LinkedIn session is valid")
        return

    email, password = settings.env("LINKEDIN_EMAIL"), settings.env("LINKEDIN_PASSWORD")
    page.goto(f"{LINKEDIN}/login", wait_until="domcontentloaded")
    if email and password:
        log.info("Signing in to LinkedIn as %s", email)
        page.fill("#username", email)
        human_pause(0.3, 0.9)
        page.fill("#password", password)
        human_pause(0.3, 0.9)
        page.click("button[type=submit]")
    elif headless:
        raise LoginError("No valid session.json and no LINKEDIN_EMAIL/LINKEDIN_PASSWORD in .env. "
                         "Run main.py option 2 to log in.")
    else:
        log.warning("Enter your LinkedIn credentials in the browser window...")

    _wait_for_feed(page, headless, checkpoint_timeout)
    save_session(context)


def _wait_for_feed(page: Page, headless: bool, timeout: float) -> None:
    deadline = time.time() + (30 if headless else timeout)
    warned = False
    while time.time() < deadline:
        url = page.url
        if any(m in url for m in _CHECKPOINT_MARKERS):
            if headless:
                raise LoginError("LinkedIn security checkpoint (CAPTCHA/2FA). Run main.py option 2 "
                                 "to complete it once in a visible browser.")
            if not warned:
                log.warning("Complete the LinkedIn security check in the browser window...")
                warned = True
        elif "/feed" in url and page.query_selector(GLOBAL_NAV):
            return
        for sel in ("#error-for-password", "#error-for-username"):
            el = page.query_selector(sel)
            if el and el.is_visible() and el.inner_text().strip():
                raise LoginError(f"LinkedIn rejected the login: {el.inner_text().strip()}")
        page.wait_for_timeout(500)
    raise LoginError(f"Timed out waiting for the LinkedIn feed (stuck on {page.url})")


def setup_login_session() -> bool:
    """Menu option 2: open a visible browser, log in (solving any CAPTCHA/2FA), save session.json."""
    with browser_context(headless=False, use_session=True) as (context, page):
        login(context, page, headless=False)
        return is_logged_in(page)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Logged in." if setup_login_session() else "Login failed.")
