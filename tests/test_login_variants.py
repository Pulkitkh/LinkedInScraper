"""Every LinkedIn sign-in page variant seen in the wild, plus the manual-login fallback in a visible browser."""

import pytest

import linkedin_login
import settings
from test_e2e_linkedin import FakeLinkedIn, _browser_available

pytestmark = pytest.mark.skipif(not _browser_available(), reason="Chromium not installed")

SUCCESS = "location.href = '/checkpoint/lg/login-submit';"

CHECK = """
function ok(u, p) { return u === 'operator@example.com' && p === 'correct-password'; }
"""

VARIANTS = {
    # homepage-style form (ids session_key / session_password, "Sign in" button without type=submit)
    "homepage": f"""<form onsubmit="return false"><input id="session_key" name="session_key" type="text">
      <input id="session_password" name="session_password" type="password">
      <button onclick="if (ok(session_key.value, session_password.value)) {{ {SUCCESS} }}">Sign in</button></form>
      <script>{CHECK}</script>""",
    # two-step: email first, password field appears after "Continue"
    "two_step": f"""<form onsubmit="event.preventDefault(); step()"><input type="email" autocomplete="username" id="e">
      <div id="pw" style="display:none"><input type="password" id="p"></div>
      <button type="submit">Continue</button></form>
      <script>{CHECK}
      function step() {{
        const pw = document.getElementById('pw');
        if (pw.style.display === 'none') {{ pw.style.display = 'block'; return; }}
        if (ok(e.value, p.value)) {{ {SUCCESS} }}
      }}</script>""",
    # "Welcome back" account picker hiding the real form
    "welcome_back": f"""<div id="picker"><p>Welcome back</p><button onclick="pick()">Pulkit K.</button>
      <button onclick="pick()">Sign in using another account</button></div>
      <form id="f" style="display:none" onsubmit="event.preventDefault(); if (ok(username.value, password.value)) {{ {SUCCESS} }}">
      <input id="username" name="session_key"><input id="password" type="password"><button type="submit">Sign in</button></form>
      <script>{CHECK} function pick() {{ picker.style.display = 'none'; f.style.display = 'block'; }}</script>""",
    # unrecognisable page (new redesign / bot check); a human signs in after a moment
    "unknown_manual": f"""<div>Please verify you are human</div>
      <script>setTimeout(() => {{ {SUCCESS} }}, 1500);</script>""",
    # wrong password, and nobody fixes it
    "wrong_password": """<form onsubmit="event.preventDefault(); document.getElementById('err').style.display='block'">
      <input id="username"><input id="password" type="password">
      <div id="err" style="display:none"><span id="error-for-password">Wrong email or password. Try again.</span></div>
      <button type="submit">Sign in</button></form>""",
    # wrong password first; the human fixes it in the window
    "wrong_then_fixed": f"""<form onsubmit="event.preventDefault()"><input id="username"><input id="password" type="password">
      <div id="error-for-password">Wrong email or password. Try again.</div><button type="submit">Sign in</button></form>
      <script>setTimeout(() => {{ {SUCCESS} }}, 1500);</script>""",
}


class VariantLinkedIn(FakeLinkedIn):
    def __init__(self, variant):
        super().__init__()
        self.variant = variant

    def handle(self, route):
        if "/login" in route.request.url and "login-submit" not in route.request.url:
            route.fulfill(content_type="text/html", body=f"<html><body>{VARIANTS[self.variant]}</body></html>")
        else:
            super().handle(route)


def _login(variant, headless_logic):
    fake = VariantLinkedIn(variant)
    with linkedin_login.browser_context(headless=True) as (context, page):  # real browser is always headless here
        context.route("https://www.linkedin.com/**", fake.handle)
        linkedin_login.login(context, page, headless=headless_logic, checkpoint_timeout=20)
        return linkedin_login.is_logged_in(page)


@pytest.mark.parametrize("variant", ["homepage", "two_step", "welcome_back"])
def test_auto_fill_every_form_variant(workspace, variant):
    assert _login(variant, headless_logic=True)
    assert "li_at" in settings.session_file().read_text()


def test_unknown_page_visible_browser_waits_for_manual_login(workspace):
    assert _login("unknown_manual", headless_logic=False)
    assert settings.session_file().exists()


def test_unknown_page_headless_explains_and_saves_screenshot(workspace):
    with pytest.raises(linkedin_login.LoginError, match="option 2") as exc:
        _login("unknown_manual", headless_logic=True)
    assert "screenshot" in str(exc.value)
    assert (settings.scratch_dir() / "login_debug.png").exists()
    assert (settings.scratch_dir() / "login_debug.html").exists()


def test_wrong_password_visible_browser_keeps_waiting(workspace):
    assert _login("wrong_then_fixed", headless_logic=False)


def test_wrong_password_headless_fails_fast(workspace):
    with pytest.raises(linkedin_login.LoginError, match="Wrong email or password"):
        _login("wrong_password", headless_logic=True)


def test_no_credentials_visible_browser_waits_for_manual_login(workspace, monkeypatch):
    monkeypatch.setenv("LINKEDIN_PASSWORD", "")
    assert _login("unknown_manual", headless_logic=False)


def test_corrupt_session_file_is_ignored(workspace):
    settings.session_file().write_text("{not json")
    assert _login("homepage", headless_logic=True)


def test_closed_browser_window_gives_clear_error(workspace, monkeypatch):
    from playwright.sync_api import Error

    def closed(*a, **k):
        raise Error("Target page, context or browser has been closed")

    real = linkedin_login.browser_context
    monkeypatch.setattr(linkedin_login, "browser_context", lambda headless, use_session: real(headless=True))
    monkeypatch.setattr(linkedin_login, "login", closed)
    with pytest.raises(linkedin_login.LoginError, match="window was closed"):
        linkedin_login.setup_login_session()


def test_user_agent_matches_real_browser(workspace):
    with linkedin_login.browser_context(headless=True) as (context, page):
        ua = page.evaluate("navigator.userAgent")
        version = context.browser.version
    assert "HeadlessChrome" not in ua and f"Chrome/{version}" in ua
