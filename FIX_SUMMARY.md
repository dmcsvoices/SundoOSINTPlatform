# Sundo Dashboard Fix Summary

## Problem
The Sundo dashboard was showing Cytoscape errors and missing Author nodes:
1. Graph was limited to 200 articles but Author edges referenced all 723 articles in DB
2. Neo4j authentication was failing (`.env` not loaded)
3. Author nodes only existed in SQLite, not Neo4j
4. Author detail endpoint had no SQLite fallback

## Files Modified

### 1. `/home/darren/sundo-pi/sundo/report/cytoscape_export.py`
**Changes:**
- Removed `LIMIT 200` from `_sqlite_articles()` query (line 169)
- Added edge validation in `export_graph()` to filter edges with non-existent targets (lines 508-510)
- Fixed `export_author_nodes()` to fallback to SQLite when Neo4j has no Author nodes (lines 285-290)

**Result:** Graph now includes all 723 articles and 167 Author nodes with valid edges

### 2. `/home/darren/sundo-pi/sundo/dashboard/app.py`
**Changes:**
- Added SQLite fallback for `/api/node/author/<author_id>` endpoint (lines 545-580)
- Author articles load from SQLite when Neo4j fails (lines 600-625)
- Author publications load from SQLite when Neo4j fails (lines 627-655)

**Result:** Author detail panel works even without Neo4j

### 3. `/home/darren/sundo-pi/sundo/config.py`
**Changes:**
- Added `load_dotenv()` to load `.env` file (lines 8-12)

**Result:** Neo4j credentials now load properly

### 4. `/home/darren/sundo-pi/.env`
**Changes:**
- Removed duplicate `NEO4J_URI` line
- Fixed to single valid URI: `bolt://100.67.91.47:17687`

**Result:** Neo4j connection works

## Current Graph Status
- **886 nodes**: 708 Articles, 167 Authors, 8 Sources, 2 PalestinianVoice, 1 Person
- **1358 edges**: 708 PUBLISHED, 183 WRITES_FOR, 467 WROTE
- **0 edges with non-existent targets**

## Testing
1. Clear browser cache (Ctrl+Shift+R)
2. Refresh dashboard at http://localhost:15000
3. Check "Show Authors" checkbox is visible
4. Toggle Authors on/off
5. Click Author nodes to see details
6. Check browser console for errors (should be none)

## Service Status
- Sundo running on port 15000
- Serving updated graph.json with all Author nodes
- Neo4j connected (100.67.91.47:17687)
- SQLite fallbacks active for resilience
