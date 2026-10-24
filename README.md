# Recruitment and Outreach Automation System

This is a command-line pipeline that automates cold outreach to US recruiters on LinkedIn for **Corp-to-Corp (C2C)** contract roles. It runs in **Client Application Mode**: it finds recruiters who are hiring, tailors each client candidate's resume to the job, and emails the recruiter on the candidate's behalf. It then follows up, tracks replies and reports results.

It is built to match the *Technical Specifications & System Architecture Blueprint* (`C2C_project_documentation.pdf`).

---

## 1. How to start (first time, about 10 minutes)

**Requirements:**

- Python 3.10+
- Google Chrome/Chromium, installed by Playwright in the steps below
- For tailored resumes as PDF: **Microsoft Word** on Windows or macOS. On Linux, LibreOffice works instead.

```bash
git clone https://github.com/Pulkitkh/intern.git
cd intern
python -m venv .venv
.venv\Scripts\activate                 # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
copy .env.example .env                 # Windows   (macOS/Linux: cp .env.example .env)
```

Open `.env` and fill in these values:

| Variable | What to put |
|---|---|
| `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` | The LinkedIn account used for searching. |
| `GMAIL_EMAIL` | The Gmail address that sends the emails. |
| `GMAIL_APP_PASSWORD` | A 16-character **App Password**, not your normal password. Create it at Google Account → Security → 2-Step Verification → App passwords. Also turn on IMAP: Gmail → Settings → Forwarding and POP/IMAP. |
| `GEMINI_API_KEY` | Free key from https://aistudio.google.com/apikey. |
| `BROWSER_CHANNEL` | Optional. `msedge` or `chrome` uses your installed browser, which LinkedIn trusts more. |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY` | Optional fallbacks if Gemini fails. |
| `TEAM_LEAD_EMAIL`, `ADMIN_EMAIL` | Optional. Copied (CC) and blind-copied (BCC) on every outreach email. |

Then add your candidates in `client_config.py` (spec section 8.1), and put each resume in `clients/`. A `.docx` resume works best. A sample candidate and resume are included, so everything works out of the box.

**Verify everything with one command.** It makes real connections to every service:

```bash
python main.py --check
```

Every row should say **PASS**: Gmail SMTP, Gmail IMAP, at least one AI provider, the LinkedIn session, and each candidate's resume. The first time, "LinkedIn session" fails until you log in with menu option 2, described below.

---

## 2. Demo workflow (menu: `python main.py`)

| Step | Menu | What happens | What to show |
|---|---|---|---|
| 1 | **1** Validate setup | Checks `.env`, resumes, the PDF converter and the CSV files, then optionally runs the live connection checks. | All green |
| 2 | **2** LinkedIn login | Opens a **visible** browser, auto-fills your credentials, and lets you solve any CAPTCHA or 2FA. Saves `session.json`, so later runs are headless. | Browser logs in |
| 3 | **3** Scrape | Searches LinkedIn for each of the candidate's roles (last 24h, USA, C2C), extracts recruiter emails, filters the posts, and saves them to `recruiters.csv`. | Open `recruiters.csv` |
| 4 | **4** Pending | Lists the recruiters this candidate hasn't contacted yet. | Table |
| 5 | **5** Outreach, **test** mode | Builds every personalized email and tailored resume, but sends them all to **your own Gmail** (subject `[TEST -> recruiter@…]`). | Open your inbox: hook sentence plus tailored PDF |
| 6 | **5** Outreach, **live** mode | Same as test mode, but sent to the real recruiters. You must type `SEND` to confirm. Each send is logged in `email_sent_history.csv`. | History CSV |
| 7 | **6** Follow-ups | Recruiters with no reply after 7 days get a threaded "Re:" follow-up. | Gmail shows one thread |
| 8 | **7** Check replies | Scans the inbox and sets `replied=1` for recruiters who answered. | History CSV |
| 9 | **9** Analytics | Prints stats and writes `analytics_report.html`, a dark-themed dashboard. | Open the HTML |
| 10 | **8** Batch mode | Does steps 3→6 for **every** candidate, using one browser session and a global daily limit. | Console summary |

Commands you can schedule (for example with Windows Task Scheduler):

```bash
python main.py --batch --mode live --limit 40 --yes      # scrape + tailor + send for all candidates
python main.py --check-replies --followups --mode live --yes
```

> **Demo tip:** do steps 1–2 before the meeting, so `session.json` exists and no CAPTCHA appears live. Use **test** mode for anything you show on screen, and keep `recruiters.csv` closed in Excel while the tool is running.

---

## 3. How it works

```
main.py ─► linkedin_login.py ─────► session.json
        ─► linkedin_scraper.py ───► recruiters.csv          (search → scroll → extract → filter)
        ─► gmail_sender.py ─┬─────► resume_customizer.py ─► llm_client.py (Gemini → Groq → OpenRouter)
        │                   │                └► DOCX → PDF (Word COM + lock_util.py, or LibreOffice)
        │                   └─────► smtp.gmail.com:587 STARTTLS (465 SSL fallback) ─► email_sent_history.csv
        ─► followup_sender.py ────► threaded "Re:" emails (In-Reply-To / References)
        ─► imap_reply_detector.py ► imap.gmail.com:993 → replied=1
        ─► analytics.py ──────────► analytics_report.html
```

### LinkedIn login (`linkedin_login.py`)

1. Loads `session.json` if it exists.
2. If the session has expired, it opens the sign-in page and fills it in from `.env`. It recognises every variant LinkedIn serves:
   - the classic `/login` form
   - the homepage form
   - a two-step email-then-password page
   - the "Welcome back" account picker
3. **Visible browser (option 2):** if the form can't be filled automatically, or LinkedIn shows a CAPTCHA, 2FA prompt or "wrong password" message, the window stays open for up to 5 minutes. Sign in by hand, and the session is saved as soon as the feed loads.
4. **Headless runs:** the run stops with a clear message telling you to use option 2. An unrecognised page is saved as `scratch/login_debug.png` and `.html`.
5. A successful login is recognised by LinkedIn's `li_at` auth cookie (or the global navigation bar).

### Scraping and filtering (`linkedin_scraper.py`)

1. **Query** per role: `"Java Developer" C2C -hotlist -fulltime -"available consultants" -Bench`. The exclusions can be set per candidate.
2. **Search URL:** posts from the past 24 hours, sorted by date, `geoUrn=["103644278"]` (United States).
3. **Scrolling:** 6 scrolls, clicking only *collapsed* "…more" buttons so the full post text loads.
4. **Post link:** taken from the post's `data-urn`. If a post has none, the scraper opens the three-dots menu, clicks **Copy link to post**, reads the clipboard, and strips the tracking parameters.
5. **Email:** `mailto:` links first, then the regex `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`. Obfuscated addresses like `name [at] domain [dot] com` are handled too. Posts without an email are skipped.
6. **Filters**, applied in this order:
   - **Bench marketing** ("on my bench", "hotlist", "marketing my consultant"…): dropped.
   - **Non-USA** (India, Pune, Canada, London…): dropped. The post must also mention a US city, a `City, ST` pattern, or a US term such as USA, GC, H1B or EST.
   - **Role mismatch:** the job title targets a domain the candidate doesn't do, such as a QA or DevOps title for a Java developer. The post is saved as `mismatched_role` and never emailed.
   - Everything else is saved as `pending`.
7. If LinkedIn changes its post layout, the scraper falls back to plain result list items.

### Resume tailoring (`resume_customizer.py`)

1. A PDF resume is first converted to DOCX with pdf2docx.
2. The **Summary**, **Experience/Projects** bullets and **Technical Skills** sections are found, including skills inside tables. Company, client and date lines are left untouched.
3. These sections are sent with the job description to Gemini, falling back to Groq and then OpenRouter. The AI returns `rewritten_summary`, `rewritten_paragraphs` and `rewritten_skills` as JSON.
4. The new text is written back run-by-run, keeping fonts, sizes, bold/italic, colours and labels like `Languages:`. Any rewrite with an unknown ID, empty text or an unreasonable length is ignored.
5. The DOCX is converted to PDF: MS Word on Windows/macOS, with a mutex lock and a retry after killing a hung Word process, or LibreOffice otherwise.
6. **If anything fails** (no AI key, AI error, conversion crash), the original resume is attached and the run continues.

### Sending (`gmail_sender.py`)

1. The 3 most frequent of 60+ technology keywords in the job description form the hook, e.g. *"My hands-on experience with Java, Spring Boot, and Kafka maps directly to what you are looking for."*
2. The subject and body come from `templates/outreach_email.txt`. Replace that file with your team's template if you have one.
3. The attachment is named `Candidate_Name_Resume_<timestamp>.pdf`, ASCII-only. Each email CCs the candidate and team lead, BCCs the admin, and waits 3 seconds before the next one.
4. **Safety rules:**
   - A recruiter is never emailed twice for the same candidate.
   - A wrong app password or an unreachable Gmail server stops the run with a clear message, and nobody is marked failed.
   - A dropped connection reconnects and retries once.
   - Hitting Gmail's daily quota stops the run.
   - A rejected address is logged as `failed` and the run continues.

### Follow-ups, replies, analytics

- `followup_sender.py` picks emails that were sent, never followed up, unanswered, and at least 7 days old. Each follow-up is a "Re:" email in the same Gmail thread with a 1–2 sentence AI value proposition, and the resume is attached again.
- `imap_reply_detector.py` searches the inbox (read-only) since the oldest unanswered email, reads the sender headers in batches of 100, and marks matching recruiters `replied=1`.
- `analytics.py` reports: recruiters scraped, emails sent, follow-ups, replies, reply rate, failures, daily sends over the last 14 days, top roles, and a breakdown per candidate.

### Troubleshooting

| Symptom | Fix |
|---|---|
| Option 2 shows a page the tool can't fill | Sign in by hand in the open window; the session is saved automatically. |
| LinkedIn keeps showing a CAPTCHA or "unusual activity" | Set `BROWSER_CHANNEL=msedge` (Windows) or `BROWSER_CHANNEL=chrome` in `.env` to use your installed browser, then run option 2 again. |
| Headless run says "Could not find the LinkedIn sign-in form" | Run option 2 once. If it keeps happening, send `scratch/login_debug.png`. |
| "Gmail rejected the login" | `GMAIL_APP_PASSWORD` must be a 16-character App Password, with 2-Step Verification on. |
| "recruiters.csv is locked" | Close the CSV in Excel and run again. Nothing was lost. |
| Scraping finds 0 posts | Normal if there are no matching posts in the last 24h. Otherwise LinkedIn changed its markup: update `SELECTORS` in `linkedin_scraper.py`. |
| You want the full error details | Run `python main.py -v`. |

### Data files (spec section 4)

- `recruiters.csv` columns: `recruiter_name, recruiter_email, role, post_url, job_description, email_sent_status (pending/sent/failed/mismatched_role), timestamp`.
- `email_sent_history.csv` columns: `recruiter_name, recruiter_email, role, post_url, candidate, email_sent_status, timestamp, subject, message_id, followup_count, last_followup_at, replied`.

Missing columns are added automatically. Files edited and re-saved in Excel still load correctly. If a file is open in Excel while the tool writes to it, you get a "close it in Excel" message and no data is lost.

### Files

| File | Purpose |
|---|---|
| `main.py` | Menu, `--check`, batch mode and orchestration. |
| `client_config.py` | Candidates, roles, exclusions, bench phrases, role domains. |
| `linkedin_login.py`, `linkedin_scraper.py` | LinkedIn login, search, extraction and filters. |
| `gmail_sender.py`, `followup_sender.py`, `imap_reply_detector.py` | Email sending, follow-ups and reply detection. |
| `resume_customizer.py`, `llm_client.py`, `lock_util.py` | Resume tailoring, the AI provider chain and the Word lock. |
| `analytics.py`, `csv_store.py`, `settings.py` | Dashboard, CSV databases, and `.env`/paths. |
| `templates/` | Outreach and follow-up email text. |
| `clients/` | Resumes (git-ignored, except the sample). |
| `scratch/` | Temporary files. |

### Adding a client-specific resume parser (spec section 8.2)

1. Write `<client>_customizer.py` with a `customize(candidate, resume_path, job_description, role)` function. It can call `resume_customizer.customize(..., parser=my_parser)` to reuse the standard pipeline with its own section parsing.
2. Register it in `gmail_sender.CUSTOMIZERS`.
3. Set `CLIENTS[key]["customizer"]` to its name.

---

## 4. Tests

```bash
pip install -r requirements.txt
pytest
```

There are 118 tests, and none of them need network access:

- **Demo walkthrough:** drives the real menu through options 2→3→4→5 (test)→5 (live)→6→7→8→9→1. Only LinkedIn, Gmail and the inbox are simulated.
- **Browser tests** against a mock LinkedIn page:
  - login, including LinkedIn's `login-submit` redirect
  - every sign-in page variant (classic, homepage, two-step, "Welcome back"), manual sign-in fallback, page changing while typing, closed browser window, corrupt `session.json`
  - a post disappearing while it's being read
  - wrong password, CAPTCHA checkpoint, session reuse, expired session
  - "…more" expansion and "Copy link to post"
  - all filter cases, and the fallback for a new page layout
- **Failure-mode tests:**
  - wrong Gmail password, Gmail unreachable on both ports, dropped connection, Gmail quota, rejected address
  - resume crash fallback, corrupt resume
  - CSV files saved by Excel (UTF-8 with BOM, and ANSI), a CSV locked by Excel
  - Windows Word retry, LibreOffice timeout, English IMAP dates on any locale
- **15 realistic LinkedIn C2C posts** (requirements, hotlists, India/Canada/UK posts, QA/DevOps/Salesforce titles), each with its expected outcome.
- **Real document and mail tests:**
  - real DOCX→PDF conversion, checking that the tailored text is in the PDF
  - a real SMTP exchange with a local mail server, checking that BCC is only in the envelope
  - threaded follow-up headers, IMAP batching, the AI fallback chain, CSV header migration

## Notes

- LinkedIn changes its page structure often. All selectors are in `SELECTORS` at the top of `linkedin_scraper.py`.
- Automating LinkedIn breaks its User Agreement and can get the account restricted. Keep volumes modest and use the daily limit.
- Gmail allows about 500 recipients per day on personal accounts. CC and BCC addresses count toward that.
- On Windows, a failed Word conversion closes all open Word windows (`taskkill`) before retrying, so save your Word documents before running.
