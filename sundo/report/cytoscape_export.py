"""Export Neo4j graph to Cytoscape.js JSON."""
from __future__ import annotations

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
)

logger = logging.getLogger(__name__)


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
        "type(r) AS relationship, r.amount AS amount"
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


def export_graph() -> Path:
    """Export the Neo4j graph as Cytoscape.js JSON to dashboard static dir."""
    DASHBOARD_STATIC.mkdir(parents=True, exist_ok=True)
    out_path = DASHBOARD_STATIC / "graph.json"

    raw_nodes = _neo4j_nodes()
    raw_edges = _neo4j_edges()

    nodes: list[dict[str, Any]] = []
    for n in raw_nodes:
        label = n.get("name") or n.get("handle") or n.get("id") or str(n.get("nid", "node"))
        nodes.append(
            {
                "data": {
                    "id": str(n.get("nid", "")),
                    "label": label,
                    "type": n.get("type", "Unknown"),
                    "credibility_score": n.get("credibility_score"),
                    "fara_linked": bool(n.get("fara_linked", False)),
                }
            }
        )

    edges: list[dict[str, Any]] = []
    for e in raw_edges:
        edges.append(
            {
                "data": {
                    "source": str(e.get("source_id", "")),
                    "target": str(e.get("target_id", "")),
                    "relationship": e.get("relationship", "RELATED"),
                    "amount": e.get("amount"),
                }
            }
        )

    graph = {"nodes": nodes, "edges": edges}
    out_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    logger.info("Graph exported: %s (%d nodes, %d edges)", out_path, len(nodes), len(edges))
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    export_graph()
