Sundo Pi Dashboard — Full Review

## What's Working Well
- Login page is clean, well-branded, and functional
- Graph visualization renders — Cytoscape.js is integrated and all 1,358 edges load correctly
- Multiple layouts all switch correctly (Concentric, Cose, Circle, Grid, Tree, Random)
- Cose (organic) layout is genuinely useful — it reveals the two distinct account clusters (BylinesBilal vs. motasemadnan) naturally
- Show Articles / Show Authors toggles work

## Issues to Fix — Prioritized

### 🔴 Critical
1. **/api/graph is unauthenticated** — Navigating directly to http://100.67.91.47:15000/api/graph returns the full graph JSON — all nodes, edges, labels, and article links — with no login required. Any unauthenticated user who discovers the URL gets all the data. The API route needs the same session check as the / route.
2. **Search is completely broken** — Typing into the "Search nodes…" box and pressing Enter does nothing — the graph doesn't filter, highlight, or zoom to any node. Given that there are 167 Authors and 708 Articles, search is the primary way to navigate. This needs to at minimum filter/highlight matching nodes by label.
3. **Node detail panel is nearly empty** — Hovering shows only two lines: the node name and its type. But each node type has rich data in the API that's going unused:
   - Author: handle, article_count, verification_status, primary_language
   - Article: article_link (clickable), published_at, tags, fara_linked
   - Source: credibility_score, fara_linked
   - PalestinianVoice: fara_linked, credibility_score
   - At minimum, clicking an Article node should open its article_link. Clicking an Author should show their handle and article count.

### 🟠 High
4. **Source nodes are invisible in the legend** — There are 8 Source nodes (Al-Quds, Middle East Eye, Mondoweiss, The Forward, +972 Magazine, JTA, The Intercept, Electronic Intifada) but the legend only lists: Person (FARA-linked), Person (unlinked), Organization, PalestinianVoice, Article, Author. Sources are silently present in the graph but unlabeled in the legend — users have no idea they exist.
5. **Article count discrepancy** — The legend displays "Article (723)" but only 708 Article nodes are in the graph. The 15-article difference suggests the count comes from the database and the graph data are out of sync. Both should show the same number.
6. **No node click action** — Clicking a node does nothing. The expected behavior for an OSINT tool is: click an Author → see all their articles and connected Sources highlighted; click an Article → open the article link or show a detail drawer. Right now clicks are silently ignored.
7. **Concentric layout is unreadable at full scale** — With 708 articles on the outermost ring, the concentric view is a near-solid band of white dots with no way to distinguish individual nodes. Either the graph should default to Cose (organic) which actually reveals structure, or the Concentric layout should hide Article nodes by default and only show Authors + Sources on the rings.

### 🟡 Medium
8. **No "Fit to screen" / reset zoom button** — After zooming into the graph there's no way to return to the default view except manually scrolling back out. A single "Reset view" button would fix this.
9. **All verification_status values are "pending"** — Every one of the 167 Authors has verification_status: "pending". Either the verification feature isn't implemented yet (in which case the field shouldn't appear), or the pipeline that sets it isn't running. This should be resolved either way.
10. **Credibility scores are flat placeholders** — All 8 Sources have a credibility score of either exactly 0.5 or exactly 0.8. These look like hardcoded defaults rather than computed scores. If the scoring algorithm isn't built yet, the field shouldn't be surfaced to users as if it's meaningful data.
11. **No filtering by date, source, or tag** — Articles have published_at timestamps and tags. There's no way to filter the graph to e.g. "articles from the last 30 days" or "articles tagged فلسطين". Time-based analysis is a core OSINT need.
12. **Graph is slow / freezes on node click** — Clicking nodes in the Concentric layout caused the renderer to freeze for 10–30 seconds. With 890 total nodes and 1,358 edges this is likely a re-layout trigger on click. Whatever event is firing on click needs to be investigated — it may be triggering a full layout recalculation.

### 🟢 Nice-to-Have
13. **No data export** — No way to export the current graph view, a filtered node list, or raw data as CSV/JSON. OSINT workflows typically need to hand data off to analysts.
14. **No navigation beyond the graph** — There are no list views (Authors table, Articles table, Sources table). For example, an analyst who wants to sort authors by article count or filter by source has no way to do that in the current UI.
15. **"Back to dashboard" on login page is misleading** — The link exists pre-login but just loops back to /login. It should either be removed or only shown post-login.
16. **No fit call after layout switches** — When switching from Concentric → Cose, the graph often renders partially off-screen. A cy.fit() call after each layout completes would keep it centered.

## Summary Table

| Priority | Issue | Fix Complexity |
|---|---|---|
| 🔴 | /api/graph unauthenticated | Low |
| 🔴 | Search broken | Medium |
| 🔴 | Node detail panel nearly empty | Medium |
| 🟠 | Source nodes missing from legend | Low |
| 🟠 | Article count mismatch in legend | Low |
| 🟠 | No click action on nodes | Medium |
| 🟠 | Concentric layout unreadable | Medium |
| 🟡 | No reset zoom button | Low |
| 🟡 | All verifications "pending" | Depends on pipeline |
| 🟡 | Flat credibility scores | Depends on algorithm |
| 🟡 | No time/tag filtering | High |
| 🟡 | Graph freezes on click | Medium |

The three critical issues (unauthenticated API, broken search, empty detail panel) should be addressed first — they make the platform either insecure or not functional for actual OSINT work. The rest can follow in order.
