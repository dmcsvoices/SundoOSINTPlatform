"""ProPublica Nonprofit Explorer API monitor for Sundo Pi OSINT platform.

Fetches IRS 990 data for target organizations and flags hasbara-related
program descriptions and grants.
"""

import json
import logging
import time
from typing import Any

import requests

from sundo.config import BASE_DIR, HASBARA_KEYWORDS, LOG_FORMAT, LOG_LEVEL, MAX_RETRIES, RETRY_BACKOFF, SEED_ORGS
from sundo.db.sqlite_store import init_db, get_connection

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("irs990_monitor")

API_BASE = "https://projects.propublica.org/nonprofits/api/v2"
SEARCH_URL = f"{API_BASE}/search.json"
ORG_URL = f"{API_BASE}/organizations/{{ein}}.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux arm64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _fetch(url: str) -> dict[str, Any] | None:
    """Fetch JSON from the ProPublica API with retries.

    Args:
        url: URL to fetch.

    Returns:
        Parsed JSON dict or None if all retries failed.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 429:
                logger.warning("Rate limited by ProPublica API (attempt %d/%d)", attempt + 1, MAX_RETRIES)
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("Request failed (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
        except json.JSONDecodeError as exc:
            logger.error("JSON decode error for %s: %s", url, exc)
            break
    logger.error("All retries exhausted for %s", url)
    return None


def _flag_hasbara(text: str) -> list[str]:
    """Check text for hasbara-related keywords.

    Args:
        text: Program description or mission text.

    Returns:
        List of matched keywords (lowercase).
    """
    if not text:
        return []
    text_lower = text.lower()
    matched = [kw for kw in HASBARA_KEYWORDS if kw.lower() in text_lower]
    return matched


def search_org(org_name: str) -> list[dict[str, Any]]:
    """Search ProPublica Nonprofit Explorer for organizations by name.

    Args:
        org_name: Organization name to search.

    Returns:
        List of organization summary dicts (may contain EIN, name, state, etc.).
    """
    logger.info("Searching IRS 990 for: %s", org_name)
    url = f"{SEARCH_URL}?q={requests.utils.quote(org_name)}"
    data = _fetch(url)
    if data is None:
        return []

    organizations = data.get("organizations", [])
    # Filter to orgs whose name closely matches the search term
    results = []
    search_lower = org_name.lower()
    for org in organizations:
        name = (org.get("name") or "").lower()
        if search_lower in name or name in search_lower:
            results.append(org)
    logger.info("Found %d matching orgs for '%s'", len(results), org_name)
    return results


def fetch_org_details(ein: str) -> dict[str, Any] | None:
    """Fetch full organization details including filings.

    Args:
        ein: Employer Identification Number.

    Returns:
        Organization details dict or None.
    """
    url = ORG_URL.format(ein=ein)
    return _fetch(url)


def parse_filing(filing: dict[str, Any]) -> dict[str, Any] | None:
    """Extract relevant fields from a single IRS 990 filing.

    Args:
        filing: Raw filing dict from ProPublica API.

    Returns:
        Parsed filing dict or None if insufficient data.
    """
    if not filing:
        return None

    revenue = filing.get("totrevenue")
    expenses = filing.get("totfuncexpns")
    assets = filing.get("totassetsend")

    # Normalize to integers if possible
    def _to_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    program_desc = filing.get("mission") or filing.get("desc") or filing.get("purpcdeddesc") or ""
    flags = _flag_hasbara(program_desc)

    return {
        "revenue": _to_int(revenue),
        "expenses": _to_int(expenses),
        "assets": _to_int(assets),
        "program_descriptions": program_desc,
        "hasbara_flags": json.dumps(flags) if flags else None,
    }


def extract_grants(filing: dict[str, Any], ein: str, tax_year: int) -> list[dict[str, Any]]:
    """Extract grant payments from a filing.

    Args:
        filing: Raw filing dict.
        ein: Organization EIN.
        tax_year: Tax year of the filing.

    Returns:
        List of grant dicts.
    """
    grants: list[dict[str, Any]] = []

    # ProPublica API may nest grants under different keys depending on form version
    grant_data = filing.get("grants") or filing.get("contributions") or []
    if not isinstance(grant_data, list):
        grant_data = [grant_data] if grant_data else []

    for g in grant_data:
        if not isinstance(g, dict):
            continue
        amount = g.get("amount") or g.get("amt") or g.get("cash_grant")
        try:
            amount_int = int(amount) if amount is not None else None
        except (ValueError, TypeError):
            amount_int = None

        grants.append(
            {
                "org_ein": ein,
                "grantee_name": g.get("name") or g.get("grantee") or "",
                "grantee_ein": g.get("ein") or g.get("grantee_ein") or "",
                "amount": amount_int,
                "purpose": g.get("purpose") or g.get("description") or "",
                "tax_year": tax_year,
            }
        )

    return grants


def save_org(org: dict[str, Any], details: dict[str, Any] | None) -> None:
    """Persist organization and latest filing data to SQLite.

    Args:
        org: Organization summary dict (must contain 'ein', 'name').
        details: Full organization details dict or None.
    """
    conn = get_connection()
    try:
        ein = org.get("ein", "")
        name = org.get("name", "")
        state = org.get("state") or org.get("statecd", "")
        city = org.get("city", "")

        total_revenue = None
        total_assets = None
        tax_year = None
        raw_json = None

        if details:
            filings = details.get("filings_with_data", []) or details.get("filings", [])
            if filings:
                latest = filings[0]
                parsed = parse_filing(latest)
                if parsed:
                    total_revenue = parsed["revenue"]
                    total_assets = parsed["assets"]
                tax_year = latest.get("tax_prd_yr") or latest.get("tax_year")
                raw_json = json.dumps(details, default=str)

        conn.execute(
            """
            INSERT INTO irs990_orgs (ein, name, city, state, total_revenue, total_assets, tax_year, filed_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(ein) DO UPDATE SET
                name=excluded.name,
                city=excluded.city,
                state=excluded.state,
                total_revenue=excluded.total_revenue,
                total_assets=excluded.total_assets,
                tax_year=excluded.tax_year,
                filed_at=CURRENT_TIMESTAMP,
                raw_json=excluded.raw_json
            """,
            (ein, name, city, state, total_revenue, total_assets, tax_year, raw_json),
        )

        # Save grants from latest filing
        if details:
            filings = details.get("filings_with_data", []) or details.get("filings", [])
            if filings:
                latest = filings[0]
                tax_year_grants = latest.get("tax_prd_yr") or latest.get("tax_year") or 0
                grants = extract_grants(latest, ein, int(tax_year_grants) if tax_year_grants else 0)
                for g in grants:
                    try:
                        conn.execute(
                            """
                            INSERT INTO irs990_grants (ein, grantee_name, grantee_ein, amount_usd, purpose, tax_year)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                g["org_ein"],
                                g["grantee_name"],
                                g["grantee_ein"],
                                g["amount"],
                                g["purpose"],
                                g["tax_year"],
                            ),
                        )
                    except Exception as exc:
                        logger.warning("Failed to insert grant for %s: %s", ein, exc)

        conn.commit()
    finally:
        conn.close()


def run() -> None:
    """Main entry point: fetch IRS 990 data for all seed organizations."""
    logger.info("Starting IRS 990 monitor run")
    init_db()

    for org_name in SEED_ORGS:
        try:
            results = search_org(org_name)
            if not results:
                logger.info("No results for '%s'", org_name)
                continue
            for org in results:
                ein = org.get("ein")
                if not ein:
                    continue
                details = fetch_org_details(ein)
                save_org(org, details)
                time.sleep(1)  # Be polite to the API
        except Exception as exc:
            logger.exception("Unhandled exception for org '%s': %s", org_name, exc)

    logger.info("IRS 990 monitor run complete")


if __name__ == "__main__":
    run()
