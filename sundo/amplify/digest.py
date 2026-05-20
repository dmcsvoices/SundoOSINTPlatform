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


FEED_NAME_MAP = {
    "https://www.wafa.ps/rss.aspx": "Wafa News Agency",
    "https://www.972mag.com/feed/": "+972 Magazine",
    "https://mondoweiss.net/feed/": "Mondoweiss",
    "https://www.middleeasteye.net/rss": "Middle East Eye",
    "https://www.dropsitenews.com/feed": "Drop Site News",
    "https://www.alquds.com/feed/": "Al-Quds",
    "https://electronicintifada.net/rss.xml": "Electronic Intifada",
    "https://www.haaretz.com/srv/haaretz-articles.rss": "Haaretz English",
    "https://theintercept.com/feed/?rss": "The Intercept",
    "https://forward.com/feed/": "The Forward",
    "https://www.jta.org/feed": "Jewish Telegraphic Agency",
}


def _feed_url_to_name(feed_url: str) -> str:
    """Map a feed URL to a human-readable source name."""
    name = FEED_NAME_MAP.get(feed_url)
    if name:
        return name
    norm = feed_url.split("?")[0].rstrip("/")
    for url, mapped_name in FEED_NAME_MAP.items():
        if url.split("?")[0].rstrip("/") == norm:
            return mapped_name
    return feed_url


def get_digest_articles(
    conn: sqlite3.Connection,
    lookback_hours: float = 24.0,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch amplify articles and count monitor articles in the lookback window.

    Returns:
        (amplify_articles, monitor_excluded_count)
    """
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)).isoformat()

    cur = conn.execute(
        """
        SELECT id, title, feed_url, source_type, link, published_at, authors, summary
        FROM rss_articles
        WHERE source_type = 'amplify'
          AND published_at > ?
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (since, limit),
    )
    amplify_articles = [dict(r) for r in cur.fetchall()]

    cur = conn.execute(
        """
        SELECT COUNT(*) FROM rss_articles
        WHERE source_type = 'monitor'
          AND published_at > ?
        """,
        (since,),
    )
    monitor_excluded_count = cur.fetchone()[0]

    return amplify_articles, monitor_excluded_count


def build_digest(conn: sqlite3.Connection, lookback_hours: float = 24.0, limit: int = 10) -> dict[str, Any]:
    """Build the digest payload with inclusion/exclusion counts and logging.

    Returns a dict with keys:
        articles, article_count, monitor_excluded_count, generated_at
    """
    amplify_articles, monitor_excluded_count = get_digest_articles(conn, lookback_hours, limit)
    article_count = len(amplify_articles)

    logger.info("Digest inclusion: %d amplify articles", article_count)
    logger.info("Digest exclusion: %d monitor articles", monitor_excluded_count)

    if monitor_excluded_count > 0:
        # Log per-source exclusion counts for visibility
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)).isoformat()
        cur = conn.execute(
            """
            SELECT feed_url, COUNT(*) as cnt
            FROM rss_articles
            WHERE source_type = 'monitor'
              AND published_at > ?
            GROUP BY feed_url
            """,
            (since,),
        )
        for row in cur.fetchall():
            source_name = _feed_url_to_name(row["feed_url"])
            logger.info("Excluded from digest: %s — %d articles", source_name, row["cnt"])

    if article_count == 0:
        logger.warning("Zero amplify articles found for digest in the last %s hours", lookback_hours)

    return {
        "articles": amplify_articles,
        "article_count": article_count,
        "monitor_excluded_count": monitor_excluded_count,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


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


def _build_text(digest: dict[str, Any], events: list[dict[str, Any]], ftc: list[dict[str, Any]], fara: list[dict[str, Any]]) -> str:
    articles = digest.get("articles", [])
    monitor_excluded = digest.get("monitor_excluded_count", 0)
    lines: list[str] = [
        "Sundo Pi Daily Digest",
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "",
        f"== Palestinian Voice Articles ({len(articles)}) ==",
        "",
    ]
    for a in articles:
        source_name = _feed_url_to_name(a.get("feed_url", ""))
        lines.append(f"- {a.get('title', 'Untitled')} ({source_name})")
        lines.append(f"  {a.get('link', '')}")
    if not articles:
        lines.append("No articles today.")
    if monitor_excluded:
        lines.append(f"\n(Monitor sources excluded: {monitor_excluded} articles)")
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


def _build_html(digest: dict[str, Any], events: list[dict[str, Any]], ftc: list[dict[str, Any]], fara: list[dict[str, Any]]) -> str:
    articles = digest.get("articles", [])
    monitor_excluded = digest.get("monitor_excluded_count", 0)

    def _li(text: str) -> str:
        return f"<li>{text}</li>"

    def _ul(items: list[str]) -> str:
        if not items:
            return "<p><em>None.</em></p>"
        return "<ul>\n" + "\n".join(items) + "\n</ul>"

    art_items = []
    for a in articles:
        source_name = _feed_url_to_name(a.get("feed_url", ""))
        art_items.append(_li(f"<a href='{a.get('link','')}'>{a.get('title','Untitled')}</a> ({source_name})"))
    evt_items = [_li(f"{e.get('pattern_type','unknown')}: {e.get('account_count',0)} accounts — {e.get('detected_at','n/a')}") for e in events]
    ftc_items = [_li(f"{v.get('handle','unknown')}: ${v.get('amount',0):,.2f} — {v.get('nature','n/a')}") for v in ftc]
    fara_items = [_li(f"{f.get('registrant_name','unknown')} ({f.get('foreign_principal_country','n/a')})") for f in fara]

    footer = ""
    if monitor_excluded:
        footer = f"<p><em>Monitor sources excluded: {monitor_excluded} articles</em></p>\n"

    return (
        "<html><body>\n"
        f"<h1>Sundo Pi Daily Digest</h1>\n"
        f"<p>{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}</p>\n"
        f"<h2>Palestinian Voice Articles ({len(articles)})</h2>\n" + _ul(art_items) + "\n"
        + footer +
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

    conn = sqlite3.connect(str(SQLITE_PATH))
    try:
        digest = build_digest(conn)
    except Exception as exc:
        logger.warning("Could not build digest: %s", exc)
        digest = {"articles": [], "article_count": 0, "monitor_excluded_count": 0, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    finally:
        conn.close()

    events = _query_coordination_events()
    ftc = _query_ftc_ready()
    fara = _query_new_fara()
    text_body = _build_text(digest, events, ftc, fara)
    html_body = _build_html(digest, events, ftc, fara)

    msg = MIMEMultipart("alternative")
    date_str = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    article_count = digest.get("article_count", 0)
    msg["Subject"] = f"Sundo Pi Daily Digest — {date_str} ({article_count} Palestinian voice articles)"
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
