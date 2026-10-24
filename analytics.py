"""Pipeline statistics and a self-contained dark-themed HTML dashboard (analytics_report.html)."""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import csv_store
import settings


def compute_stats(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    recruiters = csv_store.read_rows(settings.recruiters_csv(), csv_store.RECRUITER_FIELDS)
    history = csv_store.read_rows(settings.history_csv(), csv_store.HISTORY_FIELDS)
    sent = [h for h in history if h["email_sent_status"] == "sent"]
    replied = [h for h in sent if str(h["replied"]) == "1"]
    followed = [h for h in sent if int(h["followup_count"] or 0) > 0]

    per_candidate = defaultdict(lambda: {"sent": 0, "failed": 0, "replied": 0, "followups": 0})
    for h in history:
        c = per_candidate[h["candidate"] or "(unknown)"]
        if h["email_sent_status"] == "sent":
            c["sent"] += 1
            c["replied"] += str(h["replied"]) == "1"
            c["followups"] += int(h["followup_count"] or 0)
        elif h["email_sent_status"] == "failed":
            c["failed"] += 1

    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    daily = Counter(t.date() for h in sent if (t := csv_store.parse_time(h["timestamp"])))
    return {
        "generated_at": now.strftime(csv_store.TIME_FORMAT),
        "recruiters_scraped": len(recruiters),
        "recruiter_status": dict(Counter(r["email_sent_status"] for r in recruiters)),
        "emails_sent": len(sent),
        "emails_failed": sum(1 for h in history if h["email_sent_status"] == "failed"),
        "followups_sent": sum(int(h["followup_count"] or 0) for h in sent),
        "followed_up_threads": len(followed),
        "replies": len(replied),
        "reply_rate": round(100 * len(replied) / len(sent), 1) if sent else 0.0,
        "per_candidate": dict(per_candidate),
        "per_role": dict(Counter(r["role"] for r in recruiters).most_common(10)),
        "daily_sends": [(d.strftime("%b %d"), daily.get(d, 0)) for d in days],
    }


def _bars(pairs: list[tuple[str, int]]) -> str:
    peak = max((v for _, v in pairs), default=0) or 1
    return "".join(
        f'<div class="bar"><span class="lbl">{html.escape(str(k))}</span>'
        f'<span class="track"><span class="fill" style="width:{100 * v / peak:.0f}%"></span></span>'
        f'<span class="val">{v}</span></div>'
        for k, v in pairs
    ) or '<p class="muted">No data yet.</p>'


def render_html(stats: dict) -> str:
    cards = [
        ("Recruiters scraped", stats["recruiters_scraped"]), ("Emails sent", stats["emails_sent"]),
        ("Follow-ups sent", stats["followups_sent"]), ("Replies", stats["replies"]),
        ("Reply rate", f'{stats["reply_rate"]}%'), ("Failed sends", stats["emails_failed"]),
    ]
    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{c['sent']}</td><td>{c['followups']}</td>"
        f"<td>{c['replied']}</td><td>{c['failed']}</td></tr>"
        for name, c in sorted(stats["per_candidate"].items())
    ) or '<tr><td colspan="5" class="muted">No outreach yet.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Analytics</title>
<style>
:root {{ --bg:#0f1117; --panel:#181b24; --line:#2a2f3d; --text:#e6e8ee; --muted:#8b93a7; --accent:#5b8def; --good:#3fb68b; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
main {{ max-width:1100px; margin:0 auto; padding:32px 16px; }}
h1 {{ margin:0 0 4px; font-size:26px; }} h2 {{ font-size:16px; margin:0 0 14px; color:var(--muted); font-weight:600; }}
.muted {{ color:var(--muted); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; margin:24px 0; }}
.card, .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; }}
.card .num {{ font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }}
.card .label {{ color:var(--muted); font-size:13px; }}
.cols {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; margin-bottom:14px; }}
.bar {{ display:grid; grid-template-columns:minmax(90px,170px) 1fr 40px; gap:10px; align-items:center; margin:6px 0; font-size:13px; }}
.lbl {{ color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.track {{ background:#232838; border-radius:6px; height:10px; overflow:hidden; }}
.fill {{ display:block; height:100%; background:var(--accent); border-radius:6px; }}
.val {{ text-align:right; font-variant-numeric:tabular-nums; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-weight:600; }} td:not(:first-child), th:not(:first-child) {{ text-align:right; }}
</style></head>
<body><main>
<h1>Recruitment Outreach Analytics</h1>
<div class="muted">Generated {html.escape(stats["generated_at"])}</div>
<div class="grid">{"".join(f'<div class="card"><div class="num">{v}</div><div class="label">{k}</div></div>' for k, v in cards)}</div>
<div class="cols">
  <div class="panel"><h2>Emails sent, last 14 days</h2>{_bars(stats["daily_sends"])}</div>
  <div class="panel"><h2>Recruiter records by status</h2>{_bars(sorted(stats["recruiter_status"].items()))}</div>
</div>
<div class="cols">
  <div class="panel"><h2>Top roles scraped</h2>{_bars(list(stats["per_role"].items()))}</div>
  <div class="panel"><h2>By candidate</h2><table><thead><tr><th>Candidate</th><th>Sent</th><th>Follow-ups</th>
  <th>Replies</th><th>Failed</th></tr></thead><tbody>{rows}</tbody></table></div>
</div>
</main></body></html>"""


def export_report(path: Path | None = None) -> Path:
    path = path or settings.analytics_report()
    path.write_text(render_html(compute_stats()), encoding="utf-8")
    return path
