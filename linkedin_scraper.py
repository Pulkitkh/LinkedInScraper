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
CARD_WAIT_MS = 15000  # how long to wait for search results to render

# LinkedIn markup changes often; each field has fallbacks (first match wins).
SELECTORS = {
    "control_menu": "button[aria-label*='control menu for post' i], button.feed-shared-control-menu__trigger, "
                    "button[aria-label*='more actions' i]",
    # LinkedIn labels it "Copy link to post"; scoped to menus so a post mentioning "copy link" never matches.
    "copy_link": "[role='menu'] [role='menuitem']:has-text('Copy link'), "
                 "[role='menu'] [role='button']:has-text('Copy link'), "
                 ".feed-shared-control-menu__item:has-text('Copy link'), "
                 ".artdeco-dropdown__content-inner li:has-text('Copy link')",
    "show_more_results": "button.scaffold-finite-scroll__load-button, button:has-text('Show more results')",
}

# Clicks only collapsed "…more" toggles (the same button turns into "…less" once expanded).
EXPAND_JS = r"""
() => {
  let clicked = 0;
  for (const b of document.querySelectorAll('button, [role="button"]')) {
    const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).toLowerCase().trim();
    const isToggle = b.classList.contains('feed-shared-inline-show-more-text__see-more-less-toggle')
      || b.getAttribute('data-testid') === 'expandable-text-button'
      || /^(…|\.\.\.)\s*more$|see more|show more(?! results)/.test(label);
    if (isToggle && !/less/.test(label) && b.getAttribute('aria-expanded') !== 'true') {
      b.click();
      clicked++;
    }
  }
  return clicked;
}
"""

# Layout-independent post extraction. LinkedIn's class names change constantly (and are randomised in its
# newer UI), so instead of matching card classes we start from every email address / mailto link on the
# page and walk up to the element that represents one post in the results list.
POSTS_JS = r"""
() => {
  const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}/;
  const OBFUSCATED = /[A-Za-z0-9._%+-]+\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*[A-Za-z0-9-]+/i;
  const URN = /urn:li:(?:activity|share|ugcPost):\d{10,}/;
  const root = document.querySelector('main') || document.body;
  const PROFILE = 'a[href*="/in/"], a[href*="/company/"]';
  const hasProfile = el => !!el.querySelector(PROFILE);
  const isMarked = el => el.matches('[data-urn^="urn:li:"], [data-id^="urn:li:"], [data-view-name="feed-full-update"]');

  // the list-item level: its parent holds several children that each have a profile link
  const isListItem = el => {
    const parent = el.parentElement;
    if (!parent || !hasProfile(el)) return false;
    let siblingsWithProfiles = 0;
    for (const child of parent.children) {
      if (hasProfile(child) && (child.innerText || '').length > 40) siblingsWithProfiles++;
      if (siblingsWithProfiles >= 2) return true;
    }
    return false;
  };
  const isPostLike = el => hasProfile(el) &&
    el.matches('li, article, [role="listitem"], [role="article"], [data-view-name*="update"], [componentkey]');

  const seeds = new Set();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue || '';
    if (t.length > 5 && (EMAIL.test(t) || OBFUSCATED.test(t)) && n.parentElement) seeds.add(n.parentElement);
  }
  root.querySelectorAll('a[href^="mailto:"]').forEach(a => seeds.add(a));

  const containers = [];
  for (const seed of seeds) {
    let found = null, fallback = null;
    for (let el = seed, depth = 0; el && el !== root && depth < 30; el = el.parentElement, depth++) {
      if (isMarked(el) || isListItem(el)) { found = el; break; }
      if (!fallback && isPostLike(el)) fallback = el;
    }
    found = found || fallback;
    if (found && !containers.includes(found)) containers.push(found);
  }
  // keep only outermost posts (a reshared post nested inside another post counts once)
  const posts = containers.filter(c => !containers.some(o => o !== c && o.contains(c)));

  const txt = el => el ? (el.innerText || '').replace(/ /g, ' ').trim() : '';
  return posts.map((node, idx) => {
    node.setAttribute('data-rb-card', String(idx));
    const textEl = node.querySelector('.update-components-text, [data-testid="expandable-text-box"], ' +
      '.feed-shared-inline-show-more-text, .feed-shared-update-v2__description, .feed-shared-text');
    const actor = [...node.querySelectorAll(PROFILE)].find(a => txt(a)) || node.querySelector(PROFILE);
    const nameEl = actor && (actor.querySelector('span[aria-hidden="true"]') || actor);
    const marked = node.closest('[data-urn^="urn:li:"]');
    const urnMatch = (marked && marked.getAttribute('data-urn').match(URN)) || node.outerHTML.match(URN);
    const link = node.querySelector('a[href*="/feed/update/urn:li:"], a[href*="/posts/"]');
    const fullText = txt(node);
    const bodyText = txt(textEl);
    return {
      idx,
      urn: urnMatch ? urnMatch[0] : '',
      link: link ? link.href : '',
      // the full card text is used if the post body element isn't recognised (or misses the email)
      text: bodyText && EMAIL.test(bodyText) ? bodyText : fullText,
      actor_name: txt(nameEl).split('\n')[0],
      profile_url: actor ? actor.href : '',
      mailtos: [...node.querySelectorAll('a[href^="mailto:"]')].map(a => a.getAttribute('href')),
    };
  });
}
"""

NO_RESULTS_JS = r"""
() => /no results found|try (different|other) (keywords|search)|no matching results/i
        .test((document.querySelector('main') || document.body).innerText || '')
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


_TITLE_PREFIX_RE = re.compile(
    r"^(?:(?:urgent(?:ly)?|immediate(?:ly)?|now|actively|we(?:'re| are)?)\s+)*"
    r"(?:hiring|requirement|opening|looking for|need(?:ed)?|job\s*title|title|role|position)"
    r"(?:\s+for)?(?:\s+an?)?\s*[:\-–—!]*\s+", re.I)
_TITLE_TAIL_RE = re.compile(r"\s(?:c2c|w-?2|1099|corp[\s-]to[\s-]corp|remote|onsite|on-site|hybrid)\b", re.I)


def clean_job_title(raw: str) -> str:
    """'🚨 **HIRING: JAVA DEVELOPER | 5 OPEN POSITIONS** 🚨' -> 'Java Developer'. Empty if it isn't a title."""
    t = re.sub(r"(?<![\w])#\w+", " ", raw)                      # hashtags (keeps C#)
    t = "".join(ch if ch.isalnum() or ch.isspace() or ch in ".,/&+()-–—|:'#" else " " for ch in t)
    t = " ".join(t.split())
    t = re.sub(r"^\d+\s*[.)]\s*", "", t)                        # "3." / "1)" numbering
    for _ in range(2):
        t = _TITLE_PREFIX_RE.sub("", t).strip()
    t = re.split(r"\s*[|–—]\s*|\s+-\s+|\s\(|,|:", t)[0]      # drop " | Onsite", " – Charlotte, NC", ...
    tail = _TITLE_TAIL_RE.search(t)
    if tail and _TITLE_WORDS_RE.search(t[:tail.start()]):
        t = t[:tail.start()]
    t = " ".join(t.split()).strip(" -/&").rstrip(".")  # keep a leading ".NET"
    if t.isupper():
        t = " ".join(w if len(w) <= 3 else w.capitalize() for w in t.split())
    return t[:80] if len(t) >= 4 and _TITLE_WORDS_RE.search(t) else ""


def detect_job_title(text: str) -> str:
    m = _TITLE_LABEL_RE.search(text)
    if m and (title := clean_job_title(m.group(1))):
        return title
    for line in text.splitlines():
        if len(line) <= 160 and (title := clean_job_title(line)):
            return title
    return ""


def domains_in(text: str) -> set[str]:
    return {name for name, pattern in client_config.ROLE_DOMAINS.items() if re.search(pattern, text, re.I)}


_GENERIC_TITLE_RE = re.compile(r"\b(?:software|application|applications|programmer|developer|engineer)\b", re.I)


def role_mismatch(text: str, candidate: dict) -> bool:
    """True when the declared job title targets work the candidate doesn't do (e.g. QA or Oracle OTM for Java)."""
    allowed = set(candidate.get("allowed_domains", []))
    if not allowed:
        return False
    title = detect_job_title(text)
    title_domains = domains_in(title)
    if (title_domains & set(client_config.EXCLUSIVE_DOMAINS)) - allowed:
        return True  # e.g. "QA Tester - Java" is a QA role even though it mentions Java
    if title_domains:
        return not (title_domains & allowed)
    if title and not _GENERIC_TITLE_RE.search(title):
        return True  # a specific title with none of the candidate's skills, e.g. "Solution Architect"
    return not (domains_in(text) & allowed)  # generic or no title: the post body must mention the skills


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
    """'"Java Developer" AND C2C NOT hotlist NOT fulltime NOT "available consultants" NOT Bench'

    LinkedIn's documented boolean operators are AND / OR / NOT; a leading "-" is not reliably supported.
    """
    excluded = exclusions if exclusions is not None else client_config.DEFAULT_EXCLUSION_KEYWORDS
    terms = [f'NOT "{k}"' if " " in k else f"NOT {k}" for k in excluded]
    return " ".join([f'"{role}"', "AND", "C2C", *terms])


def relaxed_search_query(role: str) -> str:
    """Fallback when the strict query returns nothing; bench/hotlist posts are still dropped by the filters."""
    return f'"{role}" C2C'


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


def _collect_posts(page: Page) -> list[dict]:
    for attempt in range(3):
        try:
            return page.evaluate(POSTS_JS)
        except Exception as exc:  # page re-rendering or navigating; try again shortly
            log.debug("Post extraction retry %d: %s", attempt + 1, exc)
            page.wait_for_timeout(1000)
    return []


def _save_search_debug(page: Page, role: str) -> str:
    base = settings.scratch_dir() / ("search_debug_" + re.sub(r"[^A-Za-z0-9]+", "_", role).strip("_"))
    try:
        page.screenshot(path=f"{base}.png", full_page=True)
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        return f"{base}.png"
    except Exception:
        return ""


def _run_search(page: Page, query: str, max_scrolls: int, pause: tuple[float, float]) -> dict[str, dict]:
    page.goto(build_search_url(query), wait_until="domcontentloaded")
    try:
        page.wait_for_selector("main a[href*='/in/'], main a[href*='/company/']", timeout=CARD_WAIT_MS)
    except Exception:
        pass  # no results, or a slow page; the scroll loop below still gives it time

    found: dict[str, dict] = {}

    def harvest():
        for post in _collect_posts(page):
            key = post["urn"] or post["link"] or post["text"][:200]
            found.setdefault(key, post)

    for _ in range(max_scrolls):
        _expand_see_more(page)
        harvest()  # collected every scroll in case LinkedIn virtualises the list
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
    page.wait_for_timeout(300)
    harvest()
    return found


def scrape_role(page: Page, role: str, exclusions: list[str], max_scrolls: int = MAX_SCROLLS,
                pause: tuple[float, float] = (1.5, 3.0)) -> list[dict]:
    """Return raw cards ({post_url, text, actor_name, profile_url, emails}) for one search role."""
    query = build_search_query(role, exclusions)
    log.info("Searching LinkedIn: %s", query)
    found = _run_search(page, query, max_scrolls, pause)
    if not found:
        no_results = page.evaluate(NO_RESULTS_JS)
        query = relaxed_search_query(role)
        log.info("%s; retrying with a simpler query: %s",
                 "LinkedIn found no results" if no_results else "No posts with emails", query)
        found = _run_search(page, query, max_scrolls, pause)

    cards: dict[str, dict] = {}
    for post in found.values():
        emails = extract_emails(post["text"], post["mailtos"])
        if not emails:
            continue
        if post["urn"]:
            post_url = f"{LINKEDIN}/feed/update/{post['urn']}/"
        elif post["link"]:
            post_url = clean_url(post["link"])
        else:
            handle = page.query_selector(f'[data-rb-card="{post["idx"]}"]')
            post_url = copy_post_link(page, handle) if handle else ""
        key = post_url or post["text"][:200]
        cards.setdefault(key, {**post, "post_url": post_url, "emails": emails})

    if not cards:
        shot = _save_search_debug(page, role)
        log.warning("  0 posts with emails for %r%s", role,
                    f" - saved what LinkedIn showed to {shot}" if shot else "")
    else:
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
    """Recruiter rows for this candidate's roles that this candidate hasn't emailed yet.

    The strict filters run again here, so rows saved before a filter improvement are re-checked too;
    rows that now count as a role mismatch are marked mismatched_role in recruiters.csv.
    """
    candidate = client_config.get_candidate(candidate_key)
    roles = {r.lower() for r in candidate["search_roles"]}
    contacted = {r["recruiter_email"].lower()
                 for r in csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
                 if r["candidate"] == candidate["name"] and r["email_sent_status"] == "sent"}
    seen: set[str] = set()
    rows, newly_mismatched = [], set()
    for row in csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS):
        email = row["recruiter_email"].lower()
        if (row["role"].lower() not in roles or row["email_sent_status"] == "mismatched_role"
                or email in contacted or email in seen):
            continue
        status, _ = matches_strict_filter(row["job_description"], candidate)
        if status != "pending":
            if status == "mismatched_role":
                newly_mismatched.add((email, row["post_url"]))
            continue
        seen.add(email)
        rows.append(row)
    if newly_mismatched:
        csv_store.update_rows(
            settings.recruiters_csv(), csv_store.RECRUITER_FIELDS,
            lambda r: (r["recruiter_email"].lower(), r["post_url"]) in newly_mismatched,
            {"email_sent_status": "mismatched_role"})
        log.info("%d saved post(s) re-checked and marked mismatched_role", len(newly_mismatched))
    return rows
