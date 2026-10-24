"""Headful LinkedIn login that saves cookies/local storage to session.json for headless reuse."""

from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

import settings

log = logging.getLogger(__name__)

LINKEDIN = "https://www.linkedin.com"
GLOBAL_NAV = "#global-nav, header.global-nav, nav.global-nav"
# LinkedIn serves several sign-in pages (classic /login, the homepage form, a two-step email-then-password
# page, and a "Welcome back" account picker). Each field lists every variant seen; first visible one wins.
EMAIL_FIELDS = [
    "#username", "input[name='session_key']", "#session_key", "input[autocomplete='username']",
    "input[autocomplete='email']", "input[type='email']", "input[id*='email' i]",
]
PASSWORD_FIELDS = [
    "#password", "input[name='session_password']", "#session_password",
    "input[autocomplete='current-password']", "input[type='password']",
]
SUBMIT_BUTTONS = [
    "button[type='submit']", "button[data-litms-control-urn='login-submit']",
    "button:has-text('Sign in')", "button:has-text('Continue')", "button:has-text('Next')",
]
OTHER_ACCOUNT = (
    "button:has-text('Sign in using another account'), a:has-text('Sign in using another account'), "
    "button:has-text('Use another account'), a:has-text('Use another account')"
)
LOGIN_FORM = ", ".join(["input[name=session_key]", "input#username", "input[name=session_password]"])
_LOGGED_OUT_MARKERS = ("/login", "/authwall", "/uas/", "/signup", "/checkpoint")
_CHECKPOINT_MARKERS = ("/checkpoint", "/challenge")


class LoginError(RuntimeError):
    pass


def human_pause(low: float = 0.6, high: float = 1.8) -> None:
    if high > 0:
        time.sleep(random.uniform(low, high))


def _user_agent(browser_version: str) -> str:
    """A normal desktop Chrome UA matching the real engine version (no 'HeadlessChrome', no stale version)."""
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{browser_version} Safari/537.36")


@contextmanager
def browser_context(headless: bool = True, use_session: bool = True):
    """Yield (context, page). Loads session.json when present and grants clipboard access for 'Copy link'.

    BROWSER_CHANNEL=msedge or chrome uses your installed Edge/Chrome instead of Playwright's Chromium.
    """
    pw = sync_playwright().start()
    browser = None
    try:
        launch = {"headless": headless, "slow_mo": 0 if headless else 50,
                  "args": ["--disable-blink-features=AutomationControlled"]}
        if exe := settings.env("CHROMIUM_EXECUTABLE"):
            launch["executable_path"] = exe
        elif channel := settings.env("BROWSER_CHANNEL"):
            launch["channel"] = channel
        browser = pw.chromium.launch(**launch)
        opts = {"user_agent": _user_agent(browser.version), "viewport": {"width": 1366, "height": 900},
                "locale": "en-US"}
        if use_session and settings.session_file().exists():
            try:
                context = browser.new_context(storage_state=str(settings.session_file()), **opts)
            except Exception as exc:  # corrupt/edited session.json: start clean, log in again
                log.warning("Ignoring unreadable session.json (%s)", exc)
                context = browser.new_context(**opts)
        else:
            context = browser.new_context(**opts)
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=LINKEDIN)
        yield context, context.new_page()
    finally:
        # The window may have been closed by hand or the driver may be gone; cleanup must never fail the run.
        for close in ((browser.close if browser else None), pw.stop):
            if close:
                try:
                    close()
                except Exception as exc:
                    log.debug("Ignoring error while closing the browser: %s", exc)


def has_session_cookie(page: Page) -> bool:
    """li_at is LinkedIn's authentication cookie; it only exists for a signed-in browser."""
    return any(c["name"] == "li_at" and c["value"] for c in page.context.cookies(LINKEDIN))


def _on_logged_in_page(page: Page) -> bool:
    url = page.url
    return ("linkedin.com" in url and not any(m in url for m in _LOGGED_OUT_MARKERS)
            and page.query_selector(LOGIN_FORM) is None)


def is_logged_in(page: Page, navigate: bool = True) -> bool:
    if navigate:
        page.goto(f"{LINKEDIN}/feed/", wait_until="domcontentloaded")
    if not _on_logged_in_page(page):
        return False
    if has_session_cookie(page):
        return True
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


def _first_visible(page: Page, selectors: list[str], timeout_ms: int):
    """First visible element matching any selector, polling until timeout; None if nothing shows up."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible() and el.is_enabled():
                    return el
            except Exception:  # page navigating; try again
                pass
        if time.time() >= deadline:
            return None
        page.wait_for_timeout(250)


def _save_debug(page: Page) -> str:
    """Screenshot + HTML of an unexpected login page so the selectors can be updated."""
    base = settings.scratch_dir() / "login_debug"
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        (base.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
        return f"{base}.png"
    except Exception:
        return ""


def _fill_credentials(page: Page, email: str, password: str) -> bool:
    """Fill whichever LinkedIn sign-in form is showing. Returns False if no form was recognised."""
    try:
        other = page.query_selector(OTHER_ACCOUNT)
        if other and other.is_visible():  # "Welcome back" picker: switch to the normal form
            other.click()
            page.wait_for_timeout(1000)

        email_el = _first_visible(page, EMAIL_FIELDS, 15000)
        password_el = _first_visible(page, PASSWORD_FIELDS, 0)
        if email_el is None and password_el is None:
            return False
        if email_el is not None:
            email_el.fill(email)
            human_pause(0.3, 0.9)
        if password_el is None:  # two-step form: submit the email, then the password field appears
            email_el.press("Enter")
            password_el = _first_visible(page, PASSWORD_FIELDS, 15000)
            if password_el is None:
                return False
        password_el.fill(password)
        human_pause(0.3, 0.9)
        submit = _first_visible(page, SUBMIT_BUTTONS, 2000)
        if submit is not None:
            submit.click()
        else:
            password_el.press("Enter")
    except PlaywrightError as exc:
        # The page moved on while we were typing (LinkedIn redirect, or the user signing in by hand).
        # Whether that ended on the feed is decided by _wait_for_feed.
        log.debug("Sign-in form changed while filling it: %s", exc)
    return True


def login(context: BrowserContext, page: Page, headless: bool, checkpoint_timeout: float = 300) -> None:
    """Make sure the page is logged in, auto-filling credentials from .env when available.

    In a visible browser this never gives up early: if the form can't be filled automatically,
    you can sign in by hand in the window and the session is saved once the feed loads.
    """
    if is_logged_in(page):
        log.info("LinkedIn session is valid")
        return

    email, password = settings.env("LINKEDIN_EMAIL"), settings.env("LINKEDIN_PASSWORD")
    page.goto(f"{LINKEDIN}/login", wait_until="domcontentloaded")
    if _on_logged_in_page(page) and has_session_cookie(page):  # /login bounced straight to the feed
        save_session(context)
        return

    filled = False
    if email and password:
        log.info("Signing in to LinkedIn as %s", email)
        filled = _fill_credentials(page, email, password)

    if not filled:
        if headless:
            shot = _save_debug(page)
            raise LoginError("Could not find the LinkedIn sign-in form on " + page.url
                             + (f" (screenshot: {shot})" if shot else "")
                             + ". Run main.py option 2 to sign in once in a visible browser.")
        log.warning("Please sign in to LinkedIn in the browser window (waiting up to %d s)...",
                    int(checkpoint_timeout))

    _wait_for_feed(page, headless, checkpoint_timeout)
    save_session(context)


def _login_error(page: Page) -> str:
    for sel in ("#error-for-password", "#error-for-username", "form [role='alert']", ".form__label--error"):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.inner_text().strip():
                return el.inner_text().strip()
        except Exception:
            pass
    return ""


def _is_checkpoint(url: str) -> bool:
    # /checkpoint/lg/login-submit is the normal login POST on its way to the feed, not a challenge
    return any(m in url for m in _CHECKPOINT_MARKERS) and "login-submit" not in url


def _wait_for_feed(page: Page, headless: bool, timeout: float) -> None:
    deadline = time.time() + (45 if headless else timeout)
    checkpoint_since = None
    shown_error = ""
    while time.time() < deadline:
        url = page.url
        if _is_checkpoint(url):
            if checkpoint_since is None:
                checkpoint_since = time.time()
                if not headless:
                    log.warning("Complete the LinkedIn security check (CAPTCHA/2FA/PIN) in the browser window...")
            elif headless and time.time() - checkpoint_since > 4:
                raise LoginError("LinkedIn security checkpoint (CAPTCHA/2FA). Run main.py option 2 "
                                 "to complete it once in a visible browser.")
        else:
            checkpoint_since = None
            if _on_logged_in_page(page) and (has_session_cookie(page) or page.query_selector(GLOBAL_NAV)):
                return
        message = _login_error(page)
        if message:
            if headless:
                raise LoginError(f"LinkedIn rejected the login: {message}")
            if message != shown_error:  # visible browser: let the user correct it and keep waiting
                log.warning("LinkedIn says: %s - fix it in the browser window", message)
                shown_error = message
        page.wait_for_timeout(500)
    raise LoginError(f"Timed out waiting for the LinkedIn feed (stuck on {page.url})")


def setup_login_session() -> bool:
    """Menu option 2: open a visible browser, log in (solving any CAPTCHA/2FA), save session.json."""
    try:
        with browser_context(headless=False, use_session=True) as (context, page):
            login(context, page, headless=False)
            return is_logged_in(page)
    except PlaywrightError as exc:
        if "closed" in str(exc).lower():
            raise LoginError("The browser window was closed before the login finished") from exc
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Logged in." if setup_login_session() else "Login failed.")
