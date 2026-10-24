"""Recruitment & Outreach Automation: interactive console + automated batch runs.

    python main.py                                   # interactive menu
    python main.py --batch --mode test --limit 20    # unattended batch run (option 8)
    python main.py --batch --mode live --limit 40 --yes
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

from rich.console import Console
from rich.logging import RichHandler
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

import analytics
import client_config
import csv_store
import followup_sender
import gmail_sender
import imap_reply_detector
import linkedin_login
import linkedin_scraper
import llm_client
import settings

console = Console()
log = logging.getLogger("main")
VERBOSE = False  # -v shows full tracebacks

MENU = [
    ("1", "Validate setup (.env, candidates, resumes, browser)"),
    ("2", "Setup/Test LinkedIn Login Session"),
    ("3", "Scrape LinkedIn for one candidate"),
    ("4", "View pending recruiters for one candidate"),
    ("5", "Send outreach emails for one candidate"),
    ("6", "Send follow-ups (no reply after 7 days)"),
    ("7", "Check replies (IMAP)"),
    ("8", "Run Full Pipeline for ALL Candidates (Automated Batch Mode)"),
    ("9", "Analytics report"),
    ("0", "Exit"),
]


# ------------------------------------------------------------------ helpers

def validate_setup() -> list[str]:
    problems = []
    for var in ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "GMAIL_EMAIL", "GMAIL_APP_PASSWORD"):
        if not settings.env(var):
            problems.append(f".env: {var} is not set")
    if not llm_client.available_providers(llm_client.CUSTOMIZE_CHAIN):
        problems.append(".env: no GEMINI/GROQ/OPENROUTER API key - resumes will be sent without AI tailoring")
    if not settings.session_file().exists():
        problems.append("session.json missing - run option 2 to log in to LinkedIn once")
    if not (sys.platform in ("win32", "darwin") or shutil.which("soffice") or shutil.which("libreoffice")):
        problems.append("No MS Word/LibreOffice found - tailored resumes will be attached as DOCX")
    for key in client_config.candidate_keys():
        problems += client_config.validate_candidate(key)
    for path, fields in ((settings.recruiters_csv(), csv_store.RECRUITER_FIELDS),
                         (settings.history_csv(), csv_store.HISTORY_FIELDS)):
        if csv_store.migrate(path, fields):
            log.info("Initialised/migrated %s", path.name)
    return problems


def check_connections() -> list[tuple[str, bool, str]]:
    """Live checks against the real services: Gmail SMTP + IMAP login, each AI key, the LinkedIn session."""
    results = []

    def run(name, fn):
        try:
            results.append((name, True, fn() or "OK"))
        except Exception as exc:
            results.append((name, False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"))

    def smtp():
        gmail_sender.smtp_connect(timeout=20).quit()

    def imap():
        conn = imap_reply_detector.connect()
        conn.select("INBOX", readonly=True)
        conn.logout()

    run("Gmail SMTP login", smtp)
    run("Gmail IMAP login", imap)
    for provider in ("gemini", "groq", "openrouter"):
        if settings.env(f"{provider.upper()}_API_KEY"):
            def ping(p=provider):
                reply = llm_client.generate("Answer with one word.", "Reply with the word OK.", [p])
                return f"model {llm_client.current_model(p)} replied: {reply[:30]}"
            run(f"AI: {provider}", ping)
        else:
            results.append((f"AI: {provider}", None, "not configured (optional)"))

    def linkedin():
        with linkedin_login.browser_context(headless=True) as (_, page):
            if not linkedin_login.is_logged_in(page):
                raise RuntimeError("session.json missing/expired - run option 2")

    run("LinkedIn session", linkedin)
    for key in client_config.candidate_keys():
        problems = client_config.validate_candidate(key)
        results.append((f"Candidate {key}", not problems, "; ".join(problems) or "resume found"))
    return results


def show_checks(results) -> bool:
    table = Table(title="Live connection checks")
    for col in ("Check", "Result", "Details"):
        table.add_column(col, overflow="fold")
    for name, ok, detail in results:
        label = "[yellow]SKIP[/]" if ok is None else "[green]PASS[/]" if ok else "[red]FAIL[/]"
        table.add_row(name, label, detail)
    console.print(table)
    ai_ok = any(ok for name, ok, _ in results if name.startswith("AI: "))
    if not ai_ok:
        console.print("[yellow]No AI provider works - resumes will be sent without tailoring.[/]")
    return ai_ok and all(ok for name, ok, _ in results if not name.startswith("AI: "))


def choose_candidate() -> str:
    keys = client_config.candidate_keys()
    for i, key in enumerate(keys, 1):
        c = client_config.get_candidate(key)
        console.print(f"  [cyan]{i}[/] {c['name']} - {c['primary_role']} ({key})")
    idx = IntPrompt.ask("Candidate", choices=[str(i) for i in range(1, len(keys) + 1)], default=1)
    return keys[idx - 1]


def choose_mode() -> str:
    return Prompt.ask("Sending mode ([bold]test[/] = all emails go to your own inbox)",
                      choices=["test", "live"], default="test")


def ask_show_browser() -> bool:
    default = "y" if settings.show_browser() else "n"
    return Prompt.ask("Show the browser window while it searches LinkedIn?", choices=["y", "n"],
                      default=default) == "y"


def confirm_send(mode: str, count: int, assume_yes: bool = False) -> bool:
    if mode == "test" or assume_yes:
        return True
    return Prompt.ask(f"Type [bold red]SEND[/] to email {count} recruiter(s) for real") == "SEND"


def show_pending(rows: list[dict]) -> None:
    table = Table(title=f"{len(rows)} pending recruiter(s)")
    for col in ("Recruiter", "Email", "Role", "Scraped", "Post"):
        table.add_column(col, overflow="fold")
    for r in rows:
        table.add_row(r["recruiter_name"], r["recruiter_email"], r["role"], r["timestamp"], r["post_url"])
    console.print(table)


def show_report(title: str, report) -> None:
    console.print(f"[bold]{title}[/]: sent {report.sent}, failed {report.failed}, skipped {report.skipped}")
    for email, status, note in report.details:
        if status != "sent":
            console.print(f"  [yellow]{status}[/] {email} {note}")
    if report.aborted:
        console.print(f"[bold red]Stopped early:[/] {report.aborted}")


def remaining_today(limit: int) -> int:
    return max(0, limit - gmail_sender.sent_today())


# ------------------------------------------------------------------ actions

def run_outreach(candidate_key: str, mode: str, limit: int, assume_yes: bool = False):
    rows = linkedin_scraper.pending_recruiters(candidate_key)
    show_pending(rows)
    if not rows:
        return None
    if not confirm_send(mode, min(len(rows), limit), assume_yes):
        console.print("Cancelled.")
        return None
    report = gmail_sender.send_outreach(candidate_key, mode=mode, limit=limit, recruiters=rows)
    show_report(f"Outreach for {candidate_key}", report)
    return report


def run_batch(mode: str, daily_limit: int, assume_yes: bool = False, headless: bool = True) -> None:
    """Option 8: scrape every candidate (one LinkedIn session), close the browser, then tailor + send + log."""
    keys = client_config.candidate_keys()
    if not confirm_send(mode, daily_limit, assume_yes):
        console.print("Cancelled.")
        return
    ready = []
    for key in keys:
        problems = client_config.validate_candidate(key)
        if problems:
            console.print(f"[red]Skipping {key}:[/] " + "; ".join(problems))
        else:
            ready.append(key)

    # Phase 1 - LinkedIn. The browser is closed before sending, which can take a while.
    with linkedin_login.browser_context(headless=headless) as (context, page):
        linkedin_login.login(context, page, headless=headless)
        for key in ready:
            console.rule(f"[bold]Scraping: {key}")
            try:
                linkedin_scraper.scrape_candidate(key, page=page)
            except Exception as exc:  # one bad search must not stop the other candidates
                log.error("Scraping failed for %s: %s", key, str(exc).splitlines()[0] if str(exc) else exc)

    # Phase 2 - email.
    for key in ready:
        console.rule(f"[bold]Outreach: {key}")
        budget = remaining_today(daily_limit) if mode == "live" else daily_limit
        if budget <= 0:
            console.print("[yellow]Daily limit reached.[/]")
            break
        rows = linkedin_scraper.pending_recruiters(key)
        console.print(f"{len(rows)} pending recruiter(s); sending up to {budget}")
        if rows:
            report = gmail_sender.send_outreach(key, mode=mode, limit=budget, recruiters=rows)
            show_report(key, report)
            if report.aborted:
                console.print("[red]Stopping the batch - fix the problem above and run again.[/]")
                break
    console.print(f"Analytics: {analytics.export_report()}")


def interactive() -> None:
    while True:
        console.rule("[bold]Recruitment & Outreach Automation")
        for key, label in MENU:
            console.print(f"  [cyan]{key}[/] {label}")
        choice = Prompt.ask("Choose", choices=[k for k, _ in MENU], default="0")
        try:
            if choice == "0":
                return
            if choice == "1":
                problems = validate_setup()
                console.print("[green]Configuration OK.[/]" if not problems else
                              "\n".join(f"[yellow]-[/] {p}" for p in problems))
                if Prompt.ask("Run live connection checks (Gmail, AI keys, LinkedIn)?",
                              choices=["y", "n"], default="y") == "y":
                    show_checks(check_connections())
            elif choice == "2":
                ok = linkedin_login.setup_login_session()
                console.print("[green]Logged in; session.json saved.[/]" if ok else "[red]Login failed.[/]")
            elif choice == "3":
                key = choose_candidate()
                stats = linkedin_scraper.scrape_candidate(key, headless=not ask_show_browser())
                console.print(stats)
            elif choice == "4":
                show_pending(linkedin_scraper.pending_recruiters(choose_candidate()))
            elif choice == "5":
                key, mode = choose_candidate(), choose_mode()
                run_outreach(key, mode, IntPrompt.ask("Max emails", default=20))
            elif choice == "6":
                mode = choose_mode()
                if confirm_send(mode, len(followup_sender.eligible_followups())):
                    show_report("Follow-ups", followup_sender.send_followups(mode=mode))
            elif choice == "7":
                console.print(f"{imap_reply_detector.check_replies()} new replies detected.")
            elif choice == "8":
                mode, limit = choose_mode(), IntPrompt.ask("Global daily email limit", default=40)
                run_batch(mode, limit, headless=not ask_show_browser())
            elif choice == "9":
                console.print(analytics.compute_stats())
                console.print(f"Dashboard: {analytics.export_report()}")
        except Exception as exc:  # keep the menu alive; full trace only with -v
            log.error("Action failed - %s: %s", type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "",
                      exc_info=VERBOSE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="live checks: Gmail SMTP/IMAP, AI keys, LinkedIn")
    parser.add_argument("--batch", action="store_true", help="run option 8 without the menu")
    parser.add_argument("--show-browser", action="store_true",
                        help="run LinkedIn in a visible browser window (also SHOW_BROWSER=true in .env)")
    parser.add_argument("--followups", action="store_true", help="send due follow-ups without the menu")
    parser.add_argument("--check-replies", action="store_true", help="sync replies without the menu")
    parser.add_argument("--mode", choices=["test", "live"], default="test")
    parser.add_argument("--limit", type=int, default=40, help="global daily email limit")
    parser.add_argument("--yes", action="store_true", help="skip the SEND confirmation in live mode")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    global VERBOSE
    VERBOSE = args.verbose

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False)])
    try:
        if args.check:
            for problem in validate_setup():
                console.print(f"[yellow]-[/] {problem}")
            return 0 if show_checks(check_connections()) else 1
        if args.check_replies:
            console.print(f"{imap_reply_detector.check_replies()} new replies detected.")
        if args.followups:
            if confirm_send(args.mode, len(followup_sender.eligible_followups()), args.yes):
                show_report("Follow-ups", followup_sender.send_followups(mode=args.mode, limit=args.limit))
        if args.batch:
            run_batch(args.mode, args.limit, args.yes,
                      headless=not (args.show_browser or settings.show_browser()))
        if not (args.batch or args.followups or args.check_replies):
            interactive()
    except KeyboardInterrupt:
        console.print("\nStopped by user.")
        return 130
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
