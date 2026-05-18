"""Sundo Pi OSINT Monitoring Platform — Neo4j schema / constraints."""

import logging

from neo4j.exceptions import ServiceUnavailable

from sundo.db.neo4j_client import Neo4jClient

logger = logging.getLogger("sundo.db.schema")


# Constraints expected on the graph
_CONSTRAINTS = [
    "CREATE CONSTRAINT person_handle IF NOT EXISTS FOR (p:Person) REQUIRE p.handle IS UNIQUE",
    "CREATE CONSTRAINT org_ein IF NOT EXISTS FOR (o:Organization) REQUIRE o.ein IS UNIQUE",
    "CREATE CONSTRAINT post_post_id IF NOT EXISTS FOR (post:Post) REQUIRE post.post_id IS UNIQUE",
    "CREATE CONSTRAINT event_uuid IF NOT EXISTS FOR (e:CoordinationEvent) REQUIRE e.event_uuid IS UNIQUE",
]


# Indexes for performance
_INDEXES = [
    "CREATE INDEX person_platform IF NOT EXISTS FOR (p:Person) ON (p.platform)",
    "CREATE INDEX org_name IF NOT EXISTS FOR (o:Organization) ON (o.name)",
    "CREATE INDEX post_time IF NOT EXISTS FOR (post:Post) ON (post.posted_at)",
]


def init_schema(client: Neo4jClient) -> None:
    """Apply constraints and indexes once at application startup."""
    if not client.is_available():
        logger.warning("Neo4j unavailable; skipping schema init.")
        return

    for cypher in _CONSTRAINTS + _INDEXES:
        try:
            result = client._run(cypher)
            if result:
                logger.info("Applied: %s", cypher[:60])
        except ServiceUnavailable as exc:
            logger.error("Neo4j service unavailable during schema init: %s", exc)
            break
        except Exception:
            # Constraints already exist etc. are swallowed gracefully
            logger.debug("Schema statement may already exist: %s", cypher[:60])

    logger.info("Neo4j schema initialisation complete.")
