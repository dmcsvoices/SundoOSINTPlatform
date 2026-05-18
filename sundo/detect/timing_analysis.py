"""Sundo Pi OSINT Monitoring Platform — Timing / burst analysis detector.

Sliding-window burst detection on social media posts.  For each configured
watchlist hashtag we examine posts from the last 24 h, sort by timestamp, and
slide a 30-minute window across the timeline.  If >= 5 distinct accounts post
within any window a :CoordinationEvent is created in Neo4j and a row is
inserted into the SQLite ``coordination_events`` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4

from sundo import config as _cfg

BURST_WINDOW_MINUTES = getattr(_cfg, "BURST_WINDOW_MINUTES", 30)
BURST_THRESHOLD_ACCOUNTS = getattr(_cfg, "BURST_THRESHOLD_ACCOUNTS", 5)
WATCHLIST_HASHTAGS: List[str] = getattr(_cfg, "WATCHLIST_HASHTAGS", [])

from sundo.db import neo4j_client, sqlite_store

logger = logging.getLogger("sundo.detect.timing")


def _now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def _hashtag_posts(
    hashtag: str,
    since: datetime,
) -> List[Dict[str, Any]]:
    """Fetch posts containing *hashtag* posted after *since*.

    We query the ``social_posts`` SQLite table and do a naive LIKE match on
    the ``hashtags`` column (comma-separated or JSON blob).  If the column is
    NULL we fall back to a LIKE on ``content``.
    """
    since_iso = since.isoformat()
    conn = sqlite_store.get_connection()
    try:
        # Try exact match in hashtags column first
        cursor = conn.execute(
            """
            SELECT platform, post_id, author_handle, content, posted_at,
                   hashtags, mentions, urls, raw_json
            FROM social_posts
            WHERE posted_at >= ?
              AND (
                  hashtags LIKE ?
                  OR (hashtags IS NULL AND content LIKE ?)
              )
            ORDER BY posted_at ASC
            """,
            (since_iso, f"%{hashtag}%", f"%#{hashtag}%"),
        )
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("Failed to query posts for hashtag %s", hashtag)
        return []
    finally:
        conn.close()


def _find_bursts(
    posts: List[Dict[str, Any]],
    window_minutes: int,
    threshold_accounts: int,
) -> List[Dict[str, Any]]:
    """Slide a fixed-width window over *posts* and return burst events.

    Each returned dict contains:
        - ``window_start`` (datetime)
        - ``window_end``   (datetime)
        - ``handles``      (Set[str]) distinct account handles
        - ``posts``        (List[Dict]) posts inside the window
        - ``hashtag``      (str) the hashtag that triggered the burst
    """
    bursts: List[Dict[str, Any]] = []
    if not posts:
        return bursts

    window = timedelta(minutes=window_minutes)

    # Convert posted_at strings → datetime objects once
    for p in posts:
        ts = p.get("posted_at")
        if isinstance(ts, str):
            # strip trailing 'Z' → +00:00 for fromisoformat
            ts = ts.replace("Z", "+00:00")
            try:
                p["_dt"] = datetime.fromisoformat(ts)
            except ValueError:
                p["_dt"] = _now()
        elif isinstance(ts, datetime):
            p["_dt"] = ts
        else:
            p["_dt"] = _now()

    posts.sort(key=lambda x: x["_dt"])

    n = len(posts)
    left = 0
    for right in range(n):
        # Expand window until it exceeds window_minutes, then shrink from left
        while left <= right and (posts[right]["_dt"] - posts[left]["_dt"]) > window:
            left += 1

        # Count distinct handles in current window [left, right]
        handles: Set[str] = set()
        window_posts: List[Dict[str, Any]] = []
        for i in range(left, right + 1):
            h = posts[i].get("author_handle")
            if h:
                handles.add(h)
            window_posts.append(posts[i])

        if len(handles) >= threshold_accounts:
            # Only record if this is a *new* burst (start moved since last)
            if not bursts or bursts[-1]["window_start"] != posts[left]["_dt"]:
                bursts.append(
                    {
                        "window_start": posts[left]["_dt"],
                        "window_end": posts[right]["_dt"],
                        "handles": handles,
                        "posts": window_posts,
                        "hashtag": posts[0].get("hashtag", ""),
                    }
                )
    return bursts


def _record_burst(
    burst: Dict[str, Any],
    hashtag: str,
) -> None:
    """Persist a single burst to Neo4j (best-effort) and SQLite."""
    event_uuid = str(uuid4())
    handles = sorted(burst["handles"])
    domains: List[str] = []
    evidence_links: List[str] = []
    for p in burst["posts"]:
        # Gather URLs as evidence
        raw_urls = p.get("urls") or ""
        if raw_urls:
            try:
                urls = json.loads(raw_urls)
                if isinstance(urls, list):
                    evidence_links.extend(urls)
            except (json.JSONDecodeError, TypeError):
                if isinstance(raw_urls, str):
                    evidence_links.extend(
                        [u.strip() for u in raw_urls.split(",") if u.strip()]
                    )
        # Build a per-post permalink if we can
        platform = p.get("platform", "unknown")
        post_id = p.get("post_id", "")
        if platform and post_id:
            evidence_links.append(f"{platform}:{post_id}")

    # Deduplicate while preserving order
    seen: Set[str] = set()
    deduped_links: List[str] = []
    for link in evidence_links:
        if link not in seen:
            seen.add(link)
            deduped_links.append(link)

    description = (
        f"Burst detected for #{hashtag}: {len(handles)} distinct accounts posted "
        f"{len(burst['posts'])} times within a {BURST_WINDOW_MINUTES}-minute window "
        f"({burst['window_start'].isoformat()} → {burst['window_end'].isoformat()})."
    )

    # --- Neo4j (best-effort) ---
    try:
        neo = neo4j_client.Neo4jClient()
        if neo.is_available():
            ok = neo.record_coordination_event(
                event_uuid=event_uuid,
                campaign_name=f"hashtag_burst_{hashtag}",
                description=description,
                handles=handles,
                domains=domains,
                hashtags=[hashtag],
                similarity_score=None,
                estimated_accounts=len(handles),
                estimated_payment_usd=0.0,
                evidence_links=deduped_links[:20],
            )
            if ok:
                logger.info("Recorded burst event %s in Neo4j", event_uuid)
            else:
                logger.warning("Neo4j record_coordination_event returned False")
        else:
            logger.info("Neo4j unavailable; skipping graph write for %s", event_uuid)
    except Exception:
        logger.exception("Neo4j write failed for burst %s; continuing with SQLite", event_uuid)

    # --- SQLite ---
    row = {
        "event_uuid": event_uuid,
        "detected_at": _now().isoformat(),
        "campaign_name": f"hashtag_burst_{hashtag}",
        "description": description,
        "involved_handles": json.dumps(handles),
        "involved_domains": json.dumps(domains),
        "hashtag_burst": hashtag,
        "similarity_score": None,
        "estimated_accounts": len(handles),
        "estimated_payment_usd": 0.0,
        "evidence_links": json.dumps(deduped_links[:20]),
        "status": "open",
    }
    try:
        rid = sqlite_store.insert_one("coordination_events", row)
        if rid:
            logger.info("Recorded burst event %s in SQLite row %s", event_uuid, rid)
        else:
            logger.warning("SQLite insert_one returned None for burst %s", event_uuid)
    except Exception:
        logger.exception("SQLite write failed for burst %s", event_uuid)


def run() -> int:
    """Run burst detection for all watchlist hashtags.

    Returns the total number of bursts detected across all hashtags.
    """
    total_bursts = 0
    since = _now() - timedelta(hours=24)

    for hashtag in WATCHLIST_HASHTAGS:
        logger.info("Analysing hashtag #%s since %s", hashtag, since.isoformat())
        posts = _hashtag_posts(hashtag, since)
        if not posts:
            logger.info("No posts found for #%s", hashtag)
            continue

        bursts = _find_bursts(posts, BURST_WINDOW_MINUTES, BURST_THRESHOLD_ACCOUNTS)
        logger.info("Found %d burst(s) for #%s", len(bursts), hashtag)

        for burst in bursts:
            burst["hashtag"] = hashtag  # tag for downstream use
            _record_burst(burst, hashtag)
            total_bursts += 1

    return total_bursts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    count = run()
    print(f"Timing analysis complete: {count} burst(s) detected.")
