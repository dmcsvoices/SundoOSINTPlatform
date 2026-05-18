"""Sundo Pi OSINT monitoring platform — main entry point."""
from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:
    BackgroundScheduler = None  # type: ignore[misc,assignment]

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

from sundo.config import (
    BASE_DIR,
    LOG_FORMAT,
    LOG_LEVEL,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    SQLITE_PATH,
)
from sundo.report.alert_engine import check_and_alert
from sundo.report.cytoscape_export import export_graph
from sundo.report.report_generator import generate_report
from sundo.amplify.digest import send_digest
from sundo.amplify.ftc_packager import generate_ftc_packages
from sundo.amplify.voice_registry import ensure_seed_voices
try:
    from sundo.dashboard.app import run_dashboard
except Exception:
    run_dashboard = None  # type: ignore[misc,assignment]

logger = logging.getLogger("sundo.main")


def _init_sqlite() -> None:
    """Ensure SQLite database and core tables exist."""
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fara_filings (
            id INTEGER PRIMARY KEY,
            registrant_name TEXT,
            foreign_principal_country TEXT,
            filed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS ftc_violations (
            id INTEGER PRIMARY KEY,
            handle TEXT,
            platform TEXT,
            amount REAL,
            nature TEXT,
            post_url TEXT,
            payment_evidence TEXT,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS rss_articles (
            id INTEGER PRIMARY KEY,
            title TEXT,
            url TEXT,
            source TEXT,
            published_at TEXT
        );
        CREATE TABLE IF NOT EXISTS coordination_events (
            id TEXT PRIMARY KEY,
            pattern_type TEXT,
            detected_at TEXT,
            account_count INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    logger.info("SQLite initialized: %s", SQLITE_PATH)


def _init_neo4j_schema() -> None:
    """Ensure Neo4j constraints and indexes exist."""
    if GraphDatabase is None:
        logger.warning("Neo4j driver not available; skipping schema init")
        return
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            session.run(
                "CREATE CONSTRAINT coordination_event_id IF NOT EXISTS "
                "FOR (e:CoordinationEvent) REQUIRE e.id IS UNIQUE"
            )
            session.run(
                "CREATE CONSTRAINT palestinian_voice_handle IF NOT EXISTS "
                "FOR (v:PalestinianVoice) REQUIRE v.handle IS UNIQUE"
            )
            session.run(
                "CREATE INDEX fara_filing_registrant IF NOT EXISTS "
                "FOR (f:FARAFiling) ON (f.registrant_name)"
            )
        driver.close()
        logger.info("Neo4j schema initialized")
    except Exception as exc:
        logger.warning("Neo4j schema init failed (may already exist): %s", exc)


def _register_jobs(scheduler: Any) -> None:
    """Register all APScheduler jobs with their schedules."""
    # Ingest jobs (stubs — real implementations live in sundo.ingest / sundo.detect)
    # FARA: weekly Sunday 02:00
    scheduler.add_job(
        lambda: logger.info("FARA ingest job stub"),
        trigger="cron",
        day_of_week="sun",
        hour=2,
        minute=0,
        id="fara_ingest",
        replace_existing=True,
    )
    # IRS990: monthly 1st 03:00
    scheduler.add_job(
        lambda: logger.info("IRS990 ingest job stub"),
        trigger="cron",
        day=1,
        hour=3,
        minute=0,
        id="irs990_ingest",
        replace_existing=True,
    )
    # RSS: every 2 hours
    scheduler.add_job(
        lambda: logger.info("RSS ingest job stub"),
        trigger="interval",
        hours=2,
        id="rss_ingest",
        replace_existing=True,
    )
    # Social: every 4 hours
    scheduler.add_job(
        lambda: logger.info("Social ingest job stub"),
        trigger="interval",
        hours=4,
        id="social_ingest",
        replace_existing=True,
    )
    # Timing analysis: every 6 hours
    scheduler.add_job(
        lambda: logger.info("Timing analysis job stub"),
        trigger="interval",
        hours=6,
        id="timing_analysis",
        replace_existing=True,
    )
    # Similarity: every 6 hours (after timing)
    scheduler.add_job(
        lambda: logger.info("Similarity job stub"),
        trigger="interval",
        hours=6,
        minutes=5,
        id="similarity",
        replace_existing=True,
    )
    # Network graph: nightly
    scheduler.add_job(
        lambda: logger.info("Network graph job stub"),
        trigger="cron",
        hour=2,
        minute=30,
        id="network_graph",
        replace_existing=True,
    )
    # Disclosure audit: nightly
    scheduler.add_job(
        lambda: logger.info("Disclosure audit job stub"),
        trigger="cron",
        hour=3,
        minute=0,
        id="disclosure_audit",
        replace_existing=True,
    )
    # Report generator: nightly
    scheduler.add_job(
        generate_report,
        trigger="cron",
        hour=4,
        minute=0,
        id="report_generator",
        replace_existing=True,
    )
    # Cytoscape export: nightly (after report)
    scheduler.add_job(
        export_graph,
        trigger="cron",
        hour=4,
        minute=30,
        id="cytoscape_export",
        replace_existing=True,
    )
    # Digest: daily 07:00
    scheduler.add_job(
        send_digest,
        trigger="cron",
        hour=7,
        minute=0,
        id="daily_digest",
        replace_existing=True,
    )
    # Immediate alert check every 15 minutes
    scheduler.add_job(
        check_and_alert,
        trigger="interval",
        minutes=15,
        id="alert_check",
        replace_existing=True,
    )
    # Seed voice registry on startup and weekly
    scheduler.add_job(
        ensure_seed_voices,
        trigger="cron",
        day_of_week="mon",
        hour=5,
        minute=0,
        id="voice_registry",
        replace_existing=True,
    )
    # FTC packager nightly
    scheduler.add_job(
        generate_ftc_packages,
        trigger="cron",
        hour=5,
        minute=30,
        id="ftc_packager",
        replace_existing=True,
    )


def main() -> None:
    """Graceful startup: init stores, start scheduler, block until signal."""
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format=LOG_FORMAT,
    )
    logger.info("Sundo Pi starting up...")

    _init_sqlite()
    _init_neo4j_schema()
    ensure_seed_voices()

    if BackgroundScheduler is None:
        logger.error("APScheduler not installed; cannot start scheduler.")
        sys.exit(1)

    scheduler = BackgroundScheduler()
    _register_jobs(scheduler)

    # Signal handlers for clean shutdown
    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down scheduler...", signum)
        scheduler.shutdown(wait=True)
        logger.info("Scheduler stopped. Exiting.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scheduler.start()
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Start Flask dashboard in a background thread
    if run_dashboard is not None:
        import threading
        def _start_dashboard():
            try:
                run_dashboard()
            except Exception as exc:
                logger.warning("Dashboard failed to start: %s", exc)
        threading.Thread(target=_start_dashboard, daemon=True, name="dashboard").start()
        logger.info("Dashboard thread started")

    # Block main thread
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
