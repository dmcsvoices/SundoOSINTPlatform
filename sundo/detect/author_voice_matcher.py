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
from typing import Optional

try:
    from sundo.db.neo4j_client import Neo4jClient
except Exception:
    Neo4jClient = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 0.80


def run(neo4j_client=None) -> dict:
    """
    Run author-voice matching pass.
    Returns summary dict with match counts.
    """
    if Neo4jClient is None or neo4j_client is None:
        logger.warning("Neo4jClient unavailable; skipping author-voice matching")
        return {"exact_matches": 0, "fuzzy_candidates": 0, "no_match": 0}

    if not neo4j_client.is_available():
        logger.warning("Neo4j not available; skipping author-voice matching")
        return {"exact_matches": 0, "fuzzy_candidates": 0, "no_match": 0}

    authors = get_unmatched_authors(neo4j_client)
    voices = get_all_voices(neo4j_client)

    results = {
        "exact_matches": 0,
        "fuzzy_candidates": 0,
        "no_match": 0,
    }

    for author in authors:
        match_type, voice_handle = find_match(author, voices)

        if match_type == "EXACT":
            try:
                neo4j_client.link_author_to_voice(author["id"], voice_handle)
                neo4j_client.set_author_linked_voice(author["id"], voice_handle)
                logger.info(
                    "Exact match: Author %s ↔ PalestinianVoice %s",
                    author["handle"], voice_handle,
                )
                results["exact_matches"] += 1
            except Exception as exc:
                logger.warning("Failed to link exact match %s: %s", author["id"], exc)

        elif match_type == "FUZZY":
            try:
                neo4j_client.flag_author_for_review(
                    author["id"], voice_handle, "fuzzy_voice_match"
                )
                logger.info(
                    "Fuzzy candidate: Author %s ~ PalestinianVoice %s (needs review)",
                    author["handle"], voice_handle,
                )
                results["fuzzy_candidates"] += 1
            except Exception as exc:
                logger.warning("Failed to flag fuzzy candidate %s: %s", author["id"], exc)

        else:
            results["no_match"] += 1

    logger.info(
        "Author-voice matching: %d exact, %d fuzzy, %d unmatched",
        results["exact_matches"],
        results["fuzzy_candidates"],
        results["no_match"],
    )
    return results


def find_match(author: dict, voices: list[dict]) -> tuple[str, Optional[str]]:
    """
    Attempt to match a single author to a voice.
    Returns (match_type, voice_handle) or ('NONE', None).
    """
    author_handle = author.get("handle", "").lower()

    for voice in voices:
        voice_handle = voice.get("handle", "").lower()

        # Exact handle match
        if author_handle == voice_handle:
            return "EXACT", voice["handle"]

        # Fuzzy match on handle
        ratio = SequenceMatcher(None, author_handle, voice_handle).ratio()
        if ratio >= FUZZY_THRESHOLD:
            return "FUZZY", voice["handle"]

    return "NONE", None


def get_unmatched_authors(neo4j_client) -> list[dict]:
    with neo4j_client.driver.session() as session:
        result = session.run("""
            MATCH (a:Author)
            WHERE a.linked_voice_id IS NULL
            RETURN a.id AS id, a.handle AS handle,
                   a.display_name AS display_name
        """)
        return [dict(r) for r in result]


def get_all_voices(neo4j_client) -> list[dict]:
    with neo4j_client.driver.session() as session:
        result = session.run("""
            MATCH (v:PalestinianVoice)
            RETURN v.handle AS handle, v.reach_score AS reach_score
        """)
        return [dict(r) for r in result]
