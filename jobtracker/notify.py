"""Optional email digest of new matches. Configure with SMTP_* env vars; skipped when unset."""

from __future__ import annotations

import html
import os
import smtplib
from email.message import EmailMessage


def send_email(new_rows: list[dict], today: str) -> bool:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        return False
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    port = int(os.environ.get("SMTP_PORT") or 587)

    rows = sorted(new_rows, key=lambda r: int(r["score"]), reverse=True)
    msg = EmailMessage()
    msg["Subject"] = f"{len(rows)} new Rev Ops leadership role{'s' if len(rows) != 1 else ''} ({today})"
    msg["From"] = os.environ.get("EMAIL_FROM") or user or to
    msg["To"] = to
    msg.set_content("\n\n".join(
        f"[{r['score']}] {r['title']} at {r['company']} ({r['location']})\n{r['url']}\n{r['why']}" for r in rows))
    e = html.escape
    items = "".join(
        f"<li><b>{e(r['score'])}</b> &middot; <a href=\"{e(r['url'])}\">{e(r['title'])}</a> at {e(r['company'])}"
        f" &middot; {e(r['location'])}{' &middot; ' + e(r['salary']) if r['salary'] else ''}"
        f"<br><small>{e(r['why'])}</small></li>" for r in rows)
    msg.add_alternative(f"<ul>{items}</ul>", subtype="html")

    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if user and password:
            s.login(user, password)
        s.send_message(msg)
    print(f"emailed {len(rows)} new matches to {to}")
    return True
