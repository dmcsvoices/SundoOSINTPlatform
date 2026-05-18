"""Sundo Pi OSINT Monitoring Platform — FTC disclosure audit.

For every post in ``social_posts`` that does not carry a sponsorship
disclosure, check whether the author's handle appears in FARA filings
or IRS 990 grants data.  If a documented payment is found and no
disclosure exists, flag a potential FTC violation in the SQLite
``ftc_violations`` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sundo import config as _cfg

FTC_PAYMENT_THRESHOLD = getattr(_cfg, "FTC_PAYMENT_THRESHOLD", 10000.0)

from sundo.db import sqlite_store

logger = logging.getLogger("sundo.detect.disclosure")


def _now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def _column_exists(table: str, column: str) -> bool:
    """Return True if *column* exists in *table*'s SQLite schema."""
    conn = sqlite_store.get_connection()
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return any(r["name"] == column for r in cursor.fetchall())
    except Exception:
        logger.exception("Failed to check schema for %s.%s", table, column)
        return False
    finally:
        conn.close()


def _posts_needing_audit(limit: int = 500) -> List[Dict[str, Any]]:
    """Fetch posts that have not been audited for FTC disclosure.

    If the ``disclosed_sponsored`` column exists we use it directly;
    otherwise we attempt to extract the field from ``raw_json``.
    """
    conn = sqlite_store.get_connection()
    try:
        has_col = _column_exists("social_posts", "disclosed_sponsored")

        if has_col:
            cursor = conn.execute(
                """
                SELECT platform, post_id, author_handle, content, posted_at,
                       hashtags, mentions, urls, raw_json, disclosed_sponsored
                FROM social_posts
                WHERE disclosed_sponsored = FALSE
                ORDER BY posted_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            # No explicit column — fetch the most-recent batch and filter
            # via raw_json in Python.
            cursor = conn.execute(
                """
                SELECT platform, post_id, author_handle, content, posted_at,
                       hashtags, mentions, urls, raw_json
                FROM social_posts
                ORDER BY posted_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cursor.fetchall()
        posts = [dict(r) for r in rows]

        if not has_col:
            # Filter in Python
            filtered: List[Dict[str, Any]] = []
            for p in posts:
                raw = p.get("raw_json") or ""
                disclosed = False
                if raw:
                    try:
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            disclosed = bool(data.get("disclosed_sponsored", False))
                    except (json.JSONDecodeError, TypeError):
                        pass
                if not disclosed:
                    filtered.append(p)
            posts = filtered

        return posts
    except Exception:
        logger.exception("Failed to fetch posts for FTC audit")
        return []
    finally:
        conn.close()


def _search_fara_payments(handle: str) -> List[Dict[str, Any]]:
    """Search FARA filings for disbursements matching *handle*.

    The ``fara_filings`` table may contain a ``disbursements_json`` column
    or a flat ``amount_usd`` / ``purpose`` row.  We try both.
    """
    results: List[Dict[str, Any]] = []
    conn = sqlite_store.get_connection()
    try:
        has_disb = _column_exists("fara_filings", "disbursements_json")

        if has_disb:
            cursor = conn.execute(
                """
                SELECT registration_number, registrant_name, foreign_principal,
                       filing_date, amount_usd, purpose, pdf_url, disbursements_json
                FROM fara_filings
                WHERE disbursements_json LIKE ?
                """,
                (f"%{handle}%",),
            )
            for row in cursor.fetchall():
                r = dict(row)
                disb = r.get("disbursements_json") or "[]"
                try:
                    items = json.loads(disb)
                except (json.JSONDecodeError, TypeError):
                    items = []
                if not isinstance(items, list):
                    items = []
                for item in items:
                    if isinstance(item, dict) and handle.lower() in str(
                        item.get("recipient", "")
                    ).lower():
                        results.append(
                            {
                                "source": "FARA",
                                "registration_number": r["registration_number"],
                                "registrant_name": r["registrant_name"],
                                "foreign_principal": r["foreign_principal"],
                                "filing_date": r["filing_date"],
                                "amount_usd": item.get("amount", r.get("amount_usd")),
                                "purpose": item.get("purpose", r.get("purpose")),
                                "pdf_url": r["pdf_url"],
                                "matched_handle": handle,
                            }
                        )
        else:
            # Fallback: search purpose / registrant name for the handle
            cursor = conn.execute(
                """
                SELECT registration_number, registrant_name, foreign_principal,
                       filing_date, amount_usd, purpose, pdf_url
                FROM fara_filings
                WHERE purpose LIKE ? OR registrant_name LIKE ?
                """,
                (f"%{handle}%", f"%{handle}%"),
            )
            for row in cursor.fetchall():
                r = dict(row)
                amount = r.get("amount_usd") or 0.0
                if amount >= FTC_PAYMENT_THRESHOLD:
                    results.append(
                        {
                            "source": "FARA",
                            "registration_number": r["registration_number"],
                            "registrant_name": r["registrant_name"],
                            "foreign_principal": r["foreign_principal"],
                            "filing_date": r["filing_date"],
                            "amount_usd": amount,
                            "purpose": r["purpose"],
                            "pdf_url": r["pdf_url"],
                            "matched_handle": handle,
                        }
                    )
    except Exception:
        logger.exception("FARA payment search failed for %s", handle)
    finally:
        conn.close()

    return results


def _search_irs_grants(handle: str) -> List[Dict[str, Any]]:
    """Search IRS 990 grants for payments matching *handle*.

    Matches against ``grantee_name`` in the ``irs990_grants`` table.
    """
    results: List[Dict[str, Any]] = []
    conn = sqlite_store.get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT g.ein, g.grantee_name, g.grantee_ein, g.amount_usd,
                   g.purpose, g.tax_year, o.name AS org_name
            FROM irs990_grants g
            LEFT JOIN irs990_orgs o ON o.ein = g.ein
            WHERE g.grantee_name LIKE ?
            """,
            (f"%{handle}%",),
        )
        for row in cursor.fetchall():
            r = dict(row)
            amount = r.get("amount_usd") or 0.0
            if amount >= FTC_PAYMENT_THRESHOLD:
                results.append(
                    {
                        "source": "IRS990",
                        "ein": r["ein"],
                        "org_name": r["org_name"],
                        "grantee_name": r["grantee_name"],
                        "grantee_ein": r.get("grantee_ein"),
                        "amount_usd": amount,
                        "purpose": r["purpose"],
                        "tax_year": r["tax_year"],
                        "matched_handle": handle,
                    }
                )
    except Exception:
        logger.exception("IRS 990 grant search failed for %s", handle)
    finally:
        conn.close()

    return results


def _record_violation(
    post: Dict[str, Any],
    payments: List[Dict[str, Any]],
) -> None:
    """Insert a potential FTC violation row into SQLite.

    The ``ftc_violations`` table schema (from sqlite_store) is fairly
    generic, so we pack the detailed evidence into ``raw_text`` as JSON.
    """
    handle = post.get("author_handle", "unknown")
    platform = post.get("platform", "unknown")
    post_id = post.get("post_id", "unknown")

    # Build a readable case name
    case_id = f"FTC-{platform}-{post_id}-{_now().strftime('%Y%m%d%H%M%S')}"
    case_name = f"Undisclosed sponsorship by {handle} on {platform}"

    evidence = {
        "post": {
            "platform": platform,
            "post_id": post_id,
            "url": post.get("urls"),
            "content_preview": (post.get("content") or "")[:200],
            "posted_at": post.get("posted_at"),
        },
        "payments": payments,
    }

    row = {
        "case_id": case_id,
        "case_name": case_name,
        "respondent": handle,
        "violation_type": "potential_ftc_violation",
        "penalty_usd": None,
        "final_order_date": None,
        "press_release_url": None,
        "raw_text": json.dumps(evidence, default=str, indent=2),
    }

    try:
        rid = sqlite_store.insert_one("ftc_violations", row)
        if rid:
            logger.info(
                "Recorded FTC violation %s (row %s) for %s — %d payment(s) found",
                case_id,
                rid,
                handle,
                len(payments),
            )
        else:
            logger.warning("SQLite insert_one returned None for violation %s", case_id)
    except Exception:
        logger.exception("SQLite write failed for violation %s", case_id)


def run(batch_limit: int = 500) -> Tuple[int, int]:
    """Run the FTC disclosure audit.

    Args:
        batch_limit: Maximum number of unaudited posts to inspect per run.

    Returns:
        ``(posts_audited, violations_recorded)``
    """
    posts = _posts_needing_audit(limit=batch_limit)
    if not posts:
        logger.info("No posts require FTC audit at this time")
        return 0, 0

    logger.info("Auditing %d post(s) for FTC disclosure", len(posts))

    violations = 0
    audited_handles: Set[str] = set()
    handle_payments: Dict[str, List[Dict[str, Any]]] = {}

    for post in posts:
        handle = post.get("author_handle")
        if not handle:
            continue

        # Cache per-handle payment lookups
        if handle not in audited_handles:
            fara = _search_fara_payments(handle)
            irs = _search_irs_grants(handle)
            handle_payments[handle] = fara + irs
            audited_handles.add(handle)

        payments = handle_payments.get(handle, [])
        if payments:
            _record_violation(post, payments)
            violations += 1

    logger.info(
        "FTC audit complete: %d post(s) audited, %d violation(s) recorded",
        len(posts),
        violations,
    )
    return len(posts), violations


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    audited, violations = run()
    print(f"Disclosure audit complete: {audited} audited, {violations} violation(s).")
