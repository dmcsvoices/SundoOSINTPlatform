"""Sundo Pi OSINT Monitoring Platform — Neo4j graph client."""

import logging
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase, basic_auth
from neo4j.exceptions import ServiceUnavailable, AuthError

from sundo.config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD

logger = logging.getLogger("sundo.db.neo4j")


class Neo4jClient:
    """Thin wrapper around the Neo4j Python driver with graceful degradation."""

    def __init__(
        self,
        uri: str = NEO4J_URI,
        user: str = NEO4J_USER,
        password: str = NEO4J_PASSWORD,
    ):
        self.uri = uri
        self.user = user
        self.password = password
        self._driver: Optional[Any] = None
        self._available = False
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        try:
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=basic_auth(self.user, self.password),
            )
            # Verify connectivity once at init
            self._driver.verify_connectivity()
            self._available = True
            logger.info("Neo4j connected: %s", self.uri)
        except (ServiceUnavailable, AuthError) as exc:
            self._available = False
            logger.error("Neo4j unavailable (%s): %s", type(exc).__name__, exc)
        except Exception:
            self._available = False
            logger.exception("Unexpected Neo4j connection error")

    @property
    def driver(self) -> Optional[Any]:
        """Expose the underlying Neo4j driver for direct access."""
        return self._driver

    def close(self) -> None:
        if self._driver:
            self._driver.close()
            self._driver = None
            self._available = False

    def is_available(self) -> bool:
        if not self._available or not self._driver:
            return False
        try:
            self._driver.verify_connectivity()
            return True
        except Exception:
            self._available = False
            return False

    def _run(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        database: str = "neo4j",
    ) -> Optional[Any]:
        if not self.is_available():
            logger.warning("Neo4j not available; skipping query: %s", query[:60])
            return None
        try:
            with self._driver.session(database=database) as session:
                return session.run(query, parameters or {})
        except Exception:
            logger.exception("Neo4j query failed: %s", query[:80])
            return None

    # ------------------------------------------------------------------
    # Person / Org nodes
    # ------------------------------------------------------------------

    def upsert_person(
        self,
        handle: str,
        name: Optional[str] = None,
        platform: str = "unknown",
        bio: Optional[str] = None,
        follower_count: Optional[int] = None,
        verified: bool = False,
        labels: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create or update a :Person node keyed by handle."""
        extra_labels = ":" + ":".join(labels) if labels else ""
        result = self._run(
            f"""
            MERGE (p:Person{extra_labels} {{handle: $handle}})
            ON CREATE SET p.created_at = datetime(),
                          p.name = $name,
                          p.platform = $platform,
                          p.bio = $bio,
                          p.follower_count = $follower_count,
                          p.verified = $verified
            ON MATCH  SET p.updated_at = datetime(),
                          p.name = coalesce($name, p.name),
                          p.platform = coalesce($platform, p.platform),
                          p.bio = coalesce($bio, p.bio),
                          p.follower_count = coalesce($follower_count, p.follower_count),
                          p.verified = coalesce($verified, p.verified)
            RETURN p
            """,
            {
                "handle": handle,
                "name": name,
                "platform": platform,
                "bio": bio,
                "follower_count": follower_count,
                "verified": verified,
            },
        )
        if result:
            return result.single()
        return None

    def upsert_org(
        self,
        name: str,
        ein: Optional[str] = None,
        org_type: str = "unknown",
        country: Optional[str] = None,
        fara_registration_id: Optional[str] = None,
        flags: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create or update an :Organization node keyed by EIN."""
        result = self._run(
            """
            MERGE (o:Organization {ein: $ein})
            ON CREATE SET o.created_at = datetime(),
                          o.name = $name,
                          o.org_type = $org_type,
                          o.country = $country,
                          o.fara_registration_id = $fara_registration_id,
                          o.flags = $flags
            ON MATCH  SET o.updated_at = datetime(),
                          o.name = coalesce($name, o.name),
                          o.org_type = coalesce($org_type, o.org_type),
                          o.country = coalesce($country, o.country),
                          o.fara_registration_id = coalesce($fara_registration_id, o.fara_registration_id),
                          o.flags = coalesce($flags, o.flags)
            RETURN o
            """,
            {
                "name": name,
                "ein": ein or name,
                "org_type": org_type,
                "country": country,
                "fara_registration_id": fara_registration_id,
                "flags": flags or [],
            },
        )
        if result:
            return result.single()
        return None

    def link_funding(
        self,
        source_ein: str,
        target_handle: str,
        amount_usd: Optional[float] = None,
        period: Optional[str] = None,
        filing_source: str = "unknown",
        evidence_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a (Organization)-[:FUNDED]->(Person) relationship."""
        result = self._run(
            """
            MATCH (o:Organization {ein: $source_ein})
            MATCH (p:Person {handle: $target_handle})
            MERGE (o)-[r:FUNDED]->(p)
            ON CREATE SET r.created_at = datetime(),
                          r.amount_usd = $amount_usd,
                          r.period = $period,
                          r.filing_source = $filing_source,
                          r.evidence_url = $evidence_url
            ON MATCH  SET r.updated_at = datetime(),
                          r.amount_usd = coalesce($amount_usd, r.amount_usd),
                          r.period = coalesce($period, r.period),
                          r.filing_source = coalesce($filing_source, r.filing_source),
                          r.evidence_url = coalesce($evidence_url, r.evidence_url)
            RETURN r
            """,
            {
                "source_ein": source_ein,
                "target_handle": target_handle,
                "amount_usd": amount_usd,
                "period": period,
                "filing_source": filing_source,
                "evidence_url": evidence_url,
            },
        )
        if result:
            return result.single()
        return None

    def record_coordination_event(
        self,
        event_id: str,
        trigger_type: str,
        account_handles: List[str],
        hashtags: Optional[List[str]] = None,
        similarity_score: Optional[float] = None,
        time_window_minutes: Optional[int] = None,
        status: str = "open",
    ) -> Optional[Dict[str, Any]]:
        """Create a CoordinationEvent node and link to involved Persons."""
        result = self._run(
            """
            CREATE (e:CoordinationEvent {
                event_id: $event_id,
                trigger_type: $trigger_type,
                detected_at: datetime(),
                hashtags: $hashtags,
                similarity_score: $similarity_score,
                time_window_minutes: $time_window_minutes,
                status: $status
            })
            WITH e
            UNWIND $account_handles AS handle
            MERGE (p:Person {handle: handle})
            MERGE (p)-[:PARTICIPATED_IN]->(e)
            RETURN e
            """,
            {
                "event_id": event_id,
                "trigger_type": trigger_type,
                "hashtags": hashtags or [],
                "similarity_score": similarity_score,
                "time_window_minutes": time_window_minutes,
                "status": status,
                "account_handles": account_handles,
            },
        )
        if result:
            return result.single()
        return None

    def get_voice_registry(
        self,
        min_credibility: float = 0.0,
        verification_status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return PalestinianVoice nodes sorted by reach_score."""
        query = """
            MATCH (v:PalestinianVoice)
            WHERE v.credibility_score >= $min_credibility
        """
        if verification_status:
            query += " AND v.verification_status = $verification_status"
        query += """
            RETURN v.handle AS handle,
                   v.platform AS platform,
                   v.reach_score AS reach_score,
                   v.credibility_score AS credibility_score,
                   v.verification_status AS verification_status,
                   v.content_focus AS content_focus
            ORDER BY v.reach_score DESC
        """
        result = self._run(
            query,
            {
                "min_credibility": min_credibility,
                "verification_status": verification_status,
            },
        )
        if result:
            return [dict(r) for r in result]
        return []

    def create_palestinian_voice(
        self,
        handle: str,
        platform: str = "unknown",
        reach_score: float = 0.0,
        credibility_score: float = 0.5,
        verification_status: str = "pending",
        content_focus: Optional[List[str]] = None,
        language: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create or update a PalestinianVoice node."""
        result = self._run(
            """
            MERGE (v:PalestinianVoice {handle: $handle})
            ON CREATE SET v.created_at = datetime(),
                          v.platform = $platform,
                          v.reach_score = $reach_score,
                          v.credibility_score = $credibility_score,
                          v.verification_status = $verification_status,
                          v.content_focus = $content_focus,
                          v.language = $language,
                          v.last_active = datetime()
            ON MATCH  SET v.updated_at = datetime(),
                          v.platform = coalesce($platform, v.platform),
                          v.reach_score = coalesce($reach_score, v.reach_score),
                          v.credibility_score = coalesce($credibility_score, v.credibility_score),
                          v.verification_status = coalesce($verification_status, v.verification_status),
                          v.content_focus = coalesce($content_focus, v.content_focus),
                          v.language = coalesce($language, v.language),
                          v.last_active = datetime()
            RETURN v
            """,
            {
                "handle": handle,
                "platform": platform,
                "reach_score": reach_score,
                "credibility_score": credibility_score,
                "verification_status": verification_status,
                "content_focus": content_focus or [],
                "language": language,
            },
        )
        if result:
            return result.single()
        return None

    def link_voice_to_org(
        self,
        voice_handle: str,
        org_ein: str,
        relationship: str = "AFFILIATED_WITH",
    ) -> Optional[Dict[str, Any]]:
        """Link a PalestinianVoice to an Organization."""
        result = self._run(
            f"""
            MATCH (v:PalestinianVoice {{handle: $voice_handle}})
            MATCH (o:Organization {{ein: $org_ein}})
            MERGE (v)-[r:{relationship}]->(o)
            ON CREATE SET r.created_at = datetime()
            RETURN r
            """,
            {
                "voice_handle": voice_handle,
                "org_ein": org_ein,
            },
        )
        if result:
            return result.single()
        return None
