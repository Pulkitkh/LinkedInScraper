# Recruitment and Outreach Automation System

This is a command-line pipeline that automates cold outreach to US recruiters on LinkedIn for **Corp-to-Corp (C2C)** contract roles. It runs in **Client Application Mode**, sending outreach on behalf of registered client candidates.

It is built to match the *Technical Specifications & System Architecture Blueprint* (`C2C_project_documentation.pdf`).

| # | Goal | Where |
|---|---|---|
| 1 | **Targeted scraping:** finds hiring posts for each candidate's roles from the last 24 hours, US only, and pulls out recruiter emails. | `linkedin_scraper.py` |
| 2 | **Strict filtering:** drops bench-marketing posts and non-USA posts, and marks role mismatches (QA, DevOps, Salesforce…). | `linkedin_scraper.py` |
| 3 | **AI resume customization:** Gemini, with Groq and OpenRouter as fallbacks, rewrites the summary, project bullets and skills. The original styling is kept and the result is exported to PDF. | `resume_customizer.py`, `llm_client.py` |
| 4 | **Cold outreach:** sends through Gmail SMTP. Each email opens with a hook built from keywords in the job description, and CCs the candidate and team lead and BCCs the admin. | `gmail_sender.py` |
| 5 | **Threaded follow-ups:** after 7 days without a reply, sends a follow-up in the same Gmail thread (`In-Reply-To` / `References`) with an AI-written value proposition. | `followup_sender.py` |
| 6 | **IMAP reply sync:** scans the inbox and marks recruiters who replied with `replied=1`. | `imap_reply_detector.py` |
| 7 | **Analytics:** prints stats and exports a dark-themed `analytics_report.html` dashboard. | `analytics.py` |

## Architecture

```
main.py (CLI) ─► linkedin_login.py ─► session.json
              ─► linkedin_scraper.py ─► recruiters.csv          (USA geo search, last 24h, strict filters)
              ─► gmail_sender.py ──► resume_customizer.py ─► Gemini → Groq → OpenRouter
                       │                    └► DOCX → PDF (MS Word COM via docx2pdf + lock_util.py, or LibreOffice)
                       └► smtp.gmail.com:587 STARTTLS (fallback 465 SSL) ─► email_sent_history.csv
              ─► followup_sender.py (threaded Re: emails)   ─► imap_reply_detector.py (imap.gmail.com:993)
              ─► analytics.py ─► analytics_report.html
```

| File | Purpose |
|---|---|
| `main.py` | Interactive menu, setup checks, live connection checks, pipeline orchestration, batch mode. |
| `client_config.py` | Candidate profiles (`CLIENTS`), target roles and exclusions (`CANDIDATE_ROLES`), bench phrases, role domains. |
| `linkedin_login.py` | Logs in with a visible browser and saves the session to `session.json`. |
| `linkedin_scraper.py` | Playwright search, scroll, "See more" clicks, three-dots **Copy link** interceptor, email extraction, `is_usa_job`, `matches_strict_filter`. |
| `gmail_sender.py` | 60+ `JD_KEYWORDS` hooks, SMTP, de-duplication, MIME attachments, `customize_resume()` dispatcher. |
| `resume_customizer.py` | PDF→DOCX (pdf2docx), section extraction, LLM tailoring, run-level rewrite that keeps styling, DOCX→PDF. |
| `example_client_customizer.py` | Template for client-specific customizers (like `safi_customizer.py`). |
| `followup_sender.py` | Threaded follow-ups. |
| `imap_reply_detector.py` | Reply detection. |
| `analytics.py` | Statistics and the HTML dashboard. |
| `lock_util.py` | File-based lock so Word COM conversions never run at the same time. |
| `llm_client.py` | Gemini / Groq / OpenRouter fallback chain. |
| `csv_store.py` | CSV databases, with header migration at runtime. |
| `recruiters.csv`, `email_sent_history.csv` | Databases. The schemas are exactly as in spec section 4. |
| `clients/` | Candidate resumes. A sample resume is included. |
| `scratch/` | Temporary conversions and tailored resumes. |

## Setup

Requirements: Python 3.10+, and Windows with Microsoft Word for the spec's Word COM PDF conversion. On macOS with Word, docx2pdf works. On Linux, the system uses LibreOffice instead (`soffice`). If neither is present, the tailored DOCX is attached.

```bash
pip install -r requirements.txt
playwright install chromium
copy .env.example .env        # Windows   (cp on macOS/Linux) - then fill it in
```

`.env` needs:

- `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`
- `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD`. Use a [Gmail App Password](https://myaccount.google.com/apppasswords), which needs 2-Step Verification turned on. IMAP must be enabled in Gmail settings.
- `GEMINI_API_KEY`, plus optionally `GROQ_API_KEY` and `OPENROUTER_API_KEY`, each with its `*_MODEL`.
- Optionally `TEAM_LEAD_EMAIL` (CC) and `ADMIN_EMAIL` (BCC).

Then register your candidates in `client_config.py` (spec section 8.1) and put their resumes in `clients/`.

## Presentation / first-run checklist

1. `python main.py --check` makes **real** connections to Gmail SMTP and IMAP, each AI provider, and LinkedIn, and checks every candidate's resume. Every row should say PASS. You need at least one AI provider.
2. `python main.py` → **option 2** opens a visible browser and logs in to LinkedIn. Solve any CAPTCHA or 2FA prompt there. `session.json` is saved, and later runs are headless.
3. **Option 3** scrapes for one candidate. Open `recruiters.csv` to show the extracted recruiters, emails, post links and JDs.
4. **Option 4** lists that candidate's pending recruiters.
5. **Option 5** in **test** mode sends each tailored email and resume to *your own* Gmail (subject `[TEST -> recruiter@…]`). Use this to show the personalization safely.
6. **Option 5** in **live** mode: type `SEND` to email the recruiters for real. They are logged to `email_sent_history.csv`.
7. **Option 7** checks replies. **Option 9** opens `analytics_report.html`.
8. **Option 8** is the automated batch mode for every candidate. It asks for test/live mode and a global daily limit.

For unattended or scheduled runs (for example from Windows Task Scheduler):

```bash
python main.py --batch --mode live --limit 40 --yes   # scrape + tailor + send for all candidates
python main.py --check-replies --followups --mode live --yes
```

## Search and filter rules

- **Query** (spec 5.1). The exclusions are configurable per candidate:
  ```
  "<Role>" C2C -hotlist -fulltime -"available consultants" -Bench
  ```
- **Search URL:** past 24 hours, sorted by date, `geoUrn=["103644278"]` (United States). The page is scrolled 6 times and every "See more" button is clicked.
- **Post links:** taken from the post's `data-urn`. If a card has none, the three-dots menu → **Copy link** → clipboard is used, and the tracking parameters are stripped.
- **Emails:** `mailto:` links are checked first, then the regex `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` runs on the post text. Obfuscated forms like `name [at] domain [dot] com` are handled too.
- **`is_usa_job`:** rejects any post that mentions a `NON_USA_INDICATORS` entry (India, Pune, Noida, Canada…). Otherwise it needs a US city, a `City, ST` pattern, or an explicit US term (USA, USC, GC, H1B, EAD, US time zones…).
- **Bench exclusion:** drops "on my bench", "candidate available", "marketing my consultant", hotlists and similar.
- **Domain mismatch:** the declared job title is parsed. A post is stored as `mismatched_role` and never emailed when either:
  - the title targets a domain outside the candidate's `allowed_domains`, or
  - the title contains a role that defines the job on its own (QA, DevOps, Salesforce, SAP, BA, PM, data), even if it also mentions Java.

## Tests

```bash
pytest
```

There are 57 tests, and none of them need network access:

- **Browser end-to-end tests** run against a fake LinkedIn: login, wrong password, CAPTCHA checkpoint, scraping with every filter case, and the Copy-link clipboard path.
- **A full batch-mode run** goes through login, scrape, filter, real DOCX→PDF resume conversion, send, CSV logging and analytics.
- **Resume tailoring tests** check that font, size, italics, colour and bold labels are preserved, and that the text really appears in the generated PDF.
- **A real SMTP exchange** against a local mail server checks that BCC goes only in the envelope.
- **Other tests** cover the SMTP 587→465 fallback, threaded follow-up headers, IMAP batching and reply marking, the AI provider fallback chain, CSV header migration, and the lock file.

## Notes

- LinkedIn changes its page structure often. All selectors are in `SELECTORS` in `linkedin_scraper.py` and have fallbacks. Update them there if scraping stops finding posts.
- Automating LinkedIn breaks its User Agreement and can get the account restricted. Keep volumes modest and use your daily limit.
- Gmail limits personal accounts to about 500 recipients per day. CC and BCC addresses count toward that.
