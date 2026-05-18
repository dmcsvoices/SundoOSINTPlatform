"""Counter-narrative briefing generator."""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

try:
    from neo4j import GraphDatabase
except Exception:
    GraphDatabase = None  # type: ignore[misc,assignment]

from sundo.config import BRIEFINGS_DIR, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

logger = logging.getLogger(__name__)


def _neo4j_driver() -> Any:
    if GraphDatabase is None:
        raise RuntimeError("neo4j driver not available")
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _fetch_event(event_id: str) -> dict[str, Any] | None:
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (e:CoordinationEvent {id: $id}) RETURN e", {"id": event_id}
            )
            record = result.single()
            if record:
                return dict(record["e"].items())
        driver.close()
    except Exception as exc:
        logger.warning("Could not fetch event %s: %s", event_id, exc)
    return None


def _fetch_accounts(event_id: str) -> list[dict[str, Any]]:
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (e:CoordinationEvent {id: $id})-[:INVOLVES]->(a) "
                "RETURN a.handle AS handle, a.name AS name, a.platform AS platform",
                {"id": event_id},
            )
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Could not fetch accounts for event %s: %s", event_id, exc)
        return []


def _fetch_palestinian_voices() -> list[dict[str, Any]]:
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (v:PalestinianVoice) WHERE v.verification_status = 'verified' "
                "RETURN v.handle AS handle, v.name AS name, v.reach_score AS reach_score "
                "ORDER BY v.reach_score DESC LIMIT 5"
            )
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Could not fetch Palestinian voices: %s", exc)
        return []


def _fetch_fara_links(handles: list[str]) -> list[dict[str, Any]]:
    if not handles:
        return []
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (a)-[:LINKED_TO]->(f:FARAFiling) WHERE a.handle IN $handles "
                "RETURN a.handle AS handle, f.registrant_name AS registrant, "
                "f.foreign_principal_country AS country LIMIT 10",
                {"handles": handles},
            )
            rows = [r.data() for r in result]
        driver.close()
        return rows
    except Exception as exc:
        logger.warning("Could not fetch FARA links: %s", exc)
        return []


def generate_briefing(event_id: str) -> Path:
    """Generate a counter-narrative briefing for a CoordinationEvent."""
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    out_path = BRIEFINGS_DIR / f"{today}-{event_id}.md"

    event = _fetch_event(event_id)
    if event is None:
        logger.warning("Event %s not found; skipping briefing.", event_id)
        return out_path

    accounts = _fetch_accounts(event_id)
    handles = [a["handle"] for a in accounts if a.get("handle")]
    voices = _fetch_palestinian_voices()
    fara_links = _fetch_fara_links(handles)

    lines: list[str] = [
        f"# Counter-Narrative Briefing — {event_id}",
        "",
        f"**Date:** {today}  ",
        f"**Pattern:** {event.get('pattern_type', 'unknown')}  ",
        f"**Detected:** {event.get('detected_at', 'n/a')}  ",
        "",
        "## Campaign Claims",
        "",
        "_Claims detected in the coordination event:_",
        "",
    ]
    for a in accounts:
        lines.append(f"- **{a.get('handle', 'unknown')}** ({a.get('platform', 'unknown')})")
    if not accounts:
        lines.append("_No accounts linked to this event._")
    lines.extend(["", "## Documented Sources", "", "_Awaiting manual curation._", ""])

    lines.extend(["## Palestinian Voices", ""])
    if voices:
        for v in voices:
            lines.append(f"- **{v.get('handle', 'unknown')}** — reach score {v.get('reach_score', 'n/a')}")
    else:
        lines.append("_No verified voices available._")
    lines.append("")

    lines.extend(["## Evidence of Coordination", "", f"- Event ID: `{event_id}`", "- Linked accounts listed above.", ""])

    lines.extend(["## FARA / Funding Links", ""])
    if fara_links:
        for f in fara_links:
            lines.append(
                f"- **{f.get('handle', 'unknown')}** linked to registrant "
                f"`{f.get('registrant', 'n/a')}` ({f.get('country', 'n/a')})"
            )
    else:
        lines.append("_No FARA links detected._")
    lines.append("")

    lines.extend([
        "## Suggested Amplification",
        "",
        "1. Share documented counter-narratives with the Palestinian voices above.",
        "2. Highlight inconsistencies in campaign claims using primary sources.",
        "3. Flag FARA-linked accounts for transparency and public accountability.",
        "",
    ])

    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Briefing written: %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    # Example usage: generate_briefing("evt-2026-05-17-001")
