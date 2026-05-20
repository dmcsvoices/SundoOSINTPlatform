"""Tests for monitor article exclusion from digest."""
from __future__ import annotations

import logging
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure sundo is importable when running from repo root
_repo_root = Path(__file__).resolve().parent.parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from sundo.amplify.digest import (
    _feed_url_to_name,
    build_digest,
    get_digest_articles,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE rss_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            link TEXT NOT NULL,
            published_at TEXT,
            authors TEXT,
            tags TEXT,
            raw_html TEXT,
            ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def insert_article(conn, title, source_name, source_type, hours_ago=1.0, url=None):
    published_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    url = url or f"https://example.com/{title.replace(' ', '-').lower()}"
    conn.execute(
        "INSERT INTO rss_articles (title, feed_url, source_type, link, published_at) VALUES (?, ?, ?, ?, ?)",
        (title, source_name, source_type, url, published_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestDigestExclusion(unittest.TestCase):

    def test_monitor_articles_excluded(self):
        conn = make_test_db()
        insert_article(conn, "Monitor A", "https://theintercept.com/feed/?rss", "monitor")
        insert_article(conn, "Monitor B", "https://forward.com/feed/", "monitor")
        insert_article(conn, "Monitor C", "https://www.jta.org/feed", "monitor")
        amplify, excluded = get_digest_articles(conn)
        self.assertEqual(len(amplify), 0)
        self.assertEqual(excluded, 3)
        conn.close()

    def test_amplify_articles_included(self):
        conn = make_test_db()
        insert_article(conn, "Amplify A", "https://mondoweiss.net/feed/", "amplify")
        insert_article(conn, "Amplify B", "https://www.wafa.ps/rss.aspx", "amplify")
        amplify, excluded = get_digest_articles(conn)
        self.assertEqual(len(amplify), 2)
        self.assertEqual(excluded, 0)
        conn.close()

    def test_mixed_sources_only_amplify_returned(self):
        conn = make_test_db()
        insert_article(conn, "Amplify A", "https://mondoweiss.net/feed/", "amplify")
        insert_article(conn, "Amplify B", "https://www.wafa.ps/rss.aspx", "amplify")
        insert_article(conn, "Monitor A", "https://theintercept.com/feed/?rss", "monitor")
        insert_article(conn, "Monitor B", "https://forward.com/feed/", "monitor")
        insert_article(conn, "Monitor C", "https://www.jta.org/feed", "monitor")
        amplify, excluded = get_digest_articles(conn)
        self.assertEqual(len(amplify), 2)
        self.assertEqual(excluded, 3)
        conn.close()

    def test_source_type_field_present_on_all_returned(self):
        conn = make_test_db()
        insert_article(conn, "Amplify A", "https://mondoweiss.net/feed/", "amplify")
        insert_article(conn, "Monitor A", "https://theintercept.com/feed/?rss", "monitor")
        amplify, _ = get_digest_articles(conn)
        for a in amplify:
            self.assertEqual(a["source_type"], "amplify")
        conn.close()

    def test_empty_database_returns_empty_digest(self):
        conn = make_test_db()
        digest = build_digest(conn)
        self.assertEqual(digest["articles"], [])
        self.assertEqual(digest["article_count"], 0)
        self.assertEqual(digest["monitor_excluded_count"], 0)
        self.assertIn("generated_at", digest)
        conn.close()

    def test_old_articles_excluded_by_lookback(self):
        conn = make_test_db()
        insert_article(conn, "Old Article", "https://mondoweiss.net/feed/", "amplify", hours_ago=25.0)
        insert_article(conn, "New Article", "https://www.wafa.ps/rss.aspx", "amplify", hours_ago=1.0)
        amplify, _ = get_digest_articles(conn, lookback_hours=24)
        titles = [a["title"] for a in amplify]
        self.assertNotIn("Old Article", titles)
        self.assertIn("New Article", titles)
        conn.close()

    def test_limit_respected(self):
        conn = make_test_db()
        for i in range(20):
            insert_article(conn, f"Article {i}", "https://mondoweiss.net/feed/", "amplify", hours_ago=0.5)
        amplify, _ = get_digest_articles(conn, lookback_hours=24, limit=10)
        self.assertLessEqual(len(amplify), 10)
        conn.close()

    def test_monitor_count_accurate_with_old_articles(self):
        conn = make_test_db()
        insert_article(conn, "Old Monitor", "https://theintercept.com/feed/?rss", "monitor", hours_ago=25.0)
        insert_article(conn, "New Monitor", "https://forward.com/feed/", "monitor", hours_ago=1.0)
        _, excluded = get_digest_articles(conn, lookback_hours=24)
        self.assertEqual(excluded, 1)  # only the 1h-old monitor counts
        conn.close()

    def test_articles_ordered_by_published_at_desc(self):
        conn = make_test_db()
        insert_article(conn, "Middle", "https://mondoweiss.net/feed/", "amplify", hours_ago=5.0)
        insert_article(conn, "Newest", "https://www.wafa.ps/rss.aspx", "amplify", hours_ago=0.5)
        insert_article(conn, "Oldest", "https://electronicintifada.net/rss.xml", "amplify", hours_ago=10.0)
        amplify, _ = get_digest_articles(conn)
        titles = [a["title"] for a in amplify]
        self.assertEqual(titles, ["Newest", "Middle", "Oldest"])
        conn.close()

    def test_build_digest_returns_required_keys(self):
        conn = make_test_db()
        insert_article(conn, "A", "https://mondoweiss.net/feed/", "amplify")
        digest = build_digest(conn)
        self.assertIn("articles", digest)
        self.assertIn("article_count", digest)
        self.assertIn("monitor_excluded_count", digest)
        self.assertIn("generated_at", digest)
        conn.close()

    def test_build_digest_article_count_matches(self):
        conn = make_test_db()
        insert_article(conn, "A", "https://mondoweiss.net/feed/", "amplify")
        insert_article(conn, "B", "https://www.wafa.ps/rss.aspx", "amplify")
        digest = build_digest(conn)
        self.assertEqual(digest["article_count"], len(digest["articles"]))
        conn.close()

    def test_build_digest_no_monitor_articles_in_output(self):
        conn = make_test_db()
        insert_article(conn, "Amplify", "https://mondoweiss.net/feed/", "amplify")
        insert_article(conn, "Monitor", "https://theintercept.com/feed/?rss", "monitor")
        digest = build_digest(conn)
        for a in digest["articles"]:
            self.assertEqual(a["source_type"], "amplify")
        conn.close()

    def test_exclusion_logged_at_info_level(self):
        conn = make_test_db()
        insert_article(conn, "Monitor", "https://theintercept.com/feed/?rss", "monitor")
        with self.assertLogs("sundo.amplify.digest", level=logging.INFO) as cm:
            build_digest(conn)
        # Should log exclusion count at INFO
        self.assertTrue(
            any("Digest exclusion" in msg for msg in cm.output),
            f"Expected 'Digest exclusion' in logs, got: {cm.output}",
        )
        conn.close()

    def test_warning_logged_when_zero_amplify_articles(self):
        conn = make_test_db()
        with self.assertLogs("sundo.amplify.digest", level=logging.WARNING) as cm:
            build_digest(conn)
        self.assertTrue(
            any("Zero amplify articles" in msg for msg in cm.output),
            f"Expected 'Zero amplify articles' in logs, got: {cm.output}",
        )
        conn.close()


class TestFeedUrlToName(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(_feed_url_to_name("https://mondoweiss.net/feed/"), "Mondoweiss")

    def test_unknown_returns_url(self):
        self.assertEqual(_feed_url_to_name("https://unknown.site/feed"), "https://unknown.site/feed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
