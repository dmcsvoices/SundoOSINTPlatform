"""Sundo Pi OSINT Monitoring Platform — Network graph enrichment.

Queries Neo4j to identify suspicious patterns and writes an enriched
``credibility_score`` (0–1 float, lower = more suspicious) back to
:Person nodes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sundo.db import neo4j_client

logger = logging.getLogger("sundo.detect.network")


def _get_db() -> neo4j_client.Neo4jClient:
    """Return a fresh Neo4j client instance."""
    return neo4j_client.Neo4jClient()


def _find_repeat_offenders(min_events: int = 2) -> List[Dict[str, Any]]:
    """Return :Person handles that appear in >= *min_events* CoordinationEvents.

    Each dict contains ``handle`` and ``event_count``.
    """
    db = _get_db()
    if not db.is_available():
        logger.warning("Neo4j unavailable; skipping repeat-offender query")
        return []

    try:
        result = db._run(
            """
            MATCH (p:Person)-[:PARTICIPATES_IN]->(e:CoordinationEvent)
            WITH p.handle AS handle, count(DISTINCT e) AS event_count
            WHERE event_count >= $min_events
            RETURN handle, event_count
            ORDER BY event_count DESC
            """,
            {"min_events": min_events},
        )
        if not result:
            return []
        return [dict(r) for r in result]
    except Exception:
        logger.exception("Neo4j query failed: repeat offenders")
        return []


def _find_funded_accounts() -> List[Dict[str, Any]]:
    """Return :Person handles with FUNDED_BY edges to FARA-registered orgs.

    Expects :Person nodes to carry a ``funded_by`` relationship or a
    documented-payment property.  In the current schema we look for any
    :Person connected to an :Organization that carries a ``FARA`` label.
    """
    db = _get_db()
    if not db.is_available():
        logger.warning("Neo4j unavailable; skipping funded-accounts query")
        return []

    try:
        result = db._run(
            """
            MATCH (p:Person)-[:FUNDED_BY]->(o:Organization:FARA)
            RETURN DISTINCT p.handle AS handle, o.name AS org_name,
                   o.ein AS org_ein
            """
        )
        if not result:
            return []
        return [dict(r) for r in result]
    except Exception:
        logger.exception("Neo4j query failed: funded accounts")
        return []


def _find_coordination_and_payment() -> List[Dict[str, Any]]:
    """Return :Person handles that BOTH participate in CoordinationEvents
    AND have documented payments (via :FUNDED_BY or payment properties).
    """
    db = _get_db()
    if not db.is_available():
        logger.warning("Neo4j unavailable; skipping coordination+payment query")
        return []

    try:
        result = db._run(
            """
            MATCH (p:Person)-[:PARTICIPATES_IN]->(:CoordinationEvent)
            WITH DISTINCT p
            MATCH (p)-[:FUNDED_BY|RECEIVED_PAYMENT]->(o:Organization)
            RETURN p.handle AS handle, collect(DISTINCT o.name) AS org_names
            """
        )
        if not result:
            return []
        return [dict(r) for r in result]
    except Exception:
        logger.exception("Neo4j query failed: coordination + payment overlap")
        return []


def _compute_credibility_score(
    event_count: int = 0,
    is_funded: bool = False,
    is_coordination_funded: bool = False,
) -> float:
    """Compute a 0–1 credibility score (lower = more suspicious).

    Heuristic:
        - Base score: 1.0 (fully credible)
        - −0.15 per coordination event (capped at −0.6)
        - −0.2 if funded by a FARA org
        - −0.25 if both coordination AND funding overlap
    """
    score = 1.0
    penalty = min(0.15 * event_count, 0.6)
    score -= penalty
    if is_funded:
        score -= 0.20
    if is_coordination_funded:
        score -= 0.25
    return max(0.0, min(1.0, score))


def _write_credibility(
    handle: str,
    score: float,
    reasons: List[str],
) -> bool:
    """Write *score* and *reasons* back to the :Person node in Neo4j."""
    db = _get_db()
    if not db.is_available():
        logger.warning("Neo4j unavailable; cannot write credibility for %s", handle)
        return False

    try:
        result = db._run(
            """
            MATCH (p:Person {handle: $handle})
            SET p.credibility_score = $score,
                p.credibility_reasons = $reasons,
                p.credibility_updated_at = datetime()
            RETURN p.handle AS handle
            """,
            {"handle": handle, "score": score, "reasons": reasons},
        )
        if result and result.single():
            logger.info("Updated credibility for %s → %.2f", handle, score)
            return True
        return False
    except Exception:
        logger.exception("Failed to write credibility for %s", handle)
        return False


def run() -> int:
    """Run graph enrichment and return the number of nodes updated.

    Steps:
        1. Identify repeat offenders (multiple CoordinationEvents).
        2. Identify FARA-funded accounts.
        3. Identify overlap (coordination + payment).
        4. Compute and write credibility scores.
    """
    updated = 0

    # --- Step 1: repeat offenders ---
    offenders = _find_repeat_offenders(min_events=2)
    offender_map: Dict[str, int] = {o["handle"]: o["event_count"] for o in offenders}
    logger.info("Found %d repeat offender(s)", len(offenders))

    # --- Step 2: funded accounts ---
    funded = _find_funded_accounts()
    funded_set: set = {f["handle"] for f in funded}
    logger.info("Found %d FARA-funded account(s)", len(funded))

    # --- Step 3: coordination + payment overlap ---
    overlap = _find_coordination_and_payment()
    overlap_set: set = {o["handle"] for o in overlap}
    logger.info("Found %d overlap account(s)", len(overlap))

    # --- Step 4: compute + write scores ---
    all_handles = set(offender_map.keys()) | funded_set | overlap_set
    for handle in all_handles:
        event_count = offender_map.get(handle, 0)
        is_funded = handle in funded_set
        is_coordination_funded = handle in overlap_set

        score = _compute_credibility_score(
            event_count=event_count,
            is_funded=is_funded,
            is_coordination_funded=is_coordination_funded,
        )

        reasons: List[str] = []
        if event_count >= 2:
            reasons.append(f"participated in {event_count} coordination events")
        if is_funded:
            reasons.append("funded by FARA-registered organisation")
        if is_coordination_funded:
            reasons.append("coordination patterns with documented payments")

        if _write_credibility(handle, score, reasons):
            updated += 1

    logger.info("Graph enrichment complete: %d node(s) updated", updated)
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    count = run()
    print(f"Network graph enrichment complete: {count} node(s) updated.")
