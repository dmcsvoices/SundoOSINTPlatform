"""Palestinian voice registry: manage PalestinianVoice nodes in Neo4j."""
from __future__ import annotations

import logging
from typing import Any

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

from sundo.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER, SEED_VOICES

logger = logging.getLogger(__name__)


def _normalize_reach_score(
    follower_count: float | None,
    avg_engagement: float | None,
    publication_credibility: float | None,
) -> float:
    """Normalize reach score from weighted components."""
    fc = follower_count or 0.0
    ae = avg_engagement or 0.0
    pc = publication_credibility or 0.0
    # Simple normalization: assume maxima of 1M, 10k, 1.0
    n_fc = min(fc / 1_000_000.0, 1.0)
    n_ae = min(ae / 10_000.0, 1.0)
    return (n_fc * 0.3) + (n_ae * 0.4) + (pc * 0.3)


def _driver() -> Any:
    if GraphDatabase is None:
        raise RuntimeError("neo4j driver not available")
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def ensure_seed_voices() -> None:
    """Create or update seed PalestinianVoice nodes."""
    if GraphDatabase is None:
        logger.warning("Neo4j unavailable; skipping seed voice creation")
        return
    try:
        driver = _driver()
        with driver.session() as session:
            for voice in SEED_VOICES:
                # SEED_VOICES can be dicts or plain strings
                if isinstance(voice, dict):
                    handle = voice.get("handle", "")
                    platform = voice.get("platform", "unknown")
                    focus = voice.get("focus", [])
                else:
                    handle = str(voice)
                    platform = "unknown"
                    focus = []
                session.run(
                    "MERGE (v:PalestinianVoice {handle: $handle}) "
                    "ON CREATE SET v.name = $handle, v.platform = $platform, "
                    "v.content_focus = $focus, v.verification_status = 'verified', "
                    "v.reach_score = 0.0, v.follower_count = 0, v.avg_engagement = 0, "
                    "v.publication_credibility = 0.0, v.created_at = datetime() "
                    "ON MATCH SET v.updated_at = datetime()",
                    {
                        "handle": handle,
                        "platform": platform,
                        "focus": focus,
                    },
                )
        driver.close()
        logger.info("Seed voices ensured: %d voices", len(SEED_VOICES))
    except Exception as exc:
        logger.warning("Could not ensure seed voices: %s", exc)


def update_voice(
    handle: str,
    name: str | None = None,
    follower_count: float | None = None,
    avg_engagement: float | None = None,
    publication_credibility: float | None = None,
    verification_status: str | None = None,
) -> None:
    """Update a PalestinianVoice node and recalculate its reach score."""
    if GraphDatabase is None:
        logger.warning("Neo4j unavailable; skipping voice update")
        return
    reach = _normalize_reach_score(follower_count, avg_engagement, publication_credibility)
    try:
        driver = _driver()
        with driver.session() as session:
            session.run(
                "MATCH (v:PalestinianVoice {handle: $handle}) "
                "SET v.reach_score = $reach, v.updated_at = datetime() "
                "FOREACH (n IN CASE WHEN $name IS NOT NULL THEN [$name] ELSE [] END | "
                "  SET v.name = n) "
                "FOREACH (fc IN CASE WHEN $follower_count IS NOT NULL THEN [$follower_count] ELSE [] END | "
                "  SET v.follower_count = fc) "
                "FOREACH (ae IN CASE WHEN $avg_engagement IS NOT NULL THEN [$avg_engagement] ELSE [] END | "
                "  SET v.avg_engagement = ae) "
                "FOREACH (pc IN CASE WHEN $publication_credibility IS NOT NULL THEN [$publication_credibility] ELSE [] END | "
                "  SET v.publication_credibility = pc) "
                "FOREACH (vs IN CASE WHEN $verification_status IS NOT NULL THEN [$verification_status] ELSE [] END | "
                "  SET v.verification_status = vs)",
                {
                    "handle": handle,
                    "reach": reach,
                    "name": name,
                    "follower_count": follower_count,
                    "avg_engagement": avg_engagement,
                    "publication_credibility": publication_credibility,
                    "verification_status": verification_status,
                },
            )
        driver.close()
        logger.info("Updated voice: %s (reach=%.4f)", handle, reach)
    except Exception as exc:
        logger.warning("Could not update voice %s: %s", handle, exc)


def add_from_rss_byline(handle: str, byline_name: str) -> None:
    """Auto-add a PalestinianVoice from an RSS byline with pending verification."""
    if GraphDatabase is None:
        logger.warning("Neo4j unavailable; skipping RSS byline voice")
        return
    try:
        driver = _driver()
        with driver.session() as session:
            session.run(
                "MERGE (v:PalestinianVoice {handle: $handle}) "
                "ON CREATE SET v.name = $byline_name, v.verification_status = 'pending', "
                "v.reach_score = 0.0, v.follower_count = 0, v.avg_engagement = 0, "
                "v.publication_credibility = 0.0, v.created_at = datetime(), "
                "v.source = 'rss_byline' "
                "ON MATCH SET v.updated_at = datetime()",
                {"handle": handle, "byline_name": byline_name},
            )
        driver.close()
        logger.info("Added/updated RSS byline voice: %s", handle)
    except Exception as exc:
        logger.warning("Could not add byline voice %s: %s", handle, exc)


def list_voices(limit: int = 100) -> list[dict[str, Any]]:
    """List PalestinianVoice nodes ordered by reach_score."""
    if GraphDatabase is None:
        return []
    try:
        driver = _driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (v:PalestinianVoice) RETURN v.handle AS handle, v.name AS name, "
                "v.reach_score AS reach_score, v.verification_status AS verification_status "
                "ORDER BY v.reach_score DESC LIMIT $limit",
                {"limit": limit},
            )
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Could not list voices: %s", exc)
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    ensure_seed_voices()
