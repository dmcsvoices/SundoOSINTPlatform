"""Daily email digest sender."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests

from sundo.config import (
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_TO,
    SMTP_USER,
    SQLITE_PATH,
)

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

try:
    import smtplib
except Exception:
    smtplib = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _query_top_articles() -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT title, url, source, published_at FROM rss_articles "
            "ORDER BY published_at DESC LIMIT 5"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("Could not query articles: %s", exc)
        return []


def _query_coordination_events() -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, pattern_type, detected_at, account_count FROM coordination_events "
            "WHERE detected_at >= datetime('now', '-1 day') ORDER BY detected_at DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("Could not query coordination events: %s", exc)
        return []


def _query_ftc_ready() -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT handle, amount, nature FROM ftc_violations WHERE status = 'candidate'"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("Could not query FTC candidates: %s", exc)
        return []


def _query_new_fara() -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM fara_filings WHERE filed_at >= datetime('now', '-1 day')"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("Could not query FARA filings: %s", exc)
        return []


def _build_text(articles: list[dict[str, Any]], events: list[dict[str, Any]], ftc: list[dict[str, Any]], fara: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        "Sundo Pi Daily Digest",
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "",
        "== Top Articles ==",
        "",
    ]
    for a in articles:
        lines.append(f"- {a.get('title', 'Untitled')} ({a.get('source', 'unknown')})")
        lines.append(f"  {a.get('url', '')}")
    if not articles:
        lines.append("No articles today.")
    lines.extend(["", "== Coordination Events ==", ""])
    for e in events:
        lines.append(f"- {e.get('pattern_type', 'unknown')}: {e.get('account_count', 0)} accounts ({e.get('detected_at', 'n/a')})")
    if not events:
        lines.append("No events in the last 24h.")
    lines.extend(["", "== FTC Packages Ready ==", ""])
    for v in ftc:
        lines.append(f"- {v.get('handle', 'unknown')}: ${v.get('amount', 0):,.2f} — {v.get('nature', 'n/a')}")
    if not ftc:
        lines.append("None ready.")
    lines.extend(["", "== New FARA Filings ==", ""])
    for f in fara:
        lines.append(f"- {f.get('registrant_name', 'unknown')} ({f.get('foreign_principal_country', 'n/a')})")
    if not fara:
        lines.append("None today.")
    lines.append("")
    lines.append("_No tracking pixels. Plain text + HTML multipart._")
    lines.append("")
    return "\n".join(lines)


def _build_html(articles: list[dict[str, Any]], events: list[dict[str, Any]], ftc: list[dict[str, Any]], fara: list[dict[str, Any]]) -> str:
    def _li(text: str) -> str:
        return f"<li>{text}</li>"
    def _ul(items: list[str]) -> str:
        if not items:
            return "<p><em>None.</em></p>"
        return "<ul>\n" + "\n".join(items) + "\n</ul>"
    art_items = [_li(f"<a href='{a.get('url','')}'>{a.get('title','Untitled')}</a> ({a.get('source','unknown')})") for a in articles]
    evt_items = [_li(f"{e.get('pattern_type','unknown')}: {e.get('account_count',0)} accounts — {e.get('detected_at','n/a')}") for e in events]
    ftc_items = [_li(f"{v.get('handle','unknown')}: ${v.get('amount',0):,.2f} — {v.get('nature','n/a')}") for v in ftc]
    fara_items = [_li(f"{f.get('registrant_name','unknown')} ({f.get('foreign_principal_country','n/a')})") for f in fara]
    return (
        "<html><body>\n"
        f"<h1>Sundo Pi Daily Digest</h1>\n"
        f"<p>{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}</p>\n"
        "<h2>Top Articles</h2>\n" + _ul(art_items) + "\n"
        "<h2>Coordination Events</h2>\n" + _ul(evt_items) + "\n"
        "<h2>FTC Packages Ready</h2>\n" + _ul(ftc_items) + "\n"
        "<h2>New FARA Filings</h2>\n" + _ul(fara_items) + "\n"
        "<hr><p><em>No tracking pixels. Plain text + HTML multipart.</em></p>\n"
        "</body></html>"
    )


def send_digest() -> bool:
    """Build and send the daily digest email via SMTP."""
    if smtplib is None:
        logger.warning("smtplib not available; cannot send digest.")
        return False
    articles = _query_top_articles()
    events = _query_coordination_events()
    ftc = _query_ftc_ready()
    fara = _query_new_fara()
    text_body = _build_text(articles, events, ftc, fara)
    html_body = _build_html(articles, events, ftc, fara)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Sundo Pi Daily Digest — {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}"
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        if SMTP_USER and SMTP_PASSWORD:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, [SMTP_TO], msg.as_string())
        server.quit()
        logger.info("Digest sent to %s", SMTP_TO)
        return True
    except Exception as exc:
        logger.warning("Failed to send digest: %s", exc)
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    send_digest()
