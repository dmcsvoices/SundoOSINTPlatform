"""RSS feed aggregator for Sundo Pi OSINT platform.

Aggregates Palestinian and independent media RSS feeds,
categorising sources as 'amplify' (voices to amplify) or 'monitor'
(watch for patterns).
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore[misc,assignment]

from sundo.config import BASE_DIR, LOG_FORMAT, LOG_LEVEL, AMPLIFY_FEEDS, MONITOR_FEEDS
from sundo.db.sqlite_store import init_db, insert_many

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("rss_aggregator")

# ---------------------------------------------------------------------------
# Feed catalogues
# ---------------------------------------------------------------------------

AMPLIFY_FEEDS: Dict[str, str] = {
    "Wafa News Agency": "https://english.wafa.ps/rss.xml",
    "+972 Magazine": "https://www.972mag.com/feed/",
    "Mondoweiss": "https://mondoweiss.net/feed/",
    "Middle East Eye": "https://www.middleeasteye.net/rss",
    "Drop Site News": "https://dropsitenews.com/rss.xml",
    "Al-Quds": "https://www.alquds.com/feed/",
    "Electronic Intifada": "https://electronicintifada.net/rss.xml",
    "Haaretz English": "https://www.haaretz.com/rss",
}

MONITOR_FEEDS: Dict[str, str] = {
    "The Intercept": "https://theintercept.com/feed/",
    "The Forward": "https://forward.com/feed/",
    "Jewish Telegraphic Agency": "https://www.jta.org/feed/",
}

# Combined catalogue: name -> (url, source_type)
_ALL_FEEDS: Dict[str, tuple[str, str]] = {}
for _name, _url in AMPLIFY_FEEDS:
    _ALL_FEEDS[_name] = (_url, "amplify")
for _name, _url in MONITOR_FEEDS:
    _ALL_FEEDS[_name] = (_url, "monitor")

_MIN_DELAY = 2.0
_MAX_DELAY = 5.0


def _sleep_between_requests() -> None:
    """Pause for a random duration between feed fetches."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    time.sleep(delay)


def _parse_published(entry: Dict[str, Any]) -> Optional[str]:
    """Extract and normalise a published date from a feedparser entry.

    Args:
        entry: Single feedparser entry dict.

    Returns:
        ISO-formatted datetime string, or *None* if unparseable.
    """
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if published:
        try:
            dt = datetime(*published[:6])
            return dt.isoformat()
        except (ValueError, TypeError):
            pass

    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if raw:
            try:
                dt = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
                return dt.isoformat()
            except ValueError:
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
                    return dt.isoformat()
                except ValueError:
                    pass
    return None


def _extract_authors(entry: Dict[str, Any]) -> Optional[str]:
    """Collapse feedparser author structures to a comma-separated string.

    Args:
        entry: Single feedparser entry dict.

    Returns:
        Comma-separated author names, or *None*.
    """
    authors = entry.get("authors", [])
    if authors:
        names = [a.get("name", "") for a in authors if a.get("name")]
        return ", ".join(names) if names else None
    return entry.get("author")


def _extract_tags(entry: Dict[str, Any]) -> Optional[str]:
    """Collapse feedparser tag structures to a comma-separated string.

    Args:
        entry: Single feedparser entry dict.

    Returns:
        Comma-separated tag terms, or *None*.
    """
    tags = entry.get("tags", [])
    if tags:
        terms = [t.get("term", "") for t in tags if t.get("term")]
        return ", ".join(terms) if terms else None
    return None


def fetch_feed(feed_url: str, source_name: str, source_type: str) -> List[Dict[str, Any]]:
    """Fetch and parse a single RSS / Atom feed.

    Args:
        feed_url: Raw URL of the feed.
        source_name: Human-readable source label.
        source_type: Either ``'amplify'`` or ``'monitor'``.

    Returns:
        List of article row-dicts ready for insertion into ``rss_articles``.
    """
    if feedparser is None:
        logger.error("feedparser library is not installed; cannot fetch RSS feeds")
        return []

    logger.info("Fetching feed: %s (%s)", source_name, feed_url)

    # Normalise feed URL for storage (strip query params so ?rss variants� collapse to the canonical form)
    canonical_feed_url = feed_url.split("?")[0].rstrip("/")

    try:
        feed = feedparser.parse(feed_url)
    except Exception as exc:
        logger.warning("Failed to parse feed %s: %s", feed_url, exc)
        return []

    if feed.get("bozo_exception"):
        logger.warning(
            "Feed %s has parse issues: %s",
            feed_url,
            feed.bozo_exception,
        )

    articles: List[Dict[str, Any]] = []
    for entry in feed.get("entries", []):
        try:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = entry.get("summary") or entry.get("description") or ""

            if not title or not link:
                logger.debug(
                    "Skipping entry with missing title/link from %s", source_name
                )
                continue

            articles.append(
                {
                    "feed_url": canonical_feed_url,
                    "source_type": source_type,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published_at": _parse_published(entry),
                    "authors": _extract_authors(entry),
                    "tags": _extract_tags(entry),
                    "raw_html": str(entry),
                }
            )
        except Exception as exc:
            logger.warning("Error parsing entry from %s: %s", source_name, exc)
            continue

    logger.info("Fetched %d articles from %s", len(articles), source_name)
    return articles


def run() -> int:
    """Main entry point: crawl all configured feeds and persist to SQLite.

    Returns:
        Number of newly inserted articles.
    """
    logger.info("Starting RSS aggregator run")
    init_db()

    if feedparser is None:
        logger.error("feedparser is missing; RSS ingest skipped")
        return 0

    all_articles: List[Dict[str, Any]] = []

    for source_name, (feed_url, source_type) in _ALL_FEEDS.items():
        try:
            articles = fetch_feed(feed_url, source_name, source_type)
            all_articles.extend(articles)
        except Exception as exc:
            logger.exception("Unhandled exception fetching %s: %s", source_name, exc)

        _sleep_between_requests()

    inserted = insert_many("rss_articles", all_articles)
    logger.info(
        "RSS aggregator complete: %d total articles, %d inserted",
        len(all_articles),
        inserted,
    )
    return inserted


if __name__ == "__main__":
    run()
