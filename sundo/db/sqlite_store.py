"""Sundo Pi OSINT Monitoring Platform — SQLite relational store."""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sundo.config import SQLITE_PATH

logger = logging.getLogger("sundo.db.sqlite")


def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with row factory and foreign keys."""
    conn = sqlite3.connect(str(SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all tables if they do not already exist."""
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        _create_fara_filings(conn)
        _create_irs990_orgs(conn)
        _create_irs990_grants(conn)
        _create_social_posts(conn)
        _create_rss_articles(conn)
        _create_coordination_events(conn)
        _create_ftc_violations(conn)
        apply_migrations(conn)
        conn.commit()
        logger.info("SQLite schema initialised at %s", SQLITE_PATH)
    except Exception:
        conn.rollback()
        logger.exception("Failed to initialise SQLite schema")
        raise
    finally:
        conn.close()


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Safely add missing columns to existing tables. Idempotent."""
    _add_column_if_missing(conn, "rss_articles", "digest_flagged", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "rss_articles", "reviewed", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "rss_articles", "narrative_tag", "TEXT")
    _add_column_if_missing(conn, "rss_articles", "linked_event_id", "TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Add a column to a table if it does not already exist."""
    cur = conn.execute("PRAGMA table_info(?)", (table,))
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        logger.info("Migration applied: added %s to %s", column, table)


# ---------------------------------------------------------------------------
# Schema creation helpers
# ---------------------------------------------------------------------------

def _create_fara_filings(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fara_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_number TEXT NOT NULL,
            registrant_name TEXT NOT NULL,
            foreign_principal TEXT,
            country TEXT,
            filing_date TEXT,
            form_type TEXT,
            amount_usd REAL,
            purpose TEXT,
            pdf_url TEXT,
            raw_text TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(registration_number, filing_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fara_reg_num ON fara_filings(registration_number)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fara_date ON fara_filings(filing_date)"
    )


def _create_irs990_orgs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS irs990_orgs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ein TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            city TEXT,
            state TEXT,
            zip TEXT,
            country TEXT,
            total_revenue REAL,
            total_assets REAL,
            tax_year INTEGER,
            filed_at TEXT,
            raw_json TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_irs990_ein ON irs990_orgs(ein)"
    )


def _create_irs990_grants(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS irs990_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ein TEXT NOT NULL,
            grantee_name TEXT NOT NULL,
            grantee_ein TEXT,
            amount_usd REAL,
            purpose TEXT,
            tax_year INTEGER,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ein) REFERENCES irs990_orgs(ein)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grants_ein ON irs990_grants(ein)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_grants_amount ON irs990_grants(amount_usd)"
    )


def _create_social_posts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            post_id TEXT NOT NULL,
            author_handle TEXT NOT NULL,
            author_name TEXT,
            content TEXT,
            hashtags TEXT,
            mentions TEXT,
            urls TEXT,
            posted_at TEXT,
            language TEXT,
            is_reply BOOLEAN,
            reply_to_post_id TEXT,
            engagement_score REAL,
            raw_json TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, post_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_platform_id ON social_posts(platform, post_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_author ON social_posts(author_handle)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_time ON social_posts(posted_at)"
    )


def _create_rss_articles(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rss_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('amplify','monitor')),
            title TEXT NOT NULL,
            summary TEXT,
            link TEXT NOT NULL,
            published_at TEXT,
            authors TEXT,
            tags TEXT,
            raw_html TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(feed_url, link)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rss_feed ON rss_articles(feed_url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rss_type ON rss_articles(source_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rss_published ON rss_articles(published_at)"
    )


def _create_coordination_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT UNIQUE NOT NULL,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            campaign_name TEXT,
            description TEXT,
            involved_handles TEXT,
            involved_domains TEXT,
            hashtag_burst TEXT,
            similarity_score REAL,
            estimated_accounts INTEGER,
            estimated_payment_usd REAL,
            evidence_links TEXT,
            status TEXT DEFAULT 'open' CHECK(status IN ('open','confirmed','false_positive','closed')),
            assigned_to TEXT,
            resolved_at TEXT,
            notes TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coord_uuid ON coordination_events(event_uuid)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_coord_status ON coordination_events(status)"
    )


def _create_ftc_violations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ftc_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id TEXT UNIQUE NOT NULL,
            case_name TEXT NOT NULL,
            respondent TEXT,
            violation_type TEXT,
            penalty_usd REAL,
            final_order_date TEXT,
            press_release_url TEXT,
            raw_text TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ftc_case ON ftc_violations(case_id)"
    )


# ---------------------------------------------------------------------------
# Generic CRUD helpers
# ---------------------------------------------------------------------------

def insert_one(table: str, data: Dict[str, Any]) -> Optional[int]:
    """Insert a single row; return the new row id or None on conflict/error."""
    columns = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
    conn = get_connection()
    try:
        cursor = conn.execute(sql, tuple(data.values()))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        logger.debug("IntegrityError inserting into %s", table)
        return None
    except Exception:
        conn.rollback()
        logger.exception("Error inserting into %s", table)
        return None
    finally:
        conn.close()


def insert_many(table: str, rows: List[Dict[str, Any]]) -> int:
    """Insert many rows; return count of rows actually inserted."""
    if not rows:
        return 0
    columns = ", ".join(rows[0].keys())
    placeholders = ", ".join("?" for _ in rows[0])
    sql = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
    conn = get_connection()
    inserted = 0
    try:
        for row in rows:
            try:
                conn.execute(sql, tuple(row.values()))
                inserted += 1
            except sqlite3.IntegrityError:
                pass  # duplicate / conflict
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Batch insert error into %s", table)
    finally:
        conn.close()
    return inserted


def fetch_one(table: str, where: str, params: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    """Fetch a single row as dict, or None."""
    sql = f"SELECT * FROM {table} WHERE {where} LIMIT 1"
    conn = get_connection()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    except Exception:
        logger.exception("fetch_one error on %s", table)
        return None
    finally:
        conn.close()


def fetch_many(
    table: str,
    where: Optional[str] = None,
    params: Tuple[Any, ...] = (),
    order_by: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch multiple rows as list of dicts."""
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {limit}"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("fetch_many error on %s", table)
        return []
    finally:
        conn.close()


def update_one(table: str, data: Dict[str, Any], where: str, params: Tuple[Any, ...]) -> bool:
    """Update rows matching *where* with *data*."""
    if not data:
        return False
    set_clause = ", ".join(f"{k}=?" for k in data)
    sql = f"UPDATE {table} SET {set_clause} WHERE {where}"
    conn = get_connection()
    try:
        conn.execute(sql, tuple(data.values()) + params)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("update_one error on %s", table)
        return False
    finally:
        conn.close()


def delete(table: str, where: str, params: Tuple[Any, ...]) -> bool:
    """Delete rows matching *where*."""
    sql = f"DELETE FROM {table} WHERE {where}"
    conn = get_connection()
    try:
        conn.execute(sql, params)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        logger.exception("delete error on %s", table)
        return False
    finally:
        conn.close()
