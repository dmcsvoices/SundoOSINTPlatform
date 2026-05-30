from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from sqlite3 import OperationalError

import os

from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from neo4j import GraphDatabase, basic_auth
from neo4j.exceptions import ServiceUnavailable, AuthError

from sundo.config import (
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DASHBOARD_STATIC,
    NEO4J_URI,
    NEO4J_USER,
    NEO4J_PASSWORD,
    SQLITE_PATH,
    OUTPUT_DIR,
    AMPLIFY_FEEDS,
    MONITOR_FEEDS,
)
from sundo.db.sqlite_store import get_connection

logger = logging.getLogger(__name__)

# Ensure errors are also logged to sundo_errors.log
_errors_file_handler = None
if not any(isinstance(h, logging.FileHandler) and h.baseFilename.endswith("sundo_errors.log") for h in logger.handlers):
    _errors_file_handler = logging.FileHandler("sundo_errors.log", mode="a")
    _errors_file_handler.setLevel(logging.WARNING)
    _errors_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(_errors_file_handler)

# Non-secret Flask config (secret key is in .env)
app = Flask(__name__)

# Load secret key from env (falls back to a generated one if missing, but prefer env)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

# Admin credentials from env (hashed at boot so plaintext is not stored in memory)
_ADMIN_USER = os.getenv("DASHBOARD_USER", "admin")
_admin_plain = os.getenv("DASHBOARD_PASSWORD", "")
_ADMIN_PASS_HASH = generate_password_hash(_admin_plain) if _admin_plain else generate_password_hash("sundopi")

# If no password configured, log a loud warning
if not _admin_plain:
    logger.warning("DASHBOARD_PASSWORD not set in .env — using default 'sundopi'. CHANGE THIS!")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_graph() -> dict:
    graph_path = DASHBOARD_STATIC / "graph.json"
    if graph_path.exists():
        try:
            return json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse graph.json: %s", exc)
    return {"nodes": [], "edges": []}


def _neo4j_session():
    """Return a Neo4j session context manager, or None if unavailable."""
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
        return driver.session()
    except (ServiceUnavailable, AuthError, Exception):
        return None


def _neo4j_run(query: str, parameters: dict | None = None):
    """Execute a Neo4j read query and return records as list of dicts, or None on failure."""
    session = _neo4j_session()
    if session is None:
        return None
    try:
        with session:
            result = session.run(query, parameters or {})
            return [dict(r) for r in result]
    except Exception:
        logger.exception("Neo4j query failed: %s", query[:80])
        return None


def _sqlite_run(sql: str, params: tuple = ()):
    try:
        conn = get_connection()
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except OperationalError:
        logger.exception("SQLite operational error: %s", sql[:80])
        return None
    except Exception:
        logger.exception("SQLite error: %s", sql[:80])
        return None


def _feed_url_to_name(feed_url: str) -> str:
    """Map a feed URL to a human-readable source name."""
    # Quick exact-match heuristics
    mapping = {
        "https://www.wafa.ps/rss.aspx": "Wafa News Agency",
        "https://www.972mag.com/feed/": "+972 Magazine",
        "https://mondoweiss.net/feed/": "Mondoweiss",
        "https://www.middleeasteye.net/rss": "Middle East Eye",
        "https://www.dropsitenews.com/feed": "Drop Site News",
        "https://www.alquds.com/feed/": "Al-Quds",
        "https://electronicintifada.net/rss.xml": "Electronic Intifada",
        "https://www.haaretz.com/srv/haaretz-articles.rss": "Haaretz English",
        "https://theintercept.com/feed/?rss": "The Intercept",
        "https://forward.com/feed/": "The Forward",
        "https://www.jta.org/feed": "Jewish Telegraphic Agency",
    }
    # Try exact match first
    name = mapping.get(feed_url)
    if name:
        return name
    # Normalise and try again (strip query params, trailing slash)
    norm = feed_url.split("?")[0].rstrip("/")
    for url, name in mapping.items():
        if url.split("?")[0].rstrip("/") == norm:
            return name
    return feed_url


def _article_id(link: str) -> str:
    """Generate the same article node id used in cytoscape_export."""
    if not link:
        return "article_unknown"
    h = hashlib.md5(link.encode()).hexdigest()[:8]
    # Try to extract domain
    domain = "article"
    if "://" in link:
        domain_part = link.split("://", 1)[1].split("/", 1)[0]
        domain = domain_part.replace("www.", "").replace(".", "_")
    return f"article_{domain}__{h}"


def _short_id(name: str) -> str:
    """Generate a short stable ID from a source name (must match cytoscape_export)."""
    name_lower = name.lower()
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
    return "".join(w[0] for w in name.split() if w).lower()[:8]


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

@app.before_request
def require_login():
    """Redirect unauthenticated users to /login (except for public routes and static assets).
    
    API routes return 401 Unauthorized JSON instead of a redirect.
    """
    if request.endpoint in ("login", "static", "index"):
        return None
    if not session.get("logged_in"):
        # API routes should return 401 JSON, not an HTML redirect
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login() -> str:
    """Render login form or validate credentials."""
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username == _ADMIN_USER and check_password_hash(_ADMIN_PASS_HASH, password):
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout() -> str:
    """Clear session and redirect to login."""
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Stats endpoint for legend counts
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    """Return article and node counts for the legend."""
    try:
        # Count node types from graph.json (fast, no DB needed)
        graph = _load_graph()
        node_counts = {}
        for n in graph.get("nodes", []):
            t = n.get("data", {}).get("type", "Unknown")
            node_counts[t] = node_counts.get(t, 0) + 1

        # Article count must come from the graph so it matches the legend
        article_count = node_counts.get("Article", 0)

        return jsonify({
            "article_count": article_count,
            "node_counts": node_counts,
            "total_nodes": len(graph.get("nodes", [])),
            "total_edges": len(graph.get("edges", [])),
        }), 200
    except Exception as exc:
        logger.error("Error in /api/stats: %s", exc)
        return jsonify({"error": "Stats unavailable"}), 503


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@app.route("/api/search")
def api_search():
    """Search across SQLite tables for authors, articles, and sources.

    Query params:
      q     — search term (required)
      scope — filter: all | authors | sources | articles (default: all)
    """
    query = request.args.get("q", "").strip()
    scope = request.args.get("scope", "all").strip().lower()

    if not query or len(query) < 2:
        return jsonify({"results": [], "query": query, "scope": scope}), 200

    results: list[dict] = []
    search_pattern = f"%{query}%"

    # --- Authors ---
    if scope in ("all", "authors"):
        rows = _sqlite_run(
            """
            SELECT id, display_name, handle, byline_variants, article_count
            FROM authors
            WHERE display_name LIKE ? OR handle LIKE ? OR byline_variants LIKE ?
            ORDER BY article_count DESC
            LIMIT 20
            """,
            (search_pattern, search_pattern, search_pattern),
        )
        if rows:
            for r in rows:
                author_id = r.get("id", "")
                display_name = r.get("display_name") or r.get("handle") or author_id
                results.append({
                    "type": "Author",
                    "id": author_id,
                    "title": display_name,
                    "subtitle": f"@{r.get('handle', '')}" if r.get("handle") else "Author",
                    "url": None,
                })

    # --- Articles ---
    if scope in ("all", "articles"):
        rows = _sqlite_run(
            """
            SELECT title, link, feed_url, published_at, authors, tags
            FROM rss_articles
            WHERE title LIKE ? OR authors LIKE ? OR tags LIKE ?
            ORDER BY published_at DESC
            LIMIT 20
            """,
            (search_pattern, search_pattern, search_pattern),
        )
        if rows:
            for r in rows:
                article_id = _article_id(r.get("link", ""))
                source_name = _feed_url_to_name(r.get("feed_url", ""))
                pub_date = r.get("published_at", "") or ""
                results.append({
                    "type": "Article",
                    "id": article_id,
                    "title": r.get("title", "Untitled"),
                    "subtitle": f"{source_name} · {pub_date[:10]}",
                    "url": r.get("link"),
                })

    # --- Sources ---
    if scope in ("all", "sources"):
        query_lower = query.lower()
        for name, url in AMPLIFY_FEEDS + MONITOR_FEEDS:
            if query_lower in name.lower() or query_lower in url.lower():
                src_id = _short_id(name)
                source_type = "amplify" if (name, url) in AMPLIFY_FEEDS else "monitor"
                results.append({
                    "type": "Source",
                    "id": src_id,
                    "title": name,
                    "subtitle": f"{source_type.title()} feed",
                    "url": url,
                })

    # De-duplicate by id (in case of overlap)
    seen_ids: set[str] = set()
    deduped: list[dict] = []
    for r in results:
        key = f"{r['type']}:{r['id']}"
        if key not in seen_ids:
            seen_ids.add(key)
            deduped.append(r)

    return jsonify({"results": deduped, "query": query, "scope": scope}), 200


# ---------------------------------------------------------------------------
# Existing routes
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    """Render the Cytoscape.js network view."""
    graph = _load_graph()
    return render_template("index.html", graph_json=json.dumps(graph))


@app.route("/api/graph")
def api_graph() -> tuple[str, int, dict[str, str]]:
    """Serve graph.json as JSON API."""
    graph = _load_graph()
    return (
        json.dumps(graph),
        200,
        {"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# NEW: Node detail endpoints
# ---------------------------------------------------------------------------

@app.route("/api/node/person/<handle>")
def api_node_person(handle: str):
    """Return full detail for a Person node."""
    try:
        # Neo4j: base person data
        result = _neo4j_run(
            "MATCH (p:Person {handle: $handle}) RETURN p",
            {"handle": handle},
        )
        person_data = {}
        if result:
            record = result[0]
            if record:
                node = record["p"]
                person_data = {
                    "type": "Person",
                    "handle": node.get("handle", handle),
                    "platform": node.get("platform", "unknown"),
                    "follower_count": node.get("follower_count", 0) or 0,
                    "verified": bool(node.get("verified", False)),
                    "fara_linked": bool(node.get("fara_linked", False)),
                    "credibility_score": node.get("credibility_score", 0.5) or 0.5,
                    "first_seen": node.get("first_seen"),
                    "last_seen": node.get("last_seen"),
                }

        # Neo4j: funding relationships
        funding = []
        result = _neo4j_run(
            """
            MATCH (o:Organization)-[f:FUNDED]->(p:Person {handle: $handle})
            RETURN o.name AS org_name, f.amount_usd AS amount_usd, f.period AS period,
                   f.filing_source AS filing_source, f.evidence_url AS evidence_url
            """,
            {"handle": handle},
        )
        if result:
            for record in result:
                funding.append({
                    "org_name": record.get("org_name", "Unknown"),
                    "amount_usd": record.get("amount_usd"),
                    "period": record.get("period"),
                    "filing_source": record.get("filing_source", "unknown"),
                    "evidence_url": record.get("evidence_url"),
                })

        # Neo4j: coordination events
        coordination_events = []
        result = _neo4j_run(
            """
            MATCH (p:Person {handle: $handle})-[:PARTICIPATED_IN]->(c:CoordinationEvent)
            RETURN c ORDER BY c.detected_at DESC LIMIT 10
            """,
            {"handle": handle},
        )
        if result:
            for record in result:
                node = record["c"]
                coordination_events.append({
                    "event_id": node.get("event_id", ""),
                    "detected_at": node.get("detected_at"),
                    "trigger_type": node.get("trigger_type", "unknown"),
                    "account_count": node.get("estimated_accounts", 0) or 0,
                    "hashtags": node.get("hashtags", []) or [],
                    "similarity_score": node.get("similarity_score"),
                })

        # SQLite: ftc_violations (match respondent or case_name)
        ftc_violations = []
        rows = _sqlite_run(
            "SELECT * FROM ftc_violations WHERE respondent = ? ORDER BY ingested_at DESC",
            (handle,),
        )
        if rows is not None:
            for r in rows:
                ftc_violations.append({
                    "post_url": r.get("press_release_url", ""),
                    "payment_source_url": r.get("press_release_url", ""),
                    "amount_usd": r.get("penalty_usd", 0) or 0,
                    "flagged_at": r.get("final_order_date"),
                    "package_path": r.get("case_id", ""),
                })

        # SQLite: recent_posts
        recent_posts = []
        rows = _sqlite_run(
            "SELECT * FROM social_posts WHERE author_handle = ? ORDER BY posted_at DESC LIMIT 10",
            (handle,),
        )
        if rows is not None:
            for r in rows:
                content = r.get("content", "")
                preview = content[:120] if content else ""
                recent_posts.append({
                    "platform": r.get("platform", "unknown"),
                    "timestamp": r.get("posted_at"),
                    "content_preview": preview,
                    "hashtags": r.get("hashtags", "").split(",") if r.get("hashtags") else [],
                    "disclosed_sponsored": False,  # not stored in current schema
                })

        response = {
            "type": "Person",
            "handle": person_data.get("handle", handle),
            "platform": person_data.get("platform", "unknown"),
            "follower_count": person_data.get("follower_count", 0),
            "verified": person_data.get("verified", False),
            "fara_linked": person_data.get("fara_linked", False),
            "credibility_score": person_data.get("credibility_score", 0.5),
            "first_seen": person_data.get("first_seen"),
            "last_seen": person_data.get("last_seen"),
            "funding": funding,
            "coordination_events": coordination_events,
            "ftc_violations": ftc_violations,
            "recent_posts": recent_posts,
        }
        return jsonify(response), 200

    except Exception as exc:
        logger.error("Error in /api/node/person/%s: %s", handle, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/node/person/%s: %s", handle, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/node/organization/<org_id>")
def api_node_organization(org_id: str):
    """Return full detail for an Organization node."""
    try:
        # Neo4j: base org data
        result = _neo4j_run(
            "MATCH (o:Organization) WHERE o.id = $org_id OR o.ein = $org_id RETURN o",
            {"org_id": org_id},
        )
        org_data = {}
        if result:
            record = result[0]
            if record:
                node = record["o"]
                org_data = {
                    "type": "Organization",
                    "id": org_id,
                    "name": node.get("name", org_id),
                    "ein": node.get("ein", org_id),
                    "fara_registration_id": node.get("fara_registration_id"),
                    "foreign_principal": node.get("foreign_principal"),
                    "country": node.get("country"),
                    "org_type": node.get("org_type", "unknown"),
                    "flags": node.get("flags", []) or [],
                }

        # Neo4j: funded persons
        funded_persons = []
        total_disbursements = 0
        result = _neo4j_run(
            """
            MATCH (o:Organization)-[f:FUNDED]->(p:Person)
            WHERE o.id = $org_id OR o.ein = $org_id
            RETURN p.handle AS handle, p.platform AS platform,
                   p.follower_count AS follower_count, f.amount_usd AS amount_usd,
                   f.period AS period
            ORDER BY f.amount_usd DESC
            """,
            {"org_id": org_id},
        )
        if result:
            for record in result:
                amt = record.get("amount_usd") or 0
                total_disbursements += amt
                funded_persons.append({
                    "handle": record.get("handle", "unknown"),
                    "platform": record.get("platform", "unknown"),
                    "follower_count": record.get("follower_count", 0) or 0,
                    "amount_usd": amt,
                    "period": record.get("period"),
                })

        # SQLite: irs990_orgs
        irs990 = {}
        rows = _sqlite_run(
            "SELECT * FROM irs990_orgs WHERE ein = ? LIMIT 1",
            (org_id,),
        )
        if rows is None:
            return jsonify({"error": "Database unavailable"}), 503
        if not rows:
            # Fallback: try matching by name if org_id is not an EIN
            rows = _sqlite_run(
                "SELECT * FROM irs990_orgs WHERE name LIKE ? LIMIT 1",
                (f"%{org_id}%",),
            )
        for r in rows:
            irs990 = {
                "revenue": r.get("total_revenue", 0) or 0,
                "expenses": 0,  # not stored in current schema
                "total_assets": r.get("total_assets", 0) or 0,
                "program_description": "",  # not stored in current schema
                "tax_year": r.get("tax_year"),
            }

        # SQLite: irs990_grants
        grants = []
        ein_to_use = org_data.get("ein", org_id)
        rows = _sqlite_run(
            "SELECT * FROM irs990_grants WHERE ein = ? ORDER BY amount_usd DESC LIMIT 20",
            (ein_to_use,),
        )
        if rows is not None:
            grants = rows

        response = {
            "type": "Organization",
            "id": org_id,
            "name": org_data.get("name", org_id),
            "ein": org_data.get("ein", org_id),
            "fara_registration_id": org_data.get("fara_registration_id"),
            "foreign_principal": org_data.get("foreign_principal"),
            "country": org_data.get("country"),
            "org_type": org_data.get("org_type", "unknown"),
            "flags": org_data.get("flags", []),
            "irs990": irs990,
            "funded_persons": funded_persons,
            "total_disbursements_tracked": total_disbursements,
            "connected_person_count": len(funded_persons),
            "irs990_grants": grants,
        }
        return jsonify(response), 200

    except Exception as exc:
        logger.error("Error in /api/node/organization/%s: %s", org_id, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/node/organization/%s: %s", org_id, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/node/voice/<handle>")
def api_node_voice(handle: str):
    """Return full detail for a PalestinianVoice node."""
    try:
        # Neo4j: base voice data
        result = _neo4j_run(
            "MATCH (v:PalestinianVoice {handle: $handle}) RETURN v",
            {"handle": handle},
        )
        voice_data = {}
        if result:
            record = result[0]
            if record:
                node = record["v"]
                voice_data = {
                    "type": "PalestinianVoice",
                    "handle": node.get("handle", handle),
                    "platform": node.get("platform", "unknown"),
                    "reach_score": node.get("reach_score", 0.0) or 0.0,
                    "verification_status": node.get("verification_status", "pending"),
                    "content_focus": node.get("content_focus", []) or [],
                    "language": node.get("language"),
                    "last_active": node.get("last_active"),
                    "digest_include": bool(node.get("digest_include", False)),
                }

        # Neo4j: topics (COVERS relationship)
        topics = []
        result = _neo4j_run(
            "MATCH (v:PalestinianVoice {handle: $handle})-[:COVERS]->(t:Topic) RETURN t.name AS name",
            {"handle": handle},
        )
        if result:
            for record in result:
                name = record.get("name")
                if name:
                    topics.append(name)

        # SQLite: recent articles
        recent_articles = []
        rows = _sqlite_run(
            """
            SELECT title, feed_url, link, published_at, authors
            FROM rss_articles
            WHERE feed_url LIKE ? OR authors LIKE ?
            ORDER BY published_at DESC
            LIMIT 10
            """,
            (f"%{handle}%", f"%{handle}%"),
        )
        if rows is None:
            return jsonify({"error": "Database unavailable"}), 503
        for r in rows:
            recent_articles.append({
                "title": r.get("title", ""),
                "source_name": _feed_url_to_name(r.get("feed_url", "")),
                "url": r.get("link", ""),
                "published_at": r.get("published_at"),
            })

        response = {
            "type": "PalestinianVoice",
            "handle": voice_data.get("handle", handle),
            "platform": voice_data.get("platform", "unknown"),
            "reach_score": voice_data.get("reach_score", 0.0),
            "verification_status": voice_data.get("verification_status", "pending"),
            "content_focus": voice_data.get("content_focus", []),
            "language": voice_data.get("language"),
            "last_active": voice_data.get("last_active"),
            "digest_include": voice_data.get("digest_include", False),
            "topics": topics,
            "recent_articles": recent_articles,
        }
        return jsonify(response), 200

    except Exception as exc:
        logger.error("Error in /api/node/voice/%s: %s", handle, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/node/voice/%s: %s", handle, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/node/author/<author_id>")
def api_node_author(author_id: str):
    """Return full Author node detail."""
    try:
        # Try Neo4j first, fallback to SQLite
        result = _neo4j_run(
            "MATCH (a:Author {id: $id}) RETURN a",
            {"id": author_id},
        )
        
        if result:
            record = result[0]
            if record:
                node = record["a"]
                author = {
                    "type": "Author",
                    "id": author_id,
                    "handle": node.get("handle", author_id),
                    "display_name": node.get("display_name", author_id),
                    "primary_language": node.get("primary_language", "en"),
                    "article_count": node.get("article_count", 0) or 0,
                    "first_seen": node.get("first_seen"),
                    "last_seen": node.get("last_seen"),
                    "linked_voice_id": node.get("linked_voice_id"),
                    "verification_status": node.get("verification_status", "unknown"),
                    "byline_variants": node.get("byline_variants", []),
                }
        else:
            # Fallback to SQLite
            rows = _sqlite_run(
                "SELECT id, display_name, handle, article_count, first_seen, last_seen, verification_status, byline_variants FROM authors WHERE id = ?",
                (author_id,),
            )
            if not rows:
                return jsonify({"error": "Author not found"}), 404
            
            import json
            row = rows[0]
            byline_variants = row.get("byline_variants", "[]")
            try:
                byline_variants = json.loads(byline_variants) if byline_variants else []
            except Exception:
                byline_variants = []
            author = {
                "type": "Author",
                "id": author_id,
                "handle": row.get("handle", author_id),
                "display_name": row.get("display_name", author_id),
                "primary_language": "en",
                "article_count": row.get("article_count", 0) or 0,
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
                "linked_voice_id": None,
                "verification_status": row.get("verification_status") or "pending",
                "byline_variants": byline_variants,
            }

        # Neo4j: articles written (with SQLite fallback)
        articles = []
        result = _neo4j_run(
            """
            MATCH (a:Author {id: $id})-[r:WROTE]->(art:Article)
            RETURN art.id AS id, art.title AS title,
                   art.link AS url, r.published_at AS published_at,
                   r.source_name AS source_name
            ORDER BY r.published_at DESC
            LIMIT 10
            """,
            {"id": author_id},
        )
        if result:
            articles = [dict(a) for a in result]
        else:
            # Fallback to SQLite
            rows = _sqlite_run(
                """
                SELECT r.title, r.link, r.feed_url, r.published_at
                FROM rss_articles r
                JOIN authors a ON r.author_id = a.id
                WHERE a.id = ?
                ORDER BY r.published_at DESC
                LIMIT 10
                """,
                (author_id,),
            )
            if rows:
                for r in rows:
                    articles.append({
                        "id": _article_id(r.get("link", "")),
                        "title": r.get("title", ""),
                        "url": r.get("link", ""),
                        "published_at": r.get("published_at"),
                        "source_name": _feed_url_to_name(r.get("feed_url", "")),
                    })
        author["articles"] = articles

        # Neo4j: publications written for (with SQLite fallback)
        pubs = []
        result = _neo4j_run(
            """
            MATCH (a:Author {id: $id})-[r:WRITES_FOR]->(o:Organization)
            RETURN o.name AS name, o.id AS org_id,
                   r.article_count AS article_count
            ORDER BY r.article_count DESC
            """,
            {"id": author_id},
        )
        if result:
            pubs = [dict(p) for p in result]
        else:
            # Fallback to SQLite - get distinct sources this author writes for
            rows = _sqlite_run(
                """
                SELECT DISTINCT r.feed_url, COUNT(*) as article_count
                FROM rss_articles r
                JOIN authors a ON r.author_id = a.id
                WHERE a.id = ?
                GROUP BY r.feed_url
                """,
                (author_id,),
            )
            if rows:
                for r in rows:
                    feed_url = r.get("feed_url", "")
                    norm = feed_url.split("?")[0].rstrip("/") if feed_url else ""
                    # Map feed_url to source name
                    source_name = _feed_url_to_name(feed_url)
                    pubs.append({
                        "name": source_name,
                        "org_id": norm,
                        "article_count": r.get("article_count", 0),
                    })
        author["publications"] = pubs

        # Neo4j: linked PalestinianVoice if matched (no SQLite fallback for this)
        voice = _neo4j_run(
            """
            MATCH (a:Author {id: $id})-[:IS_VOICE]->(v:PalestinianVoice)
            RETURN v.handle AS handle, v.reach_score AS reach_score,
                   v.verification_status AS verification_status
            """,
            {"id": author_id},
        )
        if voice:
            author["linked_voice"] = dict(voice[0])
        else:
            author["linked_voice"] = None

        return jsonify(author), 200

    except Exception as exc:
        logger.error("Error in /api/node/author/%s: %s", author_id, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/node/author/%s: %s", author_id, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/node/source/<source_id>")
def api_node_source(source_id: str):
    """Return all articles for a Source node."""
    logger.info("Source endpoint called: %s", source_id)
    try:
        feed_url = request.args.get("feed_url", "")
        logger.info("Feed URL param: %s", feed_url)
        if not feed_url:
            # Try to resolve from known mappings
            for name, url in AMPLIFY_FEEDS + MONITOR_FEEDS:
                short = "".join(w[0] for w in name.split() if w).lower()[:8]
                if short == source_id:
                    feed_url = url
                    break

        if not feed_url:
            return jsonify({"error": "Unknown source"}), 404

        articles = []
        try:
            norm_feed_url = feed_url.split("?")[0].rstrip("/")
            rows = _sqlite_run(
                "SELECT title, link, feed_url, source_type, published_at, authors, tags "
                "FROM rss_articles WHERE feed_url = ? OR feed_url = ? OR feed_url = ? "
                "ORDER BY published_at DESC LIMIT 50",
                (feed_url, feed_url.rstrip("/"), norm_feed_url),
            )
            logger.info("SQLite rows result: %s", rows)
        except Exception as exc:
            logger.error("SQLite query failed in source endpoint: %s", exc)
            return jsonify({"error": "Database query failed"}), 503

        if rows is None:
            return jsonify({"error": "Database unavailable"}), 503

        for r in rows:
            articles.append({
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "source_name": _feed_url_to_name(r.get("feed_url", "")),
                "published_at": r.get("published_at"),
                "authors": r.get("authors"),
                "tags": r.get("tags"),
                "article_id": _article_id(r.get("link", "")),
            })

        response = {
            "type": "Source",
            "id": source_id,
            "source_name": _feed_url_to_name(feed_url),
            "source_type": "amplify" if any(feed_url == u for _, u in AMPLIFY_FEEDS) else "monitor",
            "source_category": "amplify" if any(feed_url == u for _, u in AMPLIFY_FEEDS) else "monitor",
            "feed_url": feed_url,
            "article_count": len(articles),
            "articles": articles,
        }
        return jsonify(response), 200

    except Exception as exc:
        logger.error("Error in /api/node/source/%s: %s", source_id, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/node/other/<node_id>")
def api_node_other(node_id: str):
    """Return minimal detail for article/Other nodes."""
    try:
        article_link = None

        # Try Neo4j to resolve article_link from the node
        records = _neo4j_run(
            "MATCH (n:Article {id: $node_id}) RETURN n.article_link AS link",
            {"node_id": node_id},
        )
        if records:
            record = records[0]
            article_link = record.get("link")

        # If not found by id property, try internal Neo4j id (numeric node_id)
        if not article_link:
            try:
                nid = int(node_id)
                result = _neo4j_run(
                    "MATCH (n) WHERE id(n) = $nid RETURN n.article_link AS link",
                    {"nid": nid},
                )
                if result:
                    record = result[0]
                    if record:
                        article_link = record.get("link")
            except ValueError:
                pass

        # SQLite: look up article
        row = None
        if article_link:
            rows = _sqlite_run(
                "SELECT * FROM rss_articles WHERE link = ? LIMIT 1",
                (article_link,),
            )
            if rows:
                row = rows[0]
        else:
            # Fallback: try to find exact article by matching generated IDs
            parts = node_id.replace("article_", "").split("__")
            if len(parts) >= 2:
                domain_part = parts[0].replace("_", ".")
                # Get all articles from this domain and find exact match by ID
                rows = _sqlite_run(
                    "SELECT * FROM rss_articles WHERE link LIKE ?",
                    (f"%{domain_part}%",),
                )
                if rows:
                    for r in rows:
                        if _article_id(r.get("link", "")) == node_id:
                            row = r
                            break
                # If no exact match, fall back to most recent from domain
                if row is None:
                    rows = _sqlite_run(
                        "SELECT * FROM rss_articles WHERE link LIKE ? ORDER BY published_at DESC LIMIT 1",
                        (f"%{domain_part}%",),
                    )
                    if rows:
                        row = rows[0]
            elif len(parts) == 1:
                domain_part = parts[0].replace("_", ".")
                rows = _sqlite_run(
                    "SELECT * FROM rss_articles WHERE link LIKE ? ORDER BY published_at DESC LIMIT 1",
                    (f"%{domain_part}%",),
                )
                if rows:
                    row = rows[0]

        if row is None:
            # Return empty Other schema per spec
            return jsonify({
                "type": "Other",
                "title": None,
                "source_name": None,
                "source_type": None,
                "url": None,
                "published_at": None,
                "author_handle": None,
            }), 200

        response = {
            "type": "Other",
            "title": row.get("title"),
            "source_name": _feed_url_to_name(row.get("feed_url", "")),
            "source_type": row.get("source_type"),
            "url": row.get("link"),
            "published_at": row.get("published_at"),
            "author_handle": row.get("authors"),
        }
        return jsonify(response), 200

    except Exception as exc:
        logger.error("Error in /api/node/other/%s: %s", node_id, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/node/other/%s: %s", node_id, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/action/ftc-open/<violation_id>")
def api_action_ftc_open(violation_id: str):
    """Read and return the pre-filled FTC complaint markdown file."""
    try:
        ftc_path = OUTPUT_DIR / "ftc" / f"complaint-{violation_id}.md"
        content = ""
        if ftc_path.exists():
            content = ftc_path.read_text(encoding="utf-8")
        return jsonify({
            "content": content,
            "filename": f"complaint-{violation_id}.md",
            "submit_url": "https://reportfraud.ftc.gov/",
        }), 200
    except Exception as exc:
        logger.error("Error in /api/action/ftc-open/%s: %s", violation_id, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/action/ftc-open/%s: %s", violation_id, exc)
        return jsonify({"error": "Database unavailable"}), 503


@app.route("/api/action/digest-include/<handle>", methods=["POST"])
def api_action_digest_include(handle: str):
    """Toggle digest_include on a PalestinianVoice node in Neo4j."""
    try:
        result = _neo4j_run(
            """
            MATCH (v:PalestinianVoice {handle: $handle})
            SET v.digest_include = NOT coalesce(v.digest_include, false)
            RETURN v.digest_include AS digest_include
            """,
            {"handle": handle},
        )
        if result is None:
            return jsonify({"error": "Database unavailable"}), 503
        if not result:
            # Node not found — still return a deterministic response
            return jsonify({"handle": handle, "digest_include": False}), 200
        record = result[0]
        new_value = bool(record.get("digest_include", False))
        return jsonify({"handle": handle, "digest_include": new_value}), 200
    except Exception as exc:
        logger.error("Error in /api/action/digest-include/%s: %s", handle, exc)
        if _errors_file_handler:
            logging.getLogger().error("Error in /api/action/digest-include/%s: %s", handle, exc)
        return jsonify({"error": "Database unavailable"}), 503


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_dashboard() -> None:
    """Start the Flask dashboard server."""
    DASHBOARD_STATIC.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logger.info("Dashboard starting on %s:%d", DASHBOARD_HOST, DASHBOARD_PORT)
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    run_dashboard()
