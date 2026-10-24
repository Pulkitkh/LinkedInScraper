"""Playwright scraper: searches LinkedIn posts (last 24h, US), extracts recruiter emails, applies strict filters."""

from __future__ import annotations

import logging
import re
from contextlib import nullcontext
from urllib.parse import urlencode, urlsplit, urlunsplit

from playwright.sync_api import ElementHandle, Page

import client_config
import csv_store
import settings
from linkedin_login import LINKEDIN, browser_context, human_pause, login

log = logging.getLogger(__name__)

SEARCH_URL = f"{LINKEDIN}/search/results/content/"
MAX_SCROLLS = 6
CARD_WAIT_MS = 15000  # how long to wait for post cards before trying the generic fallback

# LinkedIn markup changes often; each field has fallbacks (first match wins).
SELECTORS = {
    "card": (
        'div[data-urn^="urn:li:activity:"], div[data-urn^="urn:li:share:"], div[data-urn^="urn:li:ugcPost:"], '
        'div[data-id^="urn:li:activity:"], div.feed-shared-update-v2:not([data-urn]), '
        'div[data-view-name="feed-full-update"]'
    ),
    # Used only when none of the card selectors match (LinkedIn layout change): plain result list items.
    "fallback_card": "main ul > li",
    "control_menu": "button[aria-label*='control menu for post' i], button.feed-shared-control-menu__trigger",
    # LinkedIn labels it "Copy link to post"; scoped to menus so a post mentioning "copy link" never matches.
    "copy_link": "[role='menu'] [role='menuitem']:has-text('Copy link'), "
                 "[role='menu'] [role='button']:has-text('Copy link'), "
                 ".feed-shared-control-menu__item:has-text('Copy link'), "
                 ".artdeco-dropdown__content-inner li:has-text('Copy link')",
    "show_more_results": "button.scaffold-finite-scroll__load-button",
}

# Clicks only collapsed "…more" toggles (the same button turns into "…less" once expanded).
EXPAND_JS = r"""
() => {
  let clicked = 0;
  for (const b of document.querySelectorAll('button')) {
    const label = ((b.getAttribute('aria-label') || '') + ' ' + b.innerText).toLowerCase();
    const isToggle = b.classList.contains('feed-shared-inline-show-more-text__see-more-less-toggle')
      || /see more|…more|\.\.\.more/.test(label);
    if (isToggle && !/less/.test(label) && b.getAttribute('aria-expanded') !== 'true') {
      b.click();
      clicked++;
    }
  }
  return clicked;
}
"""

CARD_JS = """
(node) => {
  const pick = (list) => {
    for (const s of list) {
      const el = node.querySelector(s);
      if (el && el.innerText && el.innerText.trim()) return el;
    }
    return null;
  };
  const txt = (el) => el ? el.innerText.replace(/\\u00a0/g, ' ').trim() : '';
  const actor = node.querySelector(
    'a.update-components-actor__meta-link, .update-components-actor__container a[href*="/in/"], a[href*="/in/"]');
  const urnNode = node.closest('[data-urn]') || node;
  return {
    urn: node.getAttribute('data-urn') || node.getAttribute('data-id') || urnNode.getAttribute('data-urn') || '',
    text: txt(pick(['.update-components-text', '.feed-shared-inline-show-more-text',
                    '.feed-shared-update-v2__description', '.feed-shared-text'])) || txt(node),
    actor_name: txt(pick(['.update-components-actor__title span[aria-hidden="true"]',
                          '.update-components-actor__name span[aria-hidden="true"]',
                          '.update-components-actor__title', '.update-components-actor__name'])),
    profile_url: actor ? actor.href : '',
    mailtos: Array.from(node.querySelectorAll('a[href^="mailto:"]')).map(a => a.getAttribute('href')),
  };
}
"""

# ------------------------------------------------------------------ text analysis

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_AT = re.compile(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", re.I)
_DOT = re.compile(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", re.I)
_FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".docx")

NON_USA_INDICATORS = [
    "india", "pune", "noida", "bangalore", "bengaluru", "hyderabad", "chennai", "mumbai", "gurgaon",
    "gurugram", "kolkata", "ahmedabad", "kochi", "canada", "toronto", "vancouver", "mississauga",
    "united kingdom", "london", "uk only", "dubai", "uae", "singapore", "australia", "sydney", "melbourne",
    "germany", "berlin", "philippines", "manila", "malaysia", "europe", "emea", "apac", "ist",
]
USA_STATE_CODES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
    "NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
USA_CITIES = [
    "new york", "nyc", "los angeles", "chicago", "houston", "phoenix", "philadelphia", "san antonio",
    "san diego", "dallas", "austin", "jacksonville", "san jose", "fort worth", "columbus", "charlotte",
    "indianapolis", "san francisco", "seattle", "denver", "nashville", "boston", "detroit", "atlanta",
    "miami", "tampa", "orlando", "minneapolis", "pittsburgh", "raleigh", "plano", "irving", "jersey city",
    "newark", "richmond", "reston", "mclean", "st. louis", "kansas city", "salt lake city", "cincinnati",
    "cleveland", "baltimore", "bay area", "silicon valley", "sunnyvale", "princeton", "hartford", "frisco",
]
_NON_USA_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, NON_USA_INDICATORS)) + r")\b", re.I)
_STATE_RE = re.compile(r"[A-Za-z.]+,\s*(?:" + USA_STATE_CODES + r")\b")
_CITY_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, USA_CITIES)) + r")\b", re.I)
_US_TERMS_RE = re.compile(
    r"\b(?:USA|U\.S\.A?\.?|United States|US[\s-]based|US only|within (?:the )?US|remote[\s,(-]+US|"
    r"anywhere in (?:the )?US|US citizens?|USC|green card|GC|H-?1-?B|H4[\s-]?EAD|GC[\s-]?EAD|EAD|"
    r"EST|CST|MST|PST)\b"
)
_TITLE_LABEL_RE = re.compile(
    r"^\s*[-*•]?\s*(?:job\s*title|title|role|position|requirement|hiring(?:\s+for)?|opening)\s*[:\-–]\s*(.+?)\s*$",
    re.I | re.M,
)
_TITLE_WORDS_RE = re.compile(r"developer|engineer|architect|analyst|tester|lead|manager|scientist|administrator", re.I)


def extract_emails(text: str, mailtos: list[str] | None = None) -> list[str]:
    """mailto: links first, then emails found in whitespace-separated tokens of the post text."""
    found: list[str] = []

    def add(candidate: str) -> None:
        email = candidate.strip(".,;:()[]<>\"'").lower()
        if EMAIL_RE.fullmatch(email) and not email.endswith(_FILE_SUFFIXES) and email not in found:
            found.append(email)

    for href in mailtos or []:
        add(href.split(":", 1)[-1].split("?")[0])
    clean = _DOT.sub(".", _AT.sub("@", text))
    for token in clean.split():
        for match in EMAIL_RE.findall(token):
            add(match)
    return found


def is_usa_job(text: str) -> bool:
    if _NON_USA_RE.search(text):
        return False
    return bool(_STATE_RE.search(text) or _CITY_RE.search(text) or _US_TERMS_RE.search(text))


def is_bench_marketing(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in client_config.BENCH_MARKETING_PHRASES)


def detect_job_title(text: str) -> str:
    m = _TITLE_LABEL_RE.search(text)
    if m:
        return m.group(1).strip()[:120]
    for line in text.splitlines():
        line = re.sub(r"#\w+", "", line).strip(" -–:|•*")
        if line and len(line) <= 120 and _TITLE_WORDS_RE.search(line):
            return line
    return ""


def domains_in(text: str) -> set[str]:
    return {name for name, pattern in client_config.ROLE_DOMAINS.items() if re.search(pattern, text, re.I)}


def role_mismatch(text: str, candidate: dict) -> bool:
    """True when the declared job title targets a domain the candidate doesn't do (e.g. QA for a Java dev)."""
    allowed = set(candidate.get("allowed_domains", []))
    if not allowed:
        return False
    title_domains = domains_in(detect_job_title(text))
    if (title_domains & set(client_config.EXCLUSIVE_DOMAINS)) - allowed:
        return True  # e.g. "QA Tester - Java" is a QA role even though it mentions Java
    if title_domains:
        return not (title_domains & allowed)
    return not (domains_in(text) & allowed)


def matches_strict_filter(text: str, candidate: dict) -> tuple[str | None, str]:
    """Return (status, reason). status is 'pending', 'mismatched_role', or None (drop the post)."""
    if is_bench_marketing(text):
        return None, "bench marketing"
    if not is_usa_job(text):
        return None, "non-USA"
    if role_mismatch(text, candidate):
        return "mismatched_role", "role mismatch"
    return "pending", ""


def recruiter_name_from_profile(profile_url: str, fallback: str = "") -> str:
    """'https://www.linkedin.com/in/john-smith-4b2a91/' -> 'John Smith'. Prefers the visible name if given."""
    visible = re.sub(r"\(.*?\)|[^\w\s.'-]", " ", fallback.split("\n")[0].split(",")[0])
    visible = re.sub(r"\s+", " ", visible).strip()
    if visible:
        return visible
    m = re.search(r"/in/([^/?#]+)", profile_url or "")
    if not m:
        return ""
    parts = [p for p in m.group(1).split("-") if p and not re.search(r"\d", p)]
    return " ".join(p.capitalize() for p in parts)


def clean_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


# ------------------------------------------------------------------ search

def build_search_query(role: str, exclusions: list[str] | None = None) -> str:
    """'"Java Developer" C2C -hotlist -fulltime -"available consultants" -Bench'"""
    excluded = exclusions if exclusions is not None else client_config.DEFAULT_EXCLUSION_KEYWORDS
    terms = [f'-"{k}"' if " " in k else f"-{k}" for k in excluded]
    return " ".join([f'"{role}"', "C2C", *terms])


def build_search_url(query: str) -> str:
    params = {
        "keywords": query,
        "datePosted": '"past-24h"',
        "sortBy": '"date_posted"',
        "geoUrn": f'["{client_config.USA_GEO_URN}"]',
        "origin": "FACETED_SEARCH",
    }
    return f"{SEARCH_URL}?{urlencode(params)}"


def _expand_see_more(page: Page) -> None:
    try:
        page.evaluate(EXPAND_JS)
    except Exception:  # navigation in progress; the full text is usually in the DOM anyway
        pass


def copy_post_link(page: Page, card: ElementHandle) -> str:
    """Resolve a post URL through the three-dots menu -> 'Copy link' -> clipboard."""
    try:
        card.evaluate("n => n.scrollIntoView({block: 'center'})")
        page.evaluate("() => navigator.clipboard.writeText('')")
        menu = card.query_selector(SELECTORS["control_menu"])
        if not menu:
            return ""
        menu.click(force=True)
        option = page.wait_for_selector(SELECTORS["copy_link"], timeout=4000)
        option.click(force=True)
        page.wait_for_timeout(400)
        url = page.evaluate("() => navigator.clipboard.readText()") or ""
        page.keyboard.press("Escape")
        return clean_url(url) if url.startswith("http") else ""
    except Exception as exc:
        log.debug("Copy link failed: %s", exc)
        page.keyboard.press("Escape")
        return ""


def scrape_role(page: Page, role: str, exclusions: list[str], max_scrolls: int = MAX_SCROLLS,
                pause: tuple[float, float] = (1.5, 3.0)) -> list[dict]:
    """Return raw cards ({post_url, text, actor_name, profile_url, emails}) for one search role."""
    query = build_search_query(role, exclusions)
    log.info("Searching LinkedIn: %s", query)
    page.goto(build_search_url(query), wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SELECTORS["card"], timeout=CARD_WAIT_MS)
    except Exception:
        if not page.query_selector(SELECTORS["fallback_card"]):
            log.warning("No posts found for %r", role)
            return []

    for _ in range(max_scrolls):
        _expand_see_more(page)
        page.mouse.wheel(0, 2500)
        human_pause(*pause)
        try:
            more = page.query_selector(SELECTORS["show_more_results"])
            if more and more.is_visible():
                more.click()
                human_pause(*pause)
        except Exception:  # button re-rendered or gone; scrolling continues
            pass
    _expand_see_more(page)

    handles = page.query_selector_all(SELECTORS["card"])
    if not handles:
        log.warning("Post cards not recognised (LinkedIn layout changed?) - using generic result items")
        handles = page.query_selector_all(SELECTORS["fallback_card"])

    cards: dict[str, dict] = {}
    for handle in handles:
        try:
            data = handle.evaluate(CARD_JS)
        except Exception as exc:  # LinkedIn re-rendered the list and this card is gone
            log.debug("Skipping a card that disappeared: %s", exc)
            continue
        emails = extract_emails(data["text"], data["mailtos"])
        if not emails:
            continue  # only posts with a recruiter email are useful
        if data["urn"]:
            post_url = f"{LINKEDIN}/feed/update/{data['urn']}/"
        else:
            post_url = copy_post_link(page, handle)
        key = post_url or data["text"][:200]
        cards.setdefault(key, {**data, "post_url": post_url, "emails": emails})
    log.info("  %d posts with emails for %r", len(cards), role)
    return list(cards.values())


def scrape_candidate(candidate_key: str, page: Page | None = None, headless: bool = True,
                     max_scrolls: int = MAX_SCROLLS, pause: tuple[float, float] = (1.5, 3.0)) -> dict:
    """Search every role of a candidate, filter, and append new rows to recruiters.csv."""
    candidate = client_config.get_candidate(candidate_key)
    csv_store.migrate(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)
    existing = {(r["recruiter_email"], r["post_url"])
                for r in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)}
    stats = {"cards": 0, "saved": 0, "mismatched_role": 0, "dropped": {}, "duplicates": 0}
    new_rows: list[dict] = []

    ctx = nullcontext((None, page)) if page else browser_context(headless=headless)
    with ctx as (context, pg):
        if context is not None:
            login(context, pg, headless=headless)
        for role in candidate["search_roles"]:
            for card in scrape_role(pg, role, candidate.get("exclusion_keywords"), max_scrolls, pause):
                stats["cards"] += 1
                status, reason = matches_strict_filter(card["text"], candidate)
                if status is None:
                    stats["dropped"][reason] = stats["dropped"].get(reason, 0) + 1
                    continue
                email = card["emails"][0]
                if (email, card["post_url"]) in existing:
                    stats["duplicates"] += 1
                    continue
                existing.add((email, card["post_url"]))
                new_rows.append({
                    "recruiter_name": recruiter_name_from_profile(card["profile_url"], card["actor_name"]),
                    "recruiter_email": email,
                    "role": role,
                    "post_url": card["post_url"],
                    "job_description": card["text"],
                    "email_sent_status": status,
                    "timestamp": csv_store.now(),
                })
                stats["mismatched_role" if status == "mismatched_role" else "saved"] += 1

    if new_rows:
        csv_store.append_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS, new_rows)
    log.info("Scrape done for %s: %s", candidate_key, stats)
    return stats


def pending_recruiters(candidate_key: str) -> list[dict]:
    """Recruiter rows for this candidate's roles that this candidate hasn't emailed yet."""
    candidate = client_config.get_candidate(candidate_key)
    roles = {r.lower() for r in candidate["search_roles"]}
    contacted = {r["recruiter_email"].lower()
                 for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
                 if r["candidate"] == candidate["name"] and r["email_sent_status"] == "sent"}
    seen: set[str] = set()
    rows = []
    for row in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS):
        email = row["recruiter_email"].lower()
        if (row["role"].lower() in roles and row["email_sent_status"] != "mismatched_role"
                and email not in contacted and email not in seen):
            seen.add(email)
            rows.append(row)
    return rows
