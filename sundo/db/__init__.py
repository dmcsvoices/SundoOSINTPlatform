"""Sundo Pi OSINT Monitoring Platform — db package init."""

from sundo.db.sqlite_store import init_db, get_connection

__all__ = ["init_db", "get_connection"]

# Graceful optional import of Neo4j modules
try:
    from sundo.db.neo4j_client import Neo4jClient
    from sundo.db.schema import init_schema
    __all__.extend(["Neo4jClient", "init_schema"])
except ImportError:
    pass
