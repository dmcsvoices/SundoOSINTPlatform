"""Nightly markdown report generator."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from sundo.config import (
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    REPORTS_DIR,
    REPORT_ARCHIVE_DAYS,
    SQLITE_PATH,
)

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _ensure_dirs() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _clean_old_reports() -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=REPORT_ARCHIVE_DAYS)
    for p in REPORTS_DIR.glob("*.md"):
        try:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc)
            if mtime < cutoff:
                p.unlink()
                logger.info("Archived old report: %s", p.name)
        except OSError:
            pass
    for p in REPORTS_DIR.glob("*.txt"):
        try:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime, tz=dt.timezone.utc)
            if mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _query_sqlite(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as exc:
        logger.warning("SQLite query failed: %s", exc)
        return []


def _neo4j_query(cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if GraphDatabase is None:
        logger.warning("neo4j driver not available")
        return []
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(cypher, params or {})
            return [r.data() for r in result]
    except Exception as exc:
        logger.warning("Neo4j query failed: %s", exc)
        return []
    finally:
        try:
            driver.close()
        except Exception:
            pass


def _fetch_coordination_events() -> list[dict[str, Any]]:
    return _neo4j_query(
        "MATCH (e:CoordinationEvent) RETURN e.id AS id, e.detected_at AS detected_at, "
        "e.account_count AS account_count, e.pattern_type AS pattern_type "
        "ORDER BY e.detected_at DESC LIMIT 20"
    )


def _fetch_new_fara() -> list[dict[str, Any]]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    return _query_sqlite(
        "SELECT * FROM fara_filings WHERE filed_at >= ? ORDER BY filed_at DESC",
        (since,),
    )


def _fetch_ftc_candidates() -> list[dict[str, Any]]:
    return _query_sqlite(
        "SELECT * FROM ftc_violations WHERE status = 'candidate' ORDER BY amount DESC"
    )


def _fetch_palestinian_voices() -> list[dict[str, Any]]:
    return _neo4j_query(
        "MATCH (v:PalestinianVoice) "
        "RETURN v.handle AS handle, v.name AS name, v.reach_score AS reach_score, "
        "v.follower_count AS follower_count, v.avg_engagement AS avg_engagement "
        "ORDER BY v.reach_score DESC LIMIT 10"
    )


def _fetch_funding_updates() -> list[dict[str, Any]]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    return _neo4j_query(
        "MATCH (f:Funding)-[:FUNDS]->(t) "
        "WHERE f.updated_at >= $since "
        "RETURN f.id AS id, f.amount AS amount, f.source AS source, "
        "labels(t)[0] AS target_type, t.name AS target_name "
        "ORDER BY f.amount DESC LIMIT 20",
        {"since": since},
    )


def _section(title: str, items: list[dict[str, Any]], fmt: str = "md") -> str:
    lines: list[str] = [f"## {title}", ""]
    if not items:
        lines.append("_No records._")
        lines.append("")
        return "\n".join(lines)
    if fmt == "md":
        for item in items:
            bullet = "- " + ", ".join(f"{k}: {v}" for k, v in item.items() if v is not None)
            lines.append(bullet)
    else:
        for item in items:
            bullet = "* " + ", ".join(f"{k}: {v}" for k, v in item.items() if v is not None)
            lines.append(bullet)
    lines.append("")
    return "\n".join(lines)


def generate_report() -> Path:
    """Generate nightly markdown and plain-text reports."""
    _ensure_dirs()
    _clean_old_reports()
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    md_path = REPORTS_DIR / f"{today}.md"
    txt_path = REPORTS_DIR / f"{today}.txt"

    coord = _fetch_coordination_events()
    fara = _fetch_new_fara()
    ftc = _fetch_ftc_candidates()
    voices = _fetch_palestinian_voices()
    funding = _fetch_funding_updates()

    md_lines: list[str] = [
        f"# Sundo Pi Nightly Report — {today}",
        "",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
    ]

    md_lines.append(_section("Coordination Events Detected", coord, "md"))
    md_lines.append(_section("New FARA Filings", fara, "md"))
    md_lines.append(_section("FTC Violation Candidates", ftc, "md"))
    md_lines.append(_section("Palestinian Voices (Top 10 by Reach)", voices, "md"))
    md_lines.append(_section("Funding Trail Updates", funding, "md"))

    md_body = "\n".join(md_lines)
    md_path.write_text(md_body, encoding="utf-8")
    logger.info("Report written: %s", md_path)

    # Plain text version
    txt_lines: list[str] = [
        f"Sundo Pi Nightly Report — {today}",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat()}",
        "",
    ]
    txt_lines.append(_section("Coordination Events Detected", coord, "txt"))
    txt_lines.append(_section("New FARA Filings", fara, "txt"))
    txt_lines.append(_section("FTC Violation Candidates", ftc, "txt"))
    txt_lines.append(_section("Palestinian Voices (Top 10 by Reach)", voices, "txt"))
    txt_lines.append(_section("Funding Trail Updates", funding, "txt"))

    txt_body = "\n".join(txt_lines)
    txt_path.write_text(txt_body, encoding="utf-8")
    logger.info("Plain-text report written: %s", txt_path)

    return md_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    generate_report()
