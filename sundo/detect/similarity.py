"""Sundo Pi OSINT Monitoring Platform — Content similarity detector.

Uses ``datasketch`` MinHash + LSH to find near-duplicate social posts
posted by distinct accounts within a configurable look-back window.
If three or more distinct accounts post near-identical content a
``CoordinationEvent`` is created in Neo4j and SQLite.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from datasketch import MinHash, MinHashLSH

from sundo import config as _cfg

NUM_PERM = getattr(_cfg, "NUM_PERM", 128)
SIMILARITY_THRESHOLD = getattr(_cfg, "SIMILARITY_THRESHOLD", 0.70)
LOOKBACK_HOURS = getattr(_cfg, "LOOKBACK_HOURS", 48)

from sundo.db import neo4j_client, sqlite_store

logger = logging.getLogger("sundo.detect.similarity")

# Regex helpers
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_NONWORD_RE = re.compile(r"[^\w\s]+")


def _now() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def _clean_text(text: Optional[str]) -> List[str]:
    """Tokenise *text* into a list of lower-case word tokens.

    Steps:
        1. Strip URLs.
        2. Strip @mentions.
        3. Lower-case.
    4. Remove non-word characters.
        5. Split on whitespace.
    """
    if not text:
        return []
    text = _URL_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    text = text.lower()
    text = _NONWORD_RE.sub(" ", text)
    return [t for t in text.split() if t]


def _make_minhash(tokens: List[str]) -> MinHash:
    """Create a MinHash signature from *tokens*."""
    m = MinHash(num_perm=NUM_PERM)
    for token in tokens:
        m.update(token.encode("utf-8"))
    return m


def _fetch_recent_posts(since: datetime) -> List[Dict[str, Any]]:
    """Return all social_posts rows with ``posted_at >= since``."""
    since_iso = since.isoformat()
    conn = sqlite_store.get_connection()
    try:
        cursor = conn.execute(
            """
            SELECT platform, post_id, author_handle, content, posted_at,
                   hashtags, mentions, urls, raw_json
            FROM social_posts
            WHERE posted_at >= ?
            ORDER BY posted_at ASC
            """,
            (since_iso,),
        )
        return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logger.exception("Failed to fetch recent posts")
        return []
    finally:
        conn.close()


def _group_by_similarity(
    posts: List[Dict[str, Any]],
    threshold: float,
) -> List[List[Dict[str, Any]]]:
    """Group *posts* into similarity clusters using MinHash LSH.

    Returns a list of clusters, where each cluster contains posts whose
    content similarity is >= *threshold*.
    """
    if not posts:
        return []

    lsh = MinHashLSH(threshold=threshold, num_perm=NUM_PERM)
    hashes: Dict[str, MinHash] = {}

    # Insert every post into LSH
    for p in posts:
        key = f"{p.get('platform','unk')}:{p.get('post_id','unk')}"
        tokens = _clean_text(p.get("content"))
        if not tokens:
            continue
        mh = _make_minhash(tokens)
        hashes[key] = mh
        lsh.insert(key, mh)

    # Build clusters via union-find on near-duplicate keys
    parent: Dict[str, str] = {k: k for k in hashes}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for key, mh in hashes.items():
        # Query LSH for neighbours (excludes self by default)
        for neighbour in lsh.query(mh):
            if neighbour != key:
                _union(key, neighbour)

    # Gather clusters
    clusters: Dict[str, List[Dict[str, Any]]] = {}
    key_to_post: Dict[str, Dict[str, Any]] = {}
    for p in posts:
        key = f"{p.get('platform','unk')}:{p.get('post_id','unk')}"
        key_to_post[key] = p

    for key in hashes:
        root = _find(key)
        clusters.setdefault(root, []).append(key_to_post[key])

    return [c for c in clusters.values() if len(c) > 1]


def _record_similarity_event(
    cluster: List[Dict[str, Any]],
    max_similarity: float,
) -> None:
    """Persist a content-similarity coordination event to Neo4j and SQLite."""
    event_uuid = str(uuid4())
    handles = sorted({p["author_handle"] for p in cluster if p.get("author_handle")})
    if len(handles) < 2:
        return  # need at least 2 distinct accounts for a pair event

    # Extract evidence
    evidence_links: List[str] = []
    for p in cluster:
        platform = p.get("platform", "unknown")
        post_id = p.get("post_id", "")
        if platform and post_id:
            evidence_links.append(f"{platform}:{post_id}")
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

    seen: Set[str] = set()
    deduped: List[str] = []
    for link in evidence_links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)

    description = (
        f"Content similarity cluster: {len(cluster)} posts from {len(handles)} "
        f"distinct accounts (max similarity {max_similarity:.2f})."
    )

    # --- Neo4j (best-effort) ---
    try:
        neo = neo4j_client.Neo4jClient()
        if neo.is_available():
            ok = neo.record_coordination_event(
                event_uuid=event_uuid,
                campaign_name="content_similarity_cluster",
                description=description,
                handles=handles,
                domains=[],
                hashtags=[],
                similarity_score=max_similarity,
                estimated_accounts=len(handles),
                estimated_payment_usd=0.0,
                evidence_links=deduped[:20],
            )
            if ok:
                logger.info("Recorded similarity event %s in Neo4j", event_uuid)
        else:
            logger.info("Neo4j unavailable; skipping graph write for %s", event_uuid)
    except Exception:
        logger.exception("Neo4j write failed for similarity %s; continuing", event_uuid)

    # --- SQLite ---
    row = {
        "event_uuid": event_uuid,
        "detected_at": _now().isoformat(),
        "campaign_name": "content_similarity_cluster",
        "description": description,
        "involved_handles": json.dumps(handles),
        "involved_domains": json.dumps([]),
        "hashtag_burst": None,
        "similarity_score": max_similarity,
        "estimated_accounts": len(handles),
        "estimated_payment_usd": 0.0,
        "evidence_links": json.dumps(deduped[:20]),
        "status": "open",
    }
    try:
        rid = sqlite_store.insert_one("coordination_events", row)
        if rid:
            logger.info("Recorded similarity event %s in SQLite row %s", event_uuid, rid)
    except Exception:
        logger.exception("SQLite write failed for similarity %s", event_uuid)


def run(batch_size: int = 500) -> Tuple[int, int]:
    """Run content-similarity detection.

    Args:
        batch_size: Maximum number of new (unprocessed) posts to examine per run.

    Returns:
        ``(clusters_found, pair_events_recorded)`` — total clusters and how
        many resulted in recorded coordination events.
    """
    since = _now() - timedelta(hours=LOOKBACK_HOURS)
    posts = _fetch_recent_posts(since)
    if not posts:
        logger.info("No posts in the last %d hours", LOOKBACK_HOURS)
        return 0, 0

    logger.info("Fetched %d posts for similarity analysis", len(posts))

    clusters = _group_by_similarity(posts, SIMILARITY_THRESHOLD)
    logger.info("Found %d similarity cluster(s)", len(clusters))

    events_recorded = 0
    for cluster in clusters:
        # We need 3+ distinct accounts for a coordination event,
        # but we also record pairs for observability.
        distinct_handles = {p["author_handle"] for p in cluster if p.get("author_handle")}
        if len(distinct_handles) >= 3:
            _record_similarity_event(cluster, SIMILARITY_THRESHOLD)
            events_recorded += 1
        elif len(distinct_handles) == 2:
            # Log pair but do not create a CoordinationEvent
            h1, h2 = sorted(distinct_handles)
            logger.info(
                "Similarity pair detected (below coordination threshold): %s ↔ %s "
                "(%d posts)",
                h1,
                h2,
                len(cluster),
            )

    return len(clusters), events_recorded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    clusters, events = run()
    print(f"Similarity analysis complete: {clusters} cluster(s), {events} event(s) recorded.")
