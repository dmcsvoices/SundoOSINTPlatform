# Ticket 11 — Author Nodes: Represent Article Authors in the Knowledge Graph
## Detailed Implementation Specification

---

## Overview

Currently the Sundo Pi graph shows source organizations (Al-Quds, Mondoweiss,
+972 Magazine etc.) connected to their articles via `PUBLISHED` edges. Authors
are stored as a flat `author_handle` text field on the `rss_articles` SQLite
table but have no graph representation.

This ticket introduces `Author` as a first-class node type in both Neo4j and
the Cytoscape dashboard. Authors sit between sources and articles in the graph
topology:

```
Before:
 [Organization/PalestinianVoice] ──PUBLISHED──► [Article]

After:
 [Organization/PalestinianVoice] ──PUBLISHED──► [Article]
 ▲
 [Author] ──────────────────────────────────WROTE────┘
 [Author] ──────────────────────WRITES_FOR──► [Organization]
```

This unlocks several high-value queries:

- "Show me all articles by this author across all sources"
- "Which authors write for multiple outlets?" (cross-publication journalists)
- "Which Palestinian Voice node corresponds to this byline author?"
- "Which authors are most prolific on this topic in the last 30 days?"

---

## Why this matters for the mission

Individual journalists are the amplification unit — not publications. When a
coordination event pushes a false narrative, the counter-response is boosting
specific journalists covering the story accurately, not just linking to a
publication homepage.

Author nodes also enable a future merge: when an `Author` node's handle matches
a `PalestinianVoice` node's handle, they can be unified into a single entity,
connecting the journalist's byline history directly to their social reach score.

---

## Data model

### New Neo4j node: Author

```
Author {
 id: string — slugified handle: "bilal-shbair" or "bylinesbilal"
 display_name: string — human-readable: "Bilal Shbair"
 handle: string — social handle if known, else byline slug
 byline_variants: string[] — all name forms seen: ["B. Shbair", "Bilal Shbair"]
 primary_language: string — "en" | "ar" | "he" | etc.
 article_count: integer — total articles in DB (denormalized, updated nightly)
 first_seen: ISO8601
 last_seen: ISO8601
 linked_voice_id: string — handle of PalestinianVoice node if matched, else null
 verification_status: string — "verified" | "pending" | "unknown"
}
```

### New Neo4j relationships

```cypher
// Author wrote a specific article
(Author)-[:WROTE {published_at: ISO8601, source_name: string}]->(Article)

// Author writes for a publication (inferred from article history)
(Author)-[:WRITES_FOR {article_count: int, first_seen: ISO8601}]->(Organization)

// Author is the same entity as a PalestinianVoice (manual or auto-matched)
(Author)-[:IS_VOICE]->(PalestinianVoice)
```

### SQLite additions

Add to `rss_articles` table (via `apply_migrations()`):

```sql
ALTER TABLE rss_articles ADD COLUMN author_display_name TEXT;
ALTER TABLE rss_articles ADD COLUMN author_id TEXT;
```

Add new table for author registry:

```sql
CREATE TABLE IF NOT EXISTS authors (
 id TEXT PRIMARY KEY,
 display_name TEXT,
 handle TEXT,
 byline_variants TEXT, -- JSON array stored as string
 primary_language TEXT,
 article_count INTEGER DEFAULT 0,
 first_seen TEXT,
 last_seen TEXT,
 linked_voice_id TEXT,
 verification_status TEXT DEFAULT 'unknown',
 created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Files to create or modify

```
sundo/
├── ingest/
│ ├── rss_aggregator.py ← MODIFY: extract author from feed entries
│ └── author_extractor.py ← CREATE: byline parsing and normalization
├── db/
│ ├── sqlite_store.py ← MODIFY: add authors table + migrations
│ └── neo4j_client.py ← MODIFY: add author upsert helpers
├── detect/
│ └── author_voice_matcher.py ← CREATE: match Author nodes to PalestinianVoice
├── report/
│ └── cytoscape_export.py ← MODIFY: include Author nodes and edges
└── dashboard/
 └── templates/index.html ← MODIFY: Author node color, click panel
tests/
└── test_author_extractor.py ← CREATE: byline parsing unit tests
```

---

## Part 1 — Author extraction from RSS feeds

### 1a. Create `sundo/ingest/author_extractor.py`

RSS feeds expose author information in several inconsistent ways. This module
normalizes all of them into a canonical author record.

```python
"""
sundo/ingest/author_extractor.py

Extracts and normalizes author information from RSS feed entries.
Handles the inconsistent ways different feeds expose byline data.
"""

import re
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Extraction ───────────────────────────────────────────────────────────────

def extract_author(entry: dict) -> Optional[dict]:
 """
 Extract author information from a feedparser entry dict.

 Tries multiple fields in priority order:
 1. entry.author — most common
 2. entry.author_detail — structured, may include email/href
 3. entry.authors[] — list form (Atom feeds)
 4. entry.dc_creator — Dublin Core
 5. entry.tags — some feeds embed author in tags

 Returns a normalized author dict or None if no author found.
 """
 raw_name = None

 # Priority 1: entry.author (string)
 if hasattr(entry, 'author') and entry.author:
 raw_name = entry.author

 # Priority 2: entry.author_detail (structured)
 elif hasattr(entry, 'author_detail') and entry.author_detail:
 detail = entry.author_detail
 raw_name = getattr(detail, 'name', None) or getattr(detail, 'email', None)

 # Priority 3: entry.authors (list — Atom)
 elif hasattr(entry, 'authors') and entry.authors:
 first = entry.authors[0]
 raw_name = first.get('name') or first.get('email')

 # Priority 4: Dublin Core creator
 elif hasattr(entry, 'dc_creator') and entry.dc_creator:
 raw_name = entry.dc_creator

 if not raw_name:
 return None

 return normalize_author(raw_name)


def normalize_author(raw_name: str) -> Optional[dict]:
 """
 Normalize a raw byline string into a canonical author dict.

 Handles common RSS byline noise:
 - "By Bilal Shbair" → "Bilal Shbair"
 - "bilal.shbair@972mag.com" → "bilal.shbair"
 - "Staff Writer" → None (generic — discard)
 - "Reuters" → None (wire service — discard)
 - "AP" → None (wire service — discard)
 """
 if not raw_name or not raw_name.strip():
 return None

 name = raw_name.strip()

 # Strip "By " prefix (case-insensitive)
 name = re.sub(r'^[Bb]y\s+', '', name)

 # Strip email addresses — extract username if name-like
 if '@' in name:
 username = name.split('@')[0]
 # Convert dots/underscores to spaces for display
 name = username.replace('.', ' ').replace('_', ' ').title()

 name = name.strip()

 # Discard generic bylines — not useful as author nodes
 DISCARD_NAMES = {
 'staff', 'staff writer', 'staff reporter', 'editorial', 'editors',
 'the editors', 'admin', 'administrator', 'webmaster', 'news desk',
 'foreign desk', 'reuters', 'ap', 'associated press', 'afp',
 'wire service', 'contributor', 'guest writer', 'special to',
 'correspondent', 'news', 'breaking news', 'unknown', 'anonymous',
 }
 if name.lower() in DISCARD_NAMES:
 logger.debug("Discarding generic byline: %s", name)
 return None

 # Must have at least 2 characters
 if len(name) < 2:
 return None

 author_id = make_author_id(name)
 handle = make_handle(name)

 return {
 'id': author_id,
 'display_name': name,
 'handle': handle,
 'byline_variants': [name],
 }


def make_author_id(display_name: str) -> str:
 """
 Create a stable, unique author ID from a display name.

 Uses a slug of the normalized name with a short hash suffix
 to handle collisions (two different "John Smith" authors).

 Format: "john-smith-a3f2"
 """
 slug = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
 hash_suffix = hashlib.md5(display_name.encode()).hexdigest()[:4]
 return f"{slug}-{hash_suffix}"


def make_handle(display_name: str) -> str:
 """
 Create a social-media-style handle from a display name.
 "Bilal Shbair" → "bilalshbair"
 """
 return re.sub(r'[^a-z0-9]', '', display_name.lower())


# ── Language detection ────────────────────────────────────────────────────────

def detect_language(text: str) -> str:
 """
 Lightweight language detection based on character ranges.
 Returns ISO 639-1 code. Falls back to 'en'.

 For full accuracy, replace with langdetect library if available.
 """
 if not text:
 return 'en'

 # Arabic Unicode block: U+0600–U+06FF
 arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
 # Hebrew Unicode block: U+0590–U+05FF
 hebrew_chars = sum(1 for c in text if '\u0590' <= c <= '\u05FF')

 total = len(text)
 if total == 0:
 return 'en'

 if arabic_chars / total > 0.15:
 return 'ar'
 if hebrew_chars / total > 0.15:
 return 'he'

 return 'en'
```

---

### 1b. Modify `sundo/ingest/rss_aggregator.py`

In the feed entry processing loop, add author extraction after the existing
article field extraction. Add these lines where each entry is processed:

```python
from sundo.ingest.author_extractor import extract_author, detect_language

# Inside the entry processing loop, after extracting title/url/published_at:

author_data = extract_author(entry)

if author_data:
 # Detect language from article title if available
 title = entry.get('title', '')
 author_data['primary_language'] = detect_language(title)

 # Write author to SQLite authors table
 upsert_author_sqlite(conn, author_data)

 # Write to rss_articles — store author_id and display_name
 article_data['author_id'] = author_data['id']
 article_data['author_display_name'] = author_data['display_name']
else:
 article_data['author_id'] = None
 article_data['author_display_name'] = None
```

Add `upsert_author_sqlite()` to `sqlite_store.py`:

```python
def upsert_author_sqlite(conn: sqlite3.Connection, author_data: dict) -> None:
 """
 Insert or update an author record in SQLite.
 On conflict (same id), appends new byline variant if not already present
 and updates last_seen and article_count.
 """
 import json
 from datetime import datetime

 now = datetime.utcnow().isoformat()
 author_id = author_data['id']

 # Fetch existing to merge byline_variants
 existing = conn.execute(
 "SELECT byline_variants FROM authors WHERE id = ?", (author_id,)
 ).fetchone()

 if existing:
 variants = json.loads(existing[0] or '[]')
 new_variant = author_data['display_name']
 if new_variant not in variants:
 variants.append(new_variant)
 conn.execute("""
 UPDATE authors
 SET byline_variants = ?,
 last_seen = ?,
 article_count = article_count + 1,
 updated_at = ?
 WHERE id = ?
 """, (json.dumps(variants), now, now, author_id))
 else:
 conn.execute("""
 INSERT INTO authors
 (id, display_name, handle, byline_variants,
 primary_language, article_count, first_seen, last_seen,
 verification_status, created_at, updated_at)
 VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'unknown', ?, ?)
 """, (
 author_id,
 author_data['display_name'],
 author_data['handle'],
 json.dumps(author_data['byline_variants']),
 author_data.get('primary_language', 'en'),
 now, now, now, now
 ))
 conn.commit()
```

---

## Part 2 — Neo4j graph population

### 2a. Add author upsert helpers to `sundo/db/neo4j_client.py`

```python
def upsert_author(self, author_data: dict) -> None:
 """Create or update an Author node in Neo4j."""
 with self.driver.session() as session:
 session.run("""
 MERGE (a:Author {id: $id})
 ON CREATE SET
 a.display_name = $display_name,
 a.handle = $handle,
 a.byline_variants = $byline_variants,
 a.primary_language = $primary_language,
 a.article_count = 1,
 a.first_seen = $now,
 a.last_seen = $now,
 a.linked_voice_id = null,
 a.verification_status = 'unknown'
 ON MATCH SET
 a.last_seen = $now,
 a.article_count = a.article_count + 1
 """, {**author_data, 'now': datetime.utcnow().isoformat()})


def link_author_to_article(
 self,
 author_id: str,
 article_id: str,
 published_at: str,
 source_name: str
) -> None:
 """Create WROTE relationship between Author and Article nodes."""
 with self.driver.session() as session:
 session.run("""
 MATCH (a:Author {id: $author_id})
 MATCH (art:Article {id: $article_id})
 MERGE (a)-[r:WROTE]->(art)
 ON CREATE SET
 r.published_at = $published_at,
 r.source_name = $source_name
 """, {
 'author_id': author_id,
 'article_id': article_id,
 'published_at': published_at,
 'source_name': source_name,
 })


def link_author_to_organization(
 self,
 author_id: str,
 org_id: str,
 article_count: int,
 first_seen: str
) -> None:
 """Create or update WRITES_FOR relationship."""
 with self.driver.session() as session:
 session.run("""
 MATCH (a:Author {id: $author_id})
 MATCH (o:Organization {id: $org_id})
 MERGE (a)-[r:WRITES_FOR]->(o)
 ON CREATE SET
 r.article_count = $article_count,
 r.first_seen = $first_seen
 ON MATCH SET
 r.article_count = r.article_count + 1
 """, {
 'author_id': author_id,
 'org_id': org_id,
 'article_count': article_count,
 'first_seen': first_seen,
 })


def link_author_to_voice(self, author_id: str, voice_handle: str) -> None:
 """Link an Author node to a PalestinianVoice node (IS_VOICE relationship)."""
 with self.driver.session() as session:
 session.run("""
 MATCH (a:Author {id: $author_id})
 MATCH (v:PalestinianVoice {handle: $voice_handle})
 MERGE (a)-[:IS_VOICE]->(v)
 """, {'author_id': author_id, 'voice_handle': voice_handle})
```

---

## Part 3 — Author-Voice matching

### Create `sundo/detect/author_voice_matcher.py`

This module runs after each ingestion cycle and attempts to automatically match
`Author` nodes to existing `PalestinianVoice` nodes using handle similarity.
Confirmed matches create an `IS_VOICE` relationship. Unconfirmed candidates are
flagged for operator review.

```python
"""
sundo/detect/author_voice_matcher.py

Matches Author nodes (from RSS bylines) to PalestinianVoice nodes
(from the curated voice registry) using handle similarity.

Match levels:
 EXACT — author.handle == voice.handle (auto-confirmed)
 FUZZY — similarity > 0.80 (flagged for operator review)
 NONE — no match found
"""

import logging
from difflib import SequenceMatcher
from sundo.db.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.80


def run(neo4j_client: Neo4jClient) -> dict:
 """
 Run author-voice matching pass.
 Returns summary dict with match counts.
 """
 authors = get_unmatched_authors(neo4j_client)
 voices = get_all_voices(neo4j_client)

 results = {
 'exact_matches': 0,
 'fuzzy_candidates': 0,
 'no_match': 0,
 }

 for author in authors:
 match_type, voice_handle = find_match(author, voices)

 if match_type == 'EXACT':
 neo4j_client.link_author_to_voice(author['id'], voice_handle)
 neo4j_client.set_author_linked_voice(author['id'], voice_handle)
 logger.info(
 "Exact match: Author %s ↔ PalestinianVoice %s",
 author['handle'], voice_handle
 )
 results['exact_matches'] += 1

 elif match_type == 'FUZZY':
 neo4j_client.flag_author_for_review(
 author['id'], voice_handle, 'fuzzy_voice_match'
 )
 logger.info(
 "Fuzzy candidate: Author %s ~ PalestinianVoice %s (needs review)",
 author['handle'], voice_handle
 )
 results['fuzzy_candidates'] += 1

 else:
 results['no_match'] += 1

 logger.info(
 "Author-voice matching: %d exact, %d fuzzy, %d unmatched",
 results['exact_matches'],
 results['fuzzy_candidates'],
 results['no_match'],
 )
 return results


def find_match(
 author: dict,
 voices: list[dict]
) -> tuple[str, str | None]:
 """
 Attempt to match a single author to a voice.
 Returns (match_type, voice_handle) or ('NONE', None).
 """
 author_handle = author.get('handle', '').lower()

 for voice in voices:
 voice_handle = voice.get('handle', '').lower()

 # Exact handle match
 if author_handle == voice_handle:
 return 'EXACT', voice['handle']

 # Fuzzy match on handle
 ratio = SequenceMatcher(None, author_handle, voice_handle).ratio()
 if ratio >= FUZZY_THRESHOLD:
 return 'FUZZY', voice['handle']

 return 'NONE', None


def get_unmatched_authors(neo4j_client: Neo4jClient) -> list[dict]:
 with neo4j_client.driver.session() as session:
 result = session.run("""
 MATCH (a:Author)
 WHERE a.linked_voice_id IS NULL
 RETURN a.id AS id, a.handle AS handle,
 a.display_name AS display_name
 """)
 return [dict(r) for r in result]


def get_all_voices(neo4j_client: Neo4jClient) -> list[dict]:
 with neo4j_client.driver.session() as session:
 result = session.run("""
 MATCH (v:PalestinianVoice)
 RETURN v.handle AS handle, v.reach_score AS reach_score
 """)
 return [dict(r) for r in result]
```

---

## Part 4 — Cytoscape export

### Modify `sundo/report/cytoscape_export.py`

Add Author nodes and their edges to the graph export. Authors should be exported
as a distinct node type so the dashboard can color them differently.

```python
def export_author_nodes(neo4j_client) -> tuple[list, list]:
 """
 Export Author nodes and their WROTE / WRITES_FOR edges.

 Returns:
 (nodes, edges) — lists of Cytoscape.js dicts
 """
 nodes = []
 edges = []

 with neo4j_client.driver.session() as session:

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

 return nodes, edges
```

Merge these into the main `export_graph()` function alongside existing node types.

---

## Part 5 — Dashboard: Author node styling and click panel

### 5a. Node color

Add Author to the Cytoscape stylesheet. Use **teal** (`#0ca678`) to distinguish
from green (PalestinianVoice), blue (Organization), and gray (Article).

```javascript
// In cy stylesheet array — add:
{
 selector: "node[type = 'Author']",
 style: {
 'background-color': '#0ca678',
 'label': 'data(label)',
 'width': 'data(size)',
 'height': 'data(size)',
 'font-size': '10px',
 'text-valign': 'bottom',
 'text-margin-y': '4px',
 'color': '#333',
 'text-outline-width': 0,
 }
},
// WROTE edges — lighter weight than PUBLISHED
{
 selector: "edge[relationship = 'WROTE']",
 style: {
 'width': 1,
 'line-color': '#0ca678',
 'line-style': 'dashed',
 'target-arrow-color': '#0ca678',
 'target-arrow-shape': 'triangle',
 'opacity': 0.5,
 }
},
// WRITES_FOR edges — solid, slightly heavier
{
 selector: "edge[relationship = 'WRITES_FOR']",
 style: {
 'width': 2,
 'line-color': '#0ca678',
 'target-arrow-color': '#0ca678',
 'target-arrow-shape': 'triangle',
 'opacity': 0.7,
 }
},
// IS_VOICE edges — purple, makes the Author↔Voice link visually distinct
{
 selector: "edge[relationship = 'IS_VOICE']",
 style: {
 'width': 2,
 'line-color': '#7048e8',
 'target-arrow-color': '#7048e8',
 'target-arrow-shape': 'triangle',
 'line-style': 'dotted',
 'opacity': 0.8,
 }
},
```

### 5b. Legend update

Add to the legend in `index.html`:

```html
<li><span class="dot" style="background:#0ca678"></span> Author</li>
```

### 5c. Author toggle button

Add a toggle alongside the existing Show Articles checkbox:

```html
<label style="font-size:13px;cursor:pointer;margin-left:16px">
 <input type="checkbox" id="toggle-authors"
 onchange="toggleAuthorNodes(this.checked)" checked>
 Show Authors
</label>
```

```javascript
function toggleAuthorNodes(show) {
 const authorNodes = cy.nodes("[type = 'Author']");
 if (show) {
 authorNodes.show();
 authorNodes.connectedEdges().show();
 } else {
 authorNodes.hide();
 authorNodes.connectedEdges().hide();
 }
 updateNodeCount();
}
```

### 5d. Flask endpoint: `/api/node/author/<author_id>`

```python
@app.route('/api/node/author/<author_id>')
def node_author(author_id):
 """Return full Author node detail."""
 with neo4j_client.driver.session() as session:
 # Base author data
 r = session.run("""
 MATCH (a:Author {id: $id})
 RETURN a
 """, {'id': author_id}).single()

 if not r:
 return jsonify({'error': 'Author not found'}), 404

 author = dict(r['a'])

 # Articles written
 articles = session.run("""
 MATCH (a:Author {id: $id})-[r:WROTE]->(art:Article)
 RETURN art.id AS id, art.title AS title,
 art.url AS url, r.published_at AS published_at,
 r.source_name AS source_name
 ORDER BY r.published_at DESC
 LIMIT 10
 """, {'id': author_id})
 author['articles'] = [dict(a) for a in articles]

 # Publications written for
 pubs = session.run("""
 MATCH (a:Author {id: $id})-[r:WRITES_FOR]->(o:Organization)
 RETURN o.name AS name, o.id AS org_id,
 r.article_count AS article_count
 ORDER BY r.article_count DESC
 """, {'id': author_id})
 author['publications'] = [dict(p) for p in pubs]

 # Linked PalestinianVoice if matched
 voice = session.run("""
 MATCH (a:Author {id: $id})-[:IS_VOICE]->(v:PalestinianVoice)
 RETURN v.handle AS handle, v.reach_score AS reach_score,
 v.verification_status AS verification_status
 """, {'id': author_id}).single()
 author['linked_voice'] = dict(voice) if voice else None

 return jsonify(author)
```

### 5e. Author click panel renderer

Add to the JavaScript panel renderers in `index.html`:

```javascript
function fetchAuthorDetail(authorId) {
 fetch('/api/node/author/' + encodeURIComponent(authorId))
 .then(r => r.json())
 .then(d => renderAuthorPanel(d))
 .catch(() => {
 document.getElementById('panel-body').innerHTML =
 '<div class="loading-state">Could not load author data.</div>';
 });
}

function renderAuthorPanel(d) {
 let html = '';

 // Status badges
 html += '<div class="panel-section">';
 html += `<span class="badge badge-green">Author</span> `;
 if (d.primary_language === 'ar') {
 html += '<span class="badge badge-blue">Arabic</span> ';
 }
 if (d.linked_voice) {
 html += '<span class="badge" style="background:#ede9fe;color:#5f3dc4">' +
 'Voice matched</span>';
 }
 html += '</div>';

 // Core stats
 html += '<div class="panel-section">';
 html += '<div class="panel-section-title">Author</div>';
 html += row('Handle', d.handle || '—');
 html += row('Language', d.primary_language || 'en');
 html += row('Articles tracked', (d.article_count || 0).toString());
 html += row('First seen', fmtDate(d.first_seen));
 html += row('Last seen', fmtDate(d.last_seen));
 html += '</div>';

 // Byline variants
 if (d.byline_variants && d.byline_variants.length > 1) {
 html += '<div class="panel-section">';
 html += '<div class="panel-section-title">Byline variants</div>';
 const variants = typeof d.byline_variants === 'string'
 ? JSON.parse(d.byline_variants)
 : d.byline_variants;
 variants.forEach(v => {
 html += `<div style="padding:2px 0;font-size:12px">${v}</div>`;
 });
 html += '</div>';
 }

 // Publications
 if (d.publications && d.publications.length > 0) {
 html += '<div class="panel-section">';
 html += '<div class="panel-section-title">Writes for</div>';
 d.publications.forEach(p => {
 html += `<div class="article-item">`;
 html += `<strong>${p.name}</strong>`;
 html += `<div class="article-meta">${p.article_count} articles</div>`;
 html += '</div>';
 });
 html += '</div>';
 }

 // Linked Palestinian Voice
 if (d.linked_voice) {
 html += '<div class="panel-section">';
 html += '<div class="panel-section-title">Linked voice</div>';
 html += `<div class="article-item">`;
 html += `@${d.linked_voice.handle}`;
 const reachPct = Math.round((d.linked_voice.reach_score || 0) * 100);
 html += `<div class="article-meta">Reach score: ${reachPct}% · `;
 html += `${d.linked_voice.verification_status}</div>`;
 html += '</div></div>';
 }

 // Recent articles
 if (d.articles && d.articles.length > 0) {
 html += '<div class="panel-section">';
 html += '<div class="panel-section-title">Recent articles</div>';
 d.articles.forEach(a => {
 html += `<div class="article-item">`;
 html += `<a href="${a.url}" target="_blank">${a.title}</a>`;
 html += `<div class="article-meta">${a.source_name} · `;
 html += `${fmtDate(a.published_at)}</div>`;
 html += '</div>';
 });
 html += '</div>';
 }

 document.getElementById('panel-body').innerHTML = html;
}
```

Update the main tap handler to route Author node clicks:

```javascript
// In openPanel() — add Author case:
} else if (type === 'Author') {
 title.textContent = nodeData.label || nodeData.id;
 fetchAuthorDetail(id);
}
```

---

## Part 6 — Scheduler integration

Add to `sundo/main.py` APScheduler jobs:

```python
# Author-voice matching — runs after each RSS ingestion cycle
scheduler.add_job(
 func=lambda: author_voice_matcher.run(neo4j_client),
 trigger='interval',
 hours=2,
 id='author_voice_matcher',
 name='Author-Voice Matcher',
 replace_existing=True,
)
```

---

## Part 7 — Unit tests

Create `sundo/tests/test_author_extractor.py`:

```python
"""
Tests for sundo/ingest/author_extractor.py
"""

import unittest
from sundo.ingest.author_extractor import (
 extract_author, normalize_author, make_author_id, make_handle
)


class TestNormalizeAuthor(unittest.TestCase):

 def test_strips_by_prefix(self):
 result = normalize_author("By Bilal Shbair")
 self.assertEqual(result['display_name'], "Bilal Shbair")

 def test_strips_by_prefix_lowercase(self):
 result = normalize_author("by bilal shbair")
 self.assertIsNotNone(result)

 def test_email_byline_converted(self):
 result = normalize_author("bilal.shbair@972mag.com")
 self.assertIsNotNone(result)
 self.assertNotIn('@', result['display_name'])

 def test_generic_staff_discarded(self):
 self.assertIsNone(normalize_author("Staff Writer"))
 self.assertIsNone(normalize_author("staff"))
 self.assertIsNone(normalize_author("The Editors"))

 def test_wire_services_discarded(self):
 self.assertIsNone(normalize_author("Reuters"))
 self.assertIsNone(normalize_author("AP"))
 self.assertIsNone(normalize_author("Associated Press"))

 def test_empty_string_returns_none(self):
 self.assertIsNone(normalize_author(""))
 self.assertIsNone(normalize_author(" "))

 def test_single_char_returns_none(self):
 self.assertIsNone(normalize_author("X"))

 def test_arabic_name_preserved(self):
 result = normalize_author("محمد الشيخ")
 self.assertIsNotNone(result)
 self.assertEqual(result['display_name'], "محمد الشيخ")

 def test_byline_variant_stored(self):
 result = normalize_author("Bilal Shbair")
 self.assertIn("Bilal Shbair", result['byline_variants'])


class TestMakeAuthorId(unittest.TestCase):

 def test_slug_format(self):
 author_id = make_author_id("Bilal Shbair")
 self.assertRegex(author_id, r'^[a-z0-9\-]+-[a-f0-9]{4}$')

 def test_special_chars_removed(self):
 author_id = make_author_id("O'Brien & Sons")
 self.assertNotIn("'", author_id)
 self.assertNotIn("&", author_id)

 def test_same_name_same_id(self):
 self.assertEqual(
 make_author_id("Bilal Shbair"),
 make_author_id("Bilal Shbair")
 )

 def test_different_names_different_ids(self):
 self.assertNotEqual(
 make_author_id("Bilal Shbair"),
 make_author_id("Mohammed Al-Sheikh")
 )


class TestMakeHandle(unittest.TestCase):

 def test_spaces_removed(self):
 self.assertEqual(make_handle("Bilal Shbair"), "bilalshbair")

 def test_lowercase(self):
 self.assertEqual(make_handle("BILAL"), "bilal")

 def test_special_chars_removed(self):
 self.assertEqual(make_handle("O'Brien"), "obrien")


class TestExtractAuthor(unittest.TestCase):

 def _make_entry(self, **kwargs):
 """Create a minimal feedparser-like entry object."""
 class Entry:
 pass
 e = Entry()
 for k, v in kwargs.items():
 setattr(e, k, v)
 return e

 def test_extracts_from_author_field(self):
 entry = self._make_entry(author="Bilal Shbair")
 result = extract_author(entry)
 self.assertIsNotNone(result)
 self.assertEqual(result['display_name'], "Bilal Shbair")

 def test_returns_none_when_no_author(self):
 entry = self._make_entry()
 result = extract_author(entry)
 self.assertIsNone(result)

 def test_returns_none_for_generic_author(self):
 entry = self._make_entry(author="Staff Writer")
 result = extract_author(entry)
 self.assertIsNone(result)


if __name__ == '__main__':
 unittest.main()
```

---

## Implementation order for the coding agent

1. `sundo/ingest/author_extractor.py` — create, all functions
2. `sundo/db/sqlite_store.py` — add `authors` table to `apply_migrations()`
3. `sundo/db/neo4j_client.py` — add all four author helper methods
4. `sundo/ingest/rss_aggregator.py` — wire in `extract_author()` call
5. `sundo/detect/author_voice_matcher.py` — create
6. `sundo/report/cytoscape_export.py` — add `export_author_nodes()`
7. `sundo/dashboard/app.py` — add `/api/node/author/<author_id>` endpoint
8. `sundo/dashboard/templates/index.html` — node styling, legend, toggle, panel renderer
9. `sundo/main.py` — register `author_voice_matcher` scheduler job
10. `sundo/tests/test_author_extractor.py` — create, all tests

---

## Acceptance criteria

- [ ] `author_extractor.py` correctly parses author bylines from all four RSS
 feed formats (author, author_detail, authors[], dc_creator)
- [ ] Generic bylines (Staff, Reuters, AP, etc.) are discarded — not stored
- [ ] Arabic-language author names are preserved without corruption
- [ ] `authors` SQLite table created and populated after next RSS ingestion run
- [ ] `Author` nodes appear in Neo4j after ingestion
- [ ] `WROTE` edges connect Author nodes to Article nodes
- [ ] `WRITES_FOR` edges connect Author nodes to Organization nodes
- [ ] Teal Author nodes render in Cytoscape dashboard
- [ ] Author toggle checkbox shows/hides Author nodes and their edges correctly
- [ ] Clicking an Author node opens panel with articles, publications, linked voice
- [ ] Author-voice exact match creates `IS_VOICE` edge automatically
- [ ] Fuzzy match candidates are flagged in Neo4j for operator review
- [ ] `IS_VOICE` edges render in purple/dotted style in the graph
- [ ] Node size scales with `article_count` (prolific authors appear larger)
- [ ] All unit tests in `test_author_extractor.py` pass
- [ ] `pytest sundo/tests/test_author_extractor.py -v` exits with code 0

---

## Visual result when complete

```
[Al-Quds org node] ──PUBLISHED──► [Article node]
 ▲
[Author: محمد الشيخ] ────WROTE──────────┘
 │
 └──WRITES_FOR──► [Al-Quds org node]

[Author: BylinesBilal] ──WROTE──► [Article node]
 │
 └──IS_VOICE (purple dotted) ──► [PalestinianVoice: BylinesBilal]
```

The graph becomes a journalist-centric view of the information ecosystem —
not just which outlets publish what, but which individual voices are producing
the ground-level reporting that Sundo Pi exists to amplify.
