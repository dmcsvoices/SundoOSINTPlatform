#!/usr/bin/env python3
"""
Migration script: Recompute verification_status for all existing authors.

Usage:
    cd /home/darren/sundo-pi
    python3 scripts/migrate_verification_status.py

Algorithm:
    - article_count >= 10  → "verified"
    - article_count >= 5 AND 2+ distinct sources → "verified"
    - article_count < 5    → "pending"
    - 3+ byline variants OR (>=5 articles but <2 sources) → "suspicious"
"""

import sys
sys.path.insert(0, "/home/darren/sundo-pi")

import json
import logging

from sundo.db.sqlite_store import get_connection
from sundo.ingest.author_extractor import compute_verification_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate_verification_status")


def migrate_sqlite():
    """Recompute and update verification_status for all authors in SQLite."""
    conn = get_connection()
    cursor = conn.cursor()

    # Get all authors
    cursor.execute("SELECT id, article_count, byline_variants FROM authors")
    authors = cursor.fetchall()

    if not authors:
        logger.info("No authors found in database.")
        conn.close()
        return 0, 0, 0, 0

    logger.info("Migrating verification_status for %d authors...", len(authors))

    counts = {"verified": 0, "pending": 0, "suspicious": 0, "unknown": 0}

    for author in authors:
        author_id = author["id"]
        article_count = author["article_count"] or 0
        byline_raw = author["byline_variants"] or "[]"
        try:
            byline_variants = json.loads(byline_raw)
        except Exception:
            byline_variants = []

        # Count distinct sources for this author
        cursor.execute(
            "SELECT COUNT(DISTINCT feed_url) FROM rss_articles WHERE author_id = ?",
            (author_id,),
        )
        source_row = cursor.fetchone()
        source_count = source_row[0] if source_row else 0

        status = compute_verification_status(article_count, source_count, byline_variants)

        cursor.execute(
            "UPDATE authors SET verification_status = ? WHERE id = ?",
            (status, author_id),
        )

        counts[status] = counts.get(status, 0) + 1

    conn.commit()
    conn.close()

    total = sum(counts.values())
    logger.info(
        "SQLite migration complete: %d verified, %d pending, %d suspicious, %d unknown (total %d)",
        counts["verified"],
        counts["pending"],
        counts["suspicious"],
        counts.get("unknown", 0),
        total,
    )
    return counts["verified"], counts["pending"], counts["suspicious"], counts.get("unknown", 0)


def migrate_neo4j():
    """Sync updated verification_status to Neo4j Author nodes."""
    try:
        from sundo.db.neo4j_client import Neo4jClient
        neo4j = Neo4jClient()
    except Exception as e:
        logger.warning("Neo4j not available (skipping Neo4j sync): %s", e)
        return 0

    if not neo4j.is_available():
        logger.warning("Neo4j unavailable; skipping Neo4j sync.")
        return 0

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, verification_status FROM authors")
    authors = cursor.fetchall()
    conn.close()

    if not authors:
        return 0

    logger.info("Syncing verification_status for %d authors to Neo4j...", len(authors))

    updated = 0
    for author in authors:
        author_id = author["id"]
        status = author["verification_status"] or "pending"

        result = neo4j._run(
            """
            MATCH (a:Author {id: $id})
            SET a.verification_status = $status
            RETURN a.id
            """,
            {"id": author_id, "status": status},
        )
        if result:
            updated += 1

    logger.info("Neo4j sync complete: %d authors updated.", updated)
    return updated


def main():
    logger.info("Starting verification_status migration...")

    verified, pending, suspicious, unknown = migrate_sqlite()
    neo4j_updated = migrate_neo4j()

    total = verified + pending + suspicious + unknown
    logger.info(
        "Done! Migrated %d authors: %d verified, %d pending, %d suspicious%s. Neo4j: %d updated.",
        total,
        verified,
        pending,
        suspicious,
        f", {unknown} unknown" if unknown else "",
        neo4j_updated,
    )


if __name__ == "__main__":
    main()
