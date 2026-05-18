"""Push alert engine via ntfy.sh."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import requests

from sundo.config import (
    COORDINATION_EVENT_MIN_ACCOUNTS,
    FTC_PAYMENT_THRESHOLD,
    NTFY_TOPIC,
    NTFY_URL,
)

logger = logging.getLogger(__name__)


def _send_ntfy(title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    """POST an alert to ntfy.sh; return True on success."""
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = tags
    try:
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
        resp.raise_for_status()
        logger.info("ntfy alert sent: %s", title)
        return True
    except Exception as exc:
        logger.warning("ntfy unavailable, alert dropped: %s — %s", title, exc)
        return False


def alert_coordination_event(event: dict[str, Any]) -> bool:
    """Alert when a coordination event involves >= threshold accounts."""
    count = event.get("account_count", 0) or 0
    if count < COORDINATION_EVENT_MIN_ACCOUNTS:
        return False
    title = f"Coordination Event: {count} accounts"
    msg = (
        f"Pattern: {event.get('pattern_type', 'unknown')}\n"
        f"Detected: {event.get('detected_at', 'n/a')}\n"
        f"Event ID: {event.get('id', 'n/a')}"
    )
    return _send_ntfy(title, msg, priority="high", tags="warning")


def alert_new_fara(filing: dict[str, Any]) -> bool:
    """Alert when a new FARA filing matches a seed organization."""
    seed_org = filing.get("registrant_name", "")
    title = f"New FARA Filing: {seed_org}"
    msg = (
        f"Registrant: {seed_org}\n"
        f"Filed: {filing.get('filed_at', 'n/a')}\n"
        f"Country: {filing.get('foreign_principal_country', 'n/a')}"
    )
    return _send_ntfy(title, msg, priority="default", tags="document")


def alert_ftc_violation(violation: dict[str, Any]) -> bool:
    """Alert when an FTC violation has a payment > threshold."""
    amount = violation.get("amount", 0.0) or 0.0
    if amount < FTC_PAYMENT_THRESHOLD:
        return False
    handle = violation.get("handle", "unknown")
    title = f"FTC Violation: ${amount:,.0f}"
    msg = (
        f"Handle: {handle}\n"
        f"Amount: ${amount:,.2f}\n"
        f"Nature: {violation.get('nature', 'n/a')}\n"
        f"Status: {violation.get('status', 'n/a')}"
    )
    return _send_ntfy(title, msg, priority="high", tags="money_with_wings")


def check_and_alert() -> None:
    """Run all immediate alert checks."""
    # Coordination events from the last hour
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
    try:
        from neo4j import GraphDatabase

        from sundo.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            result = session.run(
                "MATCH (e:CoordinationEvent) WHERE e.detected_at >= $since RETURN e",
                {"since": since},
            )
            for record in result:
                event = dict(record["e"].items())
                alert_coordination_event(event)
        driver.close()
    except Exception as exc:
        logger.warning("Could not check coordination events for alerts: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    check_and_alert()
