#!/usr/bin/env python3
"""
Backfill script: Extract authors from existing rss_articles and populate
both SQLite authors table and Neo4j Author nodes.

Usage:
    cd /home/darren/sundo-pi
    python3 scripts/backfill_authors.py
"""

import sys
sys.path.insert(0, "/home/darren/sundo-pi")

import json
import logging
from datetime import datetime
from sundo.db.sqlite_store import get_connection
from sundo.ingest.author_extractor import extract_author

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_authors")


def backfill_sqlite():
    """Extract authors from existing articles and populate SQLite."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get all articles that have raw author data but no author_id
    cursor.execute("""
        SELECT id, feed_url, title, authors, link, published_at, source_type
        FROM rss_articles
        WHERE author_id IS NULL AND authors IS NOT NULL AND authors != ''
    """)
    articles = cursor.fetchall()

    if not articles:
        logger.info("No articles need author backfill.")
        conn.close()
        return 0, 0

    logger.info("Backfilling authors for %d articles...", len(articles))

    backfilled = 0
    authors_created = 0
    seen_authors = {}  # id -> display_name

    for article in articles:
        article_id = article['id']
        feed_url = article['feed_url']
        title = article['title']
        authors_json = article['authors']
        source_type = article['source_type'] if article['source_type'] else 'rss'

        # Parse authors field (could be JSON array or plain string)
        try:
            if authors_json.startswith('['):
                authors_data = json.loads(authors_json)
            else:
                authors_data = authors_json
        except (json.JSONDecodeError, TypeError):
            authors_data = authors_json

        # Extract author using the same logic as during ingestion
        author = extract_author({'author': authors_data, 'title': title})

        if author and author.get('id'):
            author_id = author['id']
            display_name = author.get('display_name', author_id)
            handle = author.get('handle', author_id)

            # Track unique authors
            if author_id not in seen_authors:
                seen_authors[author_id] = display_name
                authors_created += 1

                # Upsert author to SQLite
                cursor.execute("""
                    INSERT INTO authors (id, display_name, handle, article_count, first_seen, last_seen, verification_status)
                    VALUES (?, ?, ?, 1, ?, ?, 'pending')
                    ON CONFLICT(id) DO UPDATE SET
                        article_count = article_count + 1,
                        last_seen = excluded.last_seen,
                        display_name = COALESCE(excluded.display_name, authors.display_name)
                """, (author_id, display_name, handle,
                      article['published_at'] or datetime.now().isoformat(),
                      datetime.now().isoformat()))
            else:
                # Just increment count
                cursor.execute("""
                    UPDATE authors SET article_count = article_count + 1, last_seen = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), author_id))

            # Update article with author reference
            cursor.execute("""
                UPDATE rss_articles
                SET author_id = ?, author_display_name = ?
                WHERE id = ?
            """, (author_id, display_name, article_id))

            backfilled += 1

    conn.commit()
    conn.close()

    logger.info("Backfilled %d articles, created/updated %d unique authors.", backfilled, authors_created)
    return backfilled, authors_created


def backfill_neo4j():
    """Create Neo4j Author nodes and relationships from SQLite data."""
    try:
        from sundo.db import neo4j_client
        driver = neo4j_client.get_neo4j_driver()
    except Exception as e:
        logger.warning("Neo4j not available (skipping Neo4j sync): %s", e)
        return 0

    conn = get_connection()
    cursor = conn.cursor()

    # Get all authors with their articles and organizations
    cursor.execute("""
        SELECT a.id, a.display_name, a.handle, a.article_count,
               r.feed_url, r.source_type
        FROM authors a
        LEFT JOIN rss_articles r ON r.author_id = a.id
    """)

    rows = cursor.fetchall()
    if not rows:
        logger.info("No authors to sync to Neo4j.")
        conn.close()
        return 0

    logger.info("Syncing %d author records to Neo4j...", len(rows))

    with driver.session() as session:
        for row in rows:
            author_id = row['id']
            display_name = row['display_name']
            handle = row['handle']
            article_count = row['article_count']
            feed_url = row['feed_url']
            source_type = row['source_type'] or 'rss'

            # Create Author node
            session.run("""
                MERGE (a:Author {id: $id})
                SET a.display_name = $display_name,
                    a.handle = $handle,
                    a.article_count = $article_count,
                    a.source_type = $source_type
            """, {
                'id': author_id,
                'display_name': display_name,
                'handle': handle,
                'article_count': article_count,
                'source_type': source_type
            })

            # Link to Organization via WRITES_FOR
            if feed_url:
                session.run("""
                    MATCH (a:Author {id: $author_id})
                    MATCH (o:Organization)
                    WHERE o.rss_feed = $feed_url OR o.website = $feed_url
                    MERGE (a)-[:WRITES_FOR]->(o)
                """, {'author_id': author_id, 'feed_url': feed_url})

            # Link to Articles via WROTE
            session.run("""
                MATCH (a:Author {id: $author_id})
                MATCH (art:Article {author_id: $author_id})
                MERGE (a)-[:WROTE]->(art)
            """, {'author_id': author_id})

    driver.close()
    conn.close()

    logger.info("Neo4j sync complete.")
    return len(rows)


def main():
    logger.info("Starting author backfill...")

    # Step 1: SQLite backfill
    articles_backfilled, authors_created = backfill_sqlite()

    if articles_backfilled == 0:
        logger.info("No articles needed backfilling. Checking if authors already exist...")
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM authors")
        author_count = cursor.fetchone()[0]
        conn.close()

        if author_count == 0:
            logger.warning("No authors in database. Run RSS ingestion to populate authors going forward.")
            return

    # Step 2: Neo4j sync
    neo4j_count = backfill_neo4j()

    logger.info("Done! Backfilled %d articles, %d unique authors, synced %d to Neo4j.",
                articles_backfilled, authors_created, neo4j_count)


if __name__ == "__main__":
    main()
