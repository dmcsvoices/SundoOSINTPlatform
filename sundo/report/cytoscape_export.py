"""Export Neo4j + SQLite graph to Cytoscape.js JSON.

Merges live Neo4j nodes (Person, PalestinianVoice, Organization) with
SQLite rss_articles as Article + Source nodes, producing the full
visual graph for the dashboard.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

from sundo.config import (
    DASHBOARD_STATIC,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
    AMPLIFY_FEEDS,
    MONITOR_FEEDS,
)
from sundo.db.sqlite_store import get_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (must be defined before module-level usage)
# ---------------------------------------------------------------------------

def _short_id(name: str) -> str:
    """Generate a short stable ID from a source name."""
    name_lower = name.lower()
    # Hardcode known short IDs to match original demo graph
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
    if "electronic intifada" in name_lower or "intifada" in name_lower:
        return "ei"
    if "wafa" in name_lower:
        return "wafa"
    if "haaretz" in name_lower:
        return "haaretz"
    if "drop site" in name_lower:
        return "dropsite"
    # Fallback: first letters of each word
    return "".join(w[0] for w in name.split() if w).lower()[:8]


def _article_id(link: str) -> str:
    """Generate a stable article node ID from its URL."""
    h = hashlib.md5(link.encode()).hexdigest()[:8]
    # Try to extract domain
    domain = "article"
    if "://" in link:
        domain_part = link.split("://", 1)[1].split("/", 1)[0]
        domain = domain_part.replace("www.", "").replace(".", "_")
    return f"article_{domain}__{h}"


# ---------------------------------------------------------------------------
# Feed → Source mapping (normalised URLs)
# ---------------------------------------------------------------------------
_FEED_SOURCE_MAP: dict[str, tuple[str, str, float]] = {}
"""feed_url -> (source_id, source_name, credibility_score)"""


def _norm_url(url: str) -> str:
    """Normalise URL for matching: strip query params and trailing slash."""
    url = url.split("?")[0]
    return url.rstrip("/")


for _name, _url in AMPLIFY_FEEDS:
    _short = _short_id(_name)
    _FEED_SOURCE_MAP[_norm_url(_url)] = (_short, _name, 0.8)
for _name, _url in MONITOR_FEEDS:
    _short = _short_id(_name)
    _FEED_SOURCE_MAP[_norm_url(_url)] = (_short, _name, 0.5)


# ---------------------------------------------------------------------------
# Neo4j
# ---------------------------------------------------------------------------

def _neo4j_nodes() -> list[dict[str, Any]]:
    if GraphDatabase is None:
        return []
    cypher = (
        "MATCH (n) RETURN id(n) AS nid, labels(n)[0] AS type, "
        "n.id AS id, n.name AS name, n.handle AS handle, "
        "n.credibility_score AS credibility_score, n.fara_linked AS fara_linked"
    )
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(cypher)
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Neo4j node query failed: %s", exc)
        return []


def _neo4j_edges() -> list[dict[str, Any]]:
    if GraphDatabase is None:
        return []
    cypher = (
        "MATCH (a)-[r]->(b) "
        "RETURN id(a) AS source_id, id(b) AS target_id, "
        "type(r) AS relationship, r.amount AS amount, "
        "r.published_at AS published_at, r.source_name AS source_name, "
        "r.article_count AS article_count"
    )
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(cypher)
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Neo4j edge query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# SQLite → Article / Source nodes
# ---------------------------------------------------------------------------

def _sqlite_articles() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (source_nodes, article_nodes, edges) from SQLite rss_articles."""
    try:
        conn = get_connection()
        cursor = conn.execute(
            "SELECT title, link, feed_url, source_type, published_at, authors, tags "
            "FROM rss_articles ORDER BY published_at DESC"
        )
        rows = cursor.fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("SQLite rss_articles query failed: %s", exc)
        return [], [], []

    # Deduplicate sources
    seen_sources: set[str] = set()
    source_nodes: list[dict[str, Any]] = []
    article_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for row in rows:
        feed_url = row["feed_url"]
        link = row["link"]
        title = row["title"] or "Untitled"
        source_type = row["source_type"] or "unknown"

        # Resolve source info (normalised URL)
        norm = _norm_url(feed_url)
        if norm in _FEED_SOURCE_MAP:
            src_id, src_name, cred = _FEED_SOURCE_MAP[norm]
        else:
            src_id = _short_id(feed_url)
            src_name = feed_url
            cred = 0.8 if source_type == "amplify" else 0.5

        if src_id not in seen_sources:
            seen_sources.add(src_id)
            source_nodes.append(
                {
                    "data": {
                        "id": src_id,
                        "label": src_name,
                        "type": "Source",
                        "source_type": source_type,
                        "credibility_score": cred,
                        "fara_linked": False,
                        "feed_url": feed_url,
                    }
                }
            )

        art_id = _article_id(link)
        article_nodes.append(
            {
                "data": {
                    "id": art_id,
                    "label": title[:80] + ("..." if len(title) > 80 else ""),
                    "type": "Article",
                    "fara_linked": False,
                    "article_link": link,
                    "published_at": row["published_at"],
                    "authors": row["authors"],
                    "tags": row["tags"],
                }
            }
        )

        edges.append(
            {
                "data": {
                    "source": src_id,
                    "target": art_id,
                    "relationship": "PUBLISHED",
                }
            }
        )

    return source_nodes, article_nodes, edges


def export_author_nodes() -> tuple[list, list]:
    """
    Export Author nodes and their WROTE / WRITES_FOR / IS_VOICE edges.

    Returns:
        (nodes, edges) — lists of Cytoscape.js dicts
    """
    if GraphDatabase is None:
        logger.warning("Neo4j GraphDatabase unavailable; falling back to SQLite for Author nodes.")
        return _export_author_nodes_sqlite()

    nodes = []
    edges = []

    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:

            # Author nodes
            result = session.run("""
                MATCH (a:Author)
                RETURN a.id AS id,
                       a.display_name AS display_name,
                       a.handle AS handle,
                       a.article_count AS article_count,
                       a.primary_language AS primary_language,
                       a.linked_voice_id AS linked_voice_id,
                       a.verification_status AS verification_status
            """)
            for r in result:
                nodes.append({
                    'data': {
                        'id': r['id'],
                        'label': r['display_name'],
                        'type': 'Author',
                        'handle': r['handle'],
                        'article_count': r['article_count'] or 0,
                        'primary_language': r['primary_language'] or 'en',
                        'linked_voice_id': r['linked_voice_id'],
                        'verification_status': r['verification_status'],
                        'size': min(10 + (r['article_count'] or 0) * 2, 40),
                    }
                })

            # WROTE edges (Author → Article)
            result = session.run("""
                MATCH (a:Author)-[r:WROTE]->(art:Article)
                RETURN a.id AS author_id, art.id AS article_id,
                       r.published_at AS published_at
            """)
            for r in result:
                edges.append({
                    'data': {
                        'id': f"wrote-{r['author_id']}-{r['article_id']}",
                        'source': r['author_id'],
                        'target': r['article_id'],
                        'relationship': 'WROTE',
                        'published_at': r['published_at'],
                    }
                })

            # WRITES_FOR edges (Author → Organization)
            result = session.run("""
                MATCH (a:Author)-[r:WRITES_FOR]->(o:Organization)
                RETURN a.id AS author_id, o.id AS org_id,
                       r.article_count AS article_count
            """)
            for r in result:
                edges.append({
                    'data': {
                        'id': f"writefor-{r['author_id']}-{r['org_id']}",
                        'source': r['author_id'],
                        'target': r['org_id'],
                        'relationship': 'WRITES_FOR',
                        'article_count': r['article_count'],
                    }
                })

            # IS_VOICE edges (Author → PalestinianVoice)
            result = session.run("""
                MATCH (a:Author)-[:IS_VOICE]->(v:PalestinianVoice)
                RETURN a.id AS author_id, v.handle AS voice_handle
            """)
            for r in result:
                edges.append({
                    'data': {
                        'id': f"isvoice-{r['author_id']}-{r['voice_handle']}",
                        'source': r['author_id'],
                        'target': r['voice_handle'],
                        'relationship': 'IS_VOICE',
                    }
                })

        driver.close()
        
        # If Neo4j returned no Author nodes, fallback to SQLite
        if not nodes:
            logger.info("Neo4j has no Author nodes; falling back to SQLite")
            return _export_author_nodes_sqlite()
            
    except Exception as exc:
        logger.warning("Author node export from Neo4j failed: %s", exc)
        return _export_author_nodes_sqlite()

    return nodes, edges


def _export_author_nodes_sqlite() -> tuple[list, list]:
    """Fallback: Export Author nodes directly from SQLite (no Neo4j needed)."""
    import urllib.parse

    nodes = []
    edges = []

    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Author nodes from SQLite
        cursor.execute("""
            SELECT id, display_name, handle, article_count, first_seen, last_seen
            FROM authors
            ORDER BY article_count DESC
        """)
        for row in cursor.fetchall():
            aid = row['id']
            display_name = row['display_name'] or aid
            article_count = row['article_count'] or 0
            nodes.append({
                'data': {
                    'id': aid,
                    'label': display_name,
                    'type': 'Author',
                    'handle': row['handle'] or aid,
                    'article_count': article_count,
                    'primary_language': 'en',
                    'linked_voice_id': None,
                    'verification_status': 'pending',
                    'size': min(10 + article_count * 2, 40),
                }
            })

        # WRITES_FOR edges (Author → Organization via feed_url match)
        # Need to match the same source_id format used by _sqlite_articles
        cursor.execute("""
            SELECT DISTINCT a.id AS author_id, r.feed_url
            FROM authors a
            JOIN rss_articles r ON r.author_id = a.id
            WHERE r.feed_url IS NOT NULL
        """)
        for row in cursor.fetchall():
            feed_url = row['feed_url']
            # Use the same normalization logic as _sqlite_articles
            norm = _norm_url(feed_url)
            if norm in _FEED_SOURCE_MAP:
                org_id = _FEED_SOURCE_MAP[norm][0]
            else:
                org_id = _short_id(feed_url)
            
            if org_id:
                edges.append({
                    'data': {
                        'id': f"writefor-{row['author_id']}-{org_id}",
                        'source': row['author_id'],
                        'target': org_id,
                        'relationship': 'WRITES_FOR',
                    }
                })

        # WROTE edges (Author → Article)
        cursor.execute("""
            SELECT DISTINCT a.id AS author_id, r.link AS article_link
            FROM authors a
            JOIN rss_articles r ON r.author_id = a.id
        """)
        for row in cursor.fetchall():
            art_id = _article_id(row['article_link'])
            edges.append({
                'data': {
                    'id': f"wrote-{row['author_id']}-{art_id}",
                    'source': row['author_id'],
                    'target': art_id,
                    'relationship': 'WROTE',
                }
            })

        conn.close()
        logger.info("SQLite fallback: exported %d Author nodes, %d edges", len(nodes), len(edges))
    except Exception as exc:
        logger.warning("SQLite fallback for Author nodes also failed: %s", exc)

    return nodes, edges


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export_graph() -> Path:
    """Export merged Neo4j + SQLite graph as Cytoscape.js JSON."""
    DASHBOARD_STATIC.mkdir(parents=True, exist_ok=True)
    out_path = DASHBOARD_STATIC / "graph.json"

    # Neo4j nodes (Person, PalestinianVoice, Organization, Author, etc.)
    raw_nodes = _neo4j_nodes()
    # Neo4j edges (PARTICIPATES_IN, FUNDED_BY, WROTE, WRITES_FOR, IS_VOICE, etc.)
    raw_edges = _neo4j_edges()

    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # --- Neo4j nodes ---
    for n in raw_nodes:
        nid = str(n.get("nid", ""))
        # Prefer logical ID (id, handle, ein) over internal Neo4j ID
        logical_id = n.get("id") or n.get("handle") or n.get("ein") or nid
        if not logical_id or logical_id in seen_ids:
            continue
        seen_ids.add(logical_id)
        label = n.get("name") or n.get("handle") or n.get("id") or nid
        nodes.append(
            {
                "data": {
                    "id": logical_id,
                    "label": label,
                    "type": n.get("type", "Unknown"),
                    "credibility_score": n.get("credibility_score"),
                    "fara_linked": bool(n.get("fara_linked", False)),
                }
            }
        )

    # --- Author nodes + edges from Neo4j ---
    author_nodes, author_edges = export_author_nodes()
    for an in author_nodes:
        aid = an["data"]["id"]
        if aid not in seen_ids:
            seen_ids.add(aid)
            nodes.append(an)

    # --- SQLite sources + articles ---
    source_nodes, article_nodes, article_edges = _sqlite_articles()
    for sn in source_nodes:
        sid = sn["data"]["id"]
        if sid not in seen_ids:
            seen_ids.add(sid)
            nodes.append(sn)
    for an in article_nodes:
        aid = an["data"]["id"]
        if aid not in seen_ids:
            seen_ids.add(aid)
            nodes.append(an)

    # --- Edges ---
    edges: list[dict[str, Any]] = []
    seen_edge_ids: set[str] = set()
    valid_node_ids = seen_ids  # All node IDs that exist in the graph

    # Neo4j edges
    for e in raw_edges:
        src = str(e.get("source_id", ""))
        tgt = str(e.get("target_id", ""))
        rel = e.get("relationship", "RELATED")
        eid = f"{src}-{rel}-{tgt}"
        if eid not in seen_edge_ids and src in valid_node_ids and tgt in valid_node_ids:
            seen_edge_ids.add(eid)
            edges.append(
                {
                    "data": {
                        "source": src,
                        "target": tgt,
                        "relationship": rel,
                        "amount": e.get("amount"),
                    }
                }
            )

    # Author edges
    for e in author_edges:
        src = e["data"]["source"]
        tgt = e["data"]["target"]
        rel = e["data"]["relationship"]
        eid = f"{src}-{rel}-{tgt}"
        if eid not in seen_edge_ids and src in valid_node_ids and tgt in valid_node_ids:
            seen_edge_ids.add(eid)
            edges.append(e)

    # Article edges
    for e in article_edges:
        src = e["data"]["source"]
        tgt = e["data"]["target"]
        rel = e["data"]["relationship"]
        eid = f"{src}-{rel}-{tgt}"
        if eid not in seen_edge_ids and src in valid_node_ids and tgt in valid_node_ids:
            seen_edge_ids.add(eid)
            edges.append(e)

    graph = {"nodes": nodes, "edges": edges}
    out_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    logger.info(
        "Graph exported: %s (%d nodes, %d edges) — Neo4j:%d + SQLite:%d sources + %d articles + Author:%d",
        out_path,
        len(nodes),
        len(edges),
        len(raw_nodes),
        len(source_nodes),
        len(article_nodes),
        len(author_nodes),
    )
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    export_graph()
