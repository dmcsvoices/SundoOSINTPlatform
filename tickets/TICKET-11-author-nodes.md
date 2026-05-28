# Ticket 11 — Author Nodes: Represent Article Authors in the Knowledge Graph

**Type:** Feature Request  
**Priority:** High  
**Status:** Open  
**Created:** 2026-05-27  
**Component:** Graph Model / Ingest / Dashboard

---

## Core Idea

Authors sit between source organizations and articles in the graph topology. Right now the graph is flat — Al-Quds publishes articles, full stop. After this ticket, the graph reads like a newsroom:

```
[Organization/Al-Quds] ──PUBLISHED──► [Article]
         ▲
         │
[Author] ────WROTE───────────────┘
         │
         └──WRITES_FOR──► [Organization/Al-Quds]
```

That last connection is the hidden node with both their social reach score and their full article history. **That's the amplification unit — not a publication, a person.**

---

## Why This Matters

Individual journalists are the amplification unit — not publications. When a coordination event pushes a false narrative, the counter-response is boosting specific journalists covering the story accurately, not just linking to a publication homepage.

Author nodes also enable a future merge: when an `Author` node's handle matches a `PalestinianVoice` node's handle, they can be unified into a single entity, connecting the journalist's byline history directly to their social reach score.

---

## Unlocks These Queries

- "Show me all articles by this author across all sources"
- "Which authors write for multiple outlets?" (cross-publication journalists)
- "Which Palestinian Voice node corresponds to this byline author?"
- "Which authors are most prolific on this topic in the last 30 days?"

---

## High-Level Changes

| Layer | Change |
|---|---|
| **Ingest** | Extract authors from RSS entries; normalize bylines; discard generics (Staff, Reuters, AP) |
| **SQLite** | New `authors` table; `rss_articles` gets `author_id` + `author_display_name` columns |
| **Neo4j** | New `Author` node label; `WROTE`, `WRITES_FOR`, `IS_VOICE` relationships |
| **Detection** | Match `Author` handles to `PalestinianVoice` handles (exact + fuzzy) |
| **Export** | Author nodes + edges in Cytoscape.js export; size scales with `article_count` |
| **Dashboard** | Teal Author nodes; click panel showing articles, publications, linked voice; toggle checkbox |
| **Tests** | Unit tests for byline parsing and normalization |

---

## Files to Create / Modify

**Create:**
- `sundo/ingest/author_extractor.py`
- `sundo/detect/author_voice_matcher.py`
- `sundo/tests/test_author_extractor.py`

**Modify:**
- `sundo/ingest/rss_aggregator.py`
- `sundo/db/sqlite_store.py`
- `sundo/db/neo4j_client.py`
- `sundo/report/cytoscape_export.py`
- `sundo/dashboard/app.py`
- `sundo/dashboard/templates/index.html`
- `sundo/main.py`

---

## Acceptance Criteria

- [ ] `author_extractor.py` correctly parses author bylines from all four RSS feed formats (`author`, `author_detail`, `authors[]`, `dc_creator`)
- [ ] Generic bylines (Staff, Reuters, AP, etc.) are discarded — not stored
- [ ] Arabic-language author names are preserved without corruption
- [ ] `authors` SQLite table created and populated after next RSS ingestion run
- [ ] `Author` nodes appear in Neo4j after ingestion
- [ ] `WROTE` edges connect Author nodes to Article nodes
- [ ] `WRITES_FOR` edges connect Author nodes to Organization nodes
- [ ] Teal (`#0ca678`) Author nodes render in Cytoscape dashboard
- [ ] Author toggle checkbox shows/hides Author nodes and their edges correctly
- [ ] Clicking an Author node opens panel with articles, publications, linked voice
- [ ] Author-voice exact match creates `IS_VOICE` edge automatically
- [ ] Fuzzy match candidates are flagged in Neo4j for operator review
- [ ] `IS_VOICE` edges render in purple/dotted style in the graph
- [ ] Node size scales with `article_count` (prolific authors appear larger)
- [ ] All unit tests in `test_author_extractor.py` pass (`pytest sundo/tests/test_author_extractor.py -v` exits 0)

---

## Visual Result When Complete

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

The graph becomes a **journalist-centric view** of the information ecosystem — not just which outlets publish what, but which individual voices are producing the ground-level reporting that Sundo Pi exists to amplify.

---

## Detailed Implementation Specification

See attached spec: `ticket_11_author_nodes_spec.md` for full implementation details including:
- Complete `author_extractor.py` module with extraction + normalization + language detection
- SQLite migration statements and `upsert_author_sqlite()` function
- Neo4j `MERGE` helpers for Author nodes and all three relationship types
- `author_voice_matcher.py` with exact/fuzzy matching logic
- Cytoscape.js export additions for Author nodes and edges
- Dashboard styling (teal nodes, dashed/solid/dotted edges, legend, toggle, panel renderer)
- Flask `/api/node/author/<author_id>` endpoint
- JavaScript `fetchAuthorDetail()` + `renderAuthorPanel()` implementations
- APScheduler integration for periodic voice matching
- Full unit test suite for byline parsing
