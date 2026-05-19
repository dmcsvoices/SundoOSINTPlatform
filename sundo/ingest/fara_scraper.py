"""DOJ FARA database scraper for Sundo Pi OSINT platform.

Scrapes the DOJ FARA eFile system for registrants with foreign principals
linked to Israel and related organizations.
"""

import logging
import random
import time
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

from sundo.config import BASE_DIR, LOG_FORMAT, LOG_LEVEL, FARA_TARGETS, MIN_REQUEST_DELAY, MAX_REQUEST_DELAY, MAX_RETRIES, RETRY_BACKOFF
from sundo.db.sqlite_store import init_db, get_connection

logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
logger = logging.getLogger("fara_scraper")

BASE_URL = "https://efile.fara.gov/ords/fara/f"
SEARCH_URL = f"{BASE_URL}?p=1235:10"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux arm64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}


def _sleep() -> None:
    """Sleep for a random duration between MIN_REQUEST_DELAY and MAX_REQUEST_DELAY seconds."""
    delay = random.uniform(MIN_REQUEST_DELAY, MAX_REQUEST_DELAY)
    time.sleep(delay)


def _fetch(url: str, session: requests.Session) -> requests.Response | None:
    """Fetch a URL with retries and exponential backoff.

    Args:
        url: URL to fetch.
        session: Requests session to use.

    Returns:
        Response object or None if all retries failed.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Request failed (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, exc)
            if attempt < MAX_RETRIES - 1:
                backoff = RETRY_BACKOFF * (2 ** attempt)
                logger.info("Retrying in %.1f seconds...", backoff)
                time.sleep(backoff)
    logger.error("All retries exhausted for %s", url)
    return None


def _log_parse_failure(url: str, raw_content: str, reason: str) -> None:
    """Log a parse failure to sundo_errors.log.

    Args:
        url: The URL that failed to parse.
        raw_content: Raw HTML or text content.
        reason: Human-readable reason for failure.
    """
    from pathlib import Path
    log_path = Path("/home/darren/sundo-pi/logs/sundo_errors.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n{'=' * 60}\n")
        fh.write(f"Timestamp: {datetime.utcnow().isoformat()}Z\n")
        fh.write(f"Module: fara_scraper\n")
        fh.write(f"URL: {url}\n")
        fh.write(f"Reason: {reason}\n")
        fh.write(f"Raw content (first 4000 chars):\n{raw_content[:4000]}\n")
        fh.write(f"{'=' * 60}\n")


def _extract_text(element: Any) -> str:
    """Safely extract stripped text from a BeautifulSoup element.

    Args:
        element: A BeautifulSoup element or None.

    Returns:
        Stripped text or empty string.
    """
    if element is None:
        return ""
    return element.get_text(strip=True)


def _extract_fara_filings(soup: BeautifulSoup, source_url: str) -> list[dict[str, Any]]:
    """Extract FARA registrant filings from a BeautifulSoup object.

    Args:
        soup: Parsed HTML of a FARA results page.
        source_url: The URL these results came from.

    Returns:
        List of parsed filing dictionaries.
    """
    filings: list[dict[str, Any]] = []

    # The FARA API results page uses an interactive report table.
    # We look for rows with registrant data.
    rows = soup.find_all("tr")
    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        # Try to identify registrant name and principal from cell text
        texts = [_extract_text(c) for c in cells]
        full_text = " ".join(texts).lower()

        matched = any(target.lower() in full_text for target in FARA_TARGETS)
        if not matched:
            continue

        # Heuristic extraction — FARA tables vary, so we grab all text and
        # assign heuristically based on column count.
        registrant_name = texts[0] if texts else ""
        foreign_principal = texts[1] if len(texts) > 1 else ""
        country = "Israel" if "israel" in full_text else ""
        if not country:
            # Try to infer country from principal name
            country = foreign_principal.split(",")[-1].strip() if "," in foreign_principal else ""

        registration_date = ""
        for t in texts:
            if "/" in t and any(ch.isdigit() for ch in t):
                registration_date = t
                break

        activities = ""
        disbursements = ""
        exhibit_b = ""
        exhibit_c = ""

        # Look for links to Exhibit B / C in the row
        for link in row.find_all("a", href=True):
            href = link["href"]
            if "exhibit_b" in href.lower() or "exb" in href.lower():
                exhibit_b = href if href.startswith("http") else f"https://efile.fara.gov{href}"
            elif "exhibit_c" in href.lower() or "exc" in href.lower():
                exhibit_c = href if href.startswith("http") else f"https://efile.fara.gov{href}"

        filings.append(
            {
                "registrant_name": registrant_name,
                "registrant_address": "",
                "registration_date": registration_date,
                "foreign_principal_name": foreign_principal,
                "foreign_principal_country": country,
                "activities": activities,
                "disbursements": disbursements,
                "exhibit_b": exhibit_b,
                "exhibit_c": exhibit_c,
                "raw_html": str(row),
                "source_url": source_url,
            }
        )

    return filings


def search_fara_by_principal(principal_name: str, session: requests.Session) -> list[dict[str, Any]]:
    """Search FARA database for a specific foreign principal.

    Args:
        principal_name: Name of the foreign principal to search for.
        session: Requests session.

    Returns:
        List of filing dictionaries.
    """
    logger.info("Searching FARA for principal: %s", principal_name)

    # The FARA API page uses ORDS interactive reports.
    # We perform a GET to the base page first to capture any session cookies,
    # then attempt a search via query parameters if supported.
    _sleep()
    resp = _fetch(SEARCH_URL, session)
    if resp is None:
        logger.error("Failed to load FARA search page")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Attempt to locate the search form and submit a principal query
    search_form = soup.find("form")
    if search_form:
        action = search_form.get("action", SEARCH_URL)
        # Fix relative form action URLs
        if action and not action.startswith("http"):
            if action.startswith("/"):
                action = f"https://efile.fara.gov{action}"
            else:
                action = f"https://efile.fara.gov/ords/fara/{action}"
        if not action:
            action = SEARCH_URL
        
        inputs = search_form.find_all("input")
        payload: dict[str, str] = {}
        for inp in inputs:
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")

        # Try to inject the principal name into a likely search field
        for key in payload:
            if "search" in key.lower() or "query" in key.lower() or "principal" in key.lower():
                payload[key] = principal_name
                break
        else:
            # If no obvious search field, add a generic one (may be ignored by server)
            payload["p_search"] = principal_name

        _sleep()
        try:
            resp = session.post(action, data=payload, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("FARA search POST failed: %s", exc)
            raw_text = resp.text if "resp" in dir() and hasattr(resp, "text") else ""
            _log_parse_failure(action, raw_text, f"POST failed: {exc}")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")

    filings = _extract_fara_filings(soup, resp.url)
    logger.info("Found %d filings for principal '%s'", len(filings), principal_name)
    return filings


def search_fara_by_registrant(registrant_name: str, session: requests.Session) -> list[dict[str, Any]]:
    """Search FARA database for a specific registrant name.

    Args:
        registrant_name: Name of the registrant to search for.
        session: Requests session.

    Returns:
        List of filing dictionaries.
    """
    logger.info("Searching FARA for registrant: %s", registrant_name)
    # Reuse the same flow with the registrant name as query
    return search_fara_by_principal(registrant_name, session)


def save_filings(filings: list[dict[str, Any]]) -> int:
    """Persist FARA filings to SQLite, skipping duplicates.

    Args:
        filings: List of filing dictionaries.

    Returns:
        Number of new filings inserted.
    """
    if not filings:
        return 0

    conn = get_connection()
    inserted = 0
    try:
        for f in filings:
            try:
                # Build a registration_number from registrant + principal if none exists
                reg_num = f.get("registration_number", "")
                if not reg_num:
                    reg_num = f"{f.get('registrant_name', '')}-{f.get('foreign_principal_name', '')}".replace(" ", "-")[:64]

                # Extract amount from disbursements text if present
                amount_usd = None
                disbursements = f.get("disbursements", "")
                if disbursements:
                    import re
                    m = re.search(r"[\d,]+\.?\d*", str(disbursements).replace(",", ""))
                    if m:
                        try:
                            amount_usd = float(m.group())
                        except ValueError:
                            pass

                # Use exhibit_b or exhibit_c as pdf_url if available
                pdf_url = f.get("exhibit_b", "") or f.get("exhibit_c", "")

                conn.execute(
                    """
                    INSERT INTO fara_filings (
                        registration_number, registrant_name, foreign_principal,
                        country, filing_date, form_type, amount_usd, purpose,
                        pdf_url, raw_text, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(registration_number, filing_date) DO UPDATE SET
                        raw_text=excluded.raw_text,
                        pdf_url=excluded.pdf_url,
                        ingested_at=CURRENT_TIMESTAMP
                    """,
                    (
                        reg_num,
                        f.get("registrant_name", ""),
                        f.get("foreign_principal_name", ""),
                        f.get("foreign_principal_country", ""),
                        f.get("registration_date", ""),
                        "unknown",
                        amount_usd,
                        f.get("activities", ""),
                        pdf_url,
                        f.get("raw_html", ""),
                    ),
                )
                inserted += 1
            except Exception as exc:
                logger.warning("Failed to insert filing for %s: %s", f.get("registrant_name"), exc)
        conn.commit()
    finally:
        conn.close()

    return inserted


def run() -> None:
    """Main entry point: scrape FARA for all configured targets."""
    logger.info("Starting FARA scraper run")
    init_db()

    session = requests.Session()
    all_filings: list[dict[str, Any]] = []

    # Search by each target as both principal and registrant
    targets = list(set(FARA_TARGETS))
    for target in targets:
        try:
            filings = search_fara_by_principal(target, session)
            all_filings.extend(filings)
        except Exception as exc:
            logger.exception("Unhandled exception searching principal %s: %s", target, exc)

        try:
            filings = search_fara_by_registrant(target, session)
            all_filings.extend(filings)
        except Exception as exc:
            logger.exception("Unhandled exception searching registrant %s: %s", target, exc)

    inserted = save_filings(all_filings)
    logger.info("FARA scraper complete: %d total filings, %d inserted/updated", len(all_filings), inserted)


if __name__ == "__main__":
    run()
