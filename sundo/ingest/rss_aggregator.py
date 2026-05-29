"""RSS feed aggregator for Sundo Pi OSINT platform.

Aggregates Palestinian and independent media RSS feeds,
categorising sources as 'amplify' (voices to amplify) or 'monitor'
(watch for patterns).
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore[misc,assignment]

from sundo.config import BASE_DIR, LOG_FORMAT, LOG_LEVEL, AMPLIFY_FEEDS, MONITOR_FEEDS
from sundo.db.sqlite_store import init_db, insert_many, get_connection, upsert_author_sqlite
from sundo.ingest.author_extractor import extract_author, detect_language

try:
    from sundo.db.neo4j_client import Neo4jClient
except Exception:
    Neo4jClient = None  # type: ignore[misc,assignment]

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
for _name, _url in AMPLIFY_FEEDS.items():
    _ALL_FEEDS[_name] = (_url, "amplify")
for _name, _url in MONITOR_FEEDS.items():
    _ALL_FEEDS[_name] = (_url, "monitor")

_MIN_DELAY = 2.0
_MAX_DELAY = 5.0


def _sleep_between_requests() -> None:
    """Pause for a random duration between feed fetches."""
    delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
    time.sleep(delay)


def _article_id(link: str) -> str:
    """Generate a stable article node ID from its URL."""
    h = hashlib.md5(link.encode()).hexdigest()[:8]
    domain = "article"
    if "://" in link:
        domain_part = link.split("://", 1)[1].split("/", 1)[0]
        domain = domain_part.replace("www.", "").replace(".", "_")
    return f"article_{domain}__{h}"


def _source_org_id(source_name: str) -> str:
    """Generate a stable organization ID for an RSS source."""
    name_lower = source_name.lower()
    if "middle east eye" in name_lower:
        return "mee"
    if "al-quds" in name_lower or "alquds" in name_lower:
        return "alquds"
    if "forward" in name_lower:
        return "forward"
    if "intercept" in name_lower:
        return "intercept"
    if "jta" in name_lower or "jewish telegraphic" in name_lower:
        return "jta"
    if "972" in name_lower:
        return "972mag"
    if "mondoweiss" in name_lower:
        return "mondoweiss"
    if "electronic intifada" in name_lower:
        return "ei"
    if "wafa" in name_lower:
        return "wafa"
    if "haaretz" in name_lower:
        return "haaretz"
    if "drop site" in name_lower:
        return "dropsite"
    return "".join(w[0] for w in source_name.split() if w).lower()[:8]


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


def fetch_feed(feed_url: str, source_name: str, source_type: str) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Fetch and parse a single RSS / Atom feed.

    Args:
        feed_url: Raw URL of the feed.
        source_name: Human-readable source label.
        source_type: Either ``'amplify'`` or ``'monitor'``.

    Returns:
        Tuple of (articles, authors) where articles are row-dicts ready
        for insertion into ``rss_articles`` and authors is a dict mapping
        author_id to author_data dict.
    """
    if feedparser is None:
        logger.error("feedparser library is not installed; cannot fetch RSS feeds")
        return [], {}

    logger.info("Fetching feed: %s (%s)", source_name, feed_url)

    # Normalise feed URL for storage (strip query params so ?rss variants collapse to the canonical form)
    canonical_feed_url = feed_url.split("?")[0].rstrip("/")

    try:
        feed = feedparser.parse(feed_url)
    except Exception as exc:
        logger.warning("Failed to parse feed %s: %s", feed_url, exc)
        return [], {}

    if feed.get("bozo_exception"):
        logger.warning(
            "Feed %s has parse issues: %s",
            feed_url,
            feed.bozo_exception,
        )

    articles: List[Dict[str, Any]] = []
    authors: Dict[str, Dict[str, Any]] = {}
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

            author_data = extract_author(entry)
            if author_data:
                author_data["primary_language"] = detect_language(title)
                authors[author_data["id"]] = author_data

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
                    "author_id": author_data["id"] if author_data else None,
                    "author_display_name": author_data["display_name"] if author_data else None,
                }
            )
        except Exception as exc:
            logger.warning("Error parsing entry from %s: %s", source_name, exc)
            continue

    logger.info("Fetched %d articles from %s", len(articles), source_name)
    return articles, authors


def run() -> int:
    """Main entry point: crawl all configured feeds and persist to SQLite and Neo4j.

    Returns:
        Number of newly inserted articles.
    """
    logger.info("Starting RSS aggregator run")
    init_db()

    if feedparser is None:
        logger.error("feedparser is missing; RSS ingest skipped")
        return 0

    all_articles: List[Dict[str, Any]] = []
    all_authors: Dict[str, Dict[str, Any]] = {}

    for source_name, (feed_url, source_type) in _ALL_FEEDS.items():
        try:
            articles, authors = fetch_feed(feed_url, source_name, source_type)
            all_articles.extend(articles)
            all_authors.update(authors)
        except Exception as exc:
            logger.exception("Unhandled exception fetching %s: %s", source_name, exc)

        _sleep_between_requests()

    # Persist authors to SQLite and Neo4j
    conn = get_connection()
    neo4j: Any = None
    if Neo4jClient is not None:
        try:
            neo4j = Neo4jClient()
        except Exception as exc:
            logger.warning("Neo4j client unavailable: %s", exc)

    try:
        now = datetime.utcnow().isoformat()

        # Upsert all unique authors
        for author_data in all_authors.values():
            try:
                upsert_author_sqlite(conn, author_data)
            except Exception as exc:
                logger.warning("Failed to upsert author to SQLite %s: %s", author_data.get("id"), exc)
            if neo4j is not None and neo4j.is_available():
                try:
                    neo4j.upsert_author(author_data)
                except Exception as exc:
                    logger.warning("Failed to upsert author to Neo4j %s: %s", author_data.get("id"), exc)

        # Ensure source organizations exist in Neo4j
        if neo4j is not None and neo4j.is_available():
            for source_name, (feed_url, source_type) in _ALL_FEEDS.items():
                org_id = _source_org_id(source_name)
                try:
                    neo4j.upsert_org(
                        name=source_name,
                        ein=f"rss:{org_id}",
                        org_type="media",
                    )
                except Exception as exc:
                    logger.warning("Failed to upsert org %s: %s", source_name, exc)

        # Upsert articles and link authors
        for article in all_articles:
            author_id = article.get("author_id")
            link = article.get("link")
            if neo4j is not None and neo4j.is_available() and link:
                art_id = _article_id(link)
                try:
                    neo4j.upsert_article(
                        article_id=art_id,
                        title=article.get("title", ""),
                        link=link,
                        published_at=article.get("published_at"),
                        source_name=article.get("source_name", ""),
                    )
                except Exception as exc:
                    logger.warning("Failed to upsert article to Neo4j %s: %s", art_id, exc)

                if author_id:
                    try:
                        neo4j.link_author_to_article(
                            author_id=author_id,
                            article_id=art_id,
                            published_at=article.get("published_at") or now,
                            source_name=article.get("source_name", ""),
                        )
                    except Exception as exc:
                        logger.warning("Failed to link author to article %s: %s", art_id, exc)

                    # Link author to source organization
                    source_name_from_feed = None
                    feed_url_norm = article.get("feed_url", "").split("?")[0].rstrip("/")
                    for sn, (fu, _) in _ALL_FEEDS.items():
                        if fu.split("?")[0].rstrip("/") == feed_url_norm:
                            source_name_from_feed = sn
                            break
                    if source_name_from_feed:
                        org_id = _source_org_id(source_name_from_feed)
                        try:
                            neo4j.link_author_to_organization(
                                author_id=author_id,
                                org_id=f"rss:{org_id}",
                                article_count=1,
                                first_seen=article.get("published_at") or now,
                            )
                        except Exception as exc:
                            logger.warning("Failed to link author to org %s: %s", org_id, exc)

        inserted = insert_many("rss_articles", all_articles)
    finally:
        conn.close()
        if neo4j is not None:
            try:
                neo4j.close()
            except Exception:
                pass

    logger.info(
        "RSS aggregator complete: %d total articles, %d inserted, %d authors",
        len(all_articles),
        inserted,
        len(all_authors),
    )
    return inserted


if __name__ == "__main__":
    run()
