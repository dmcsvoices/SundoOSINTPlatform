"""
sundo/ingest/author_extractor.py

Extracts and normalizes author information from RSS feed entries.
Handles the inconsistent ways different feeds expose byline data.
"""

import re
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Extraction ───────────────────────────────────────────────────────────────

def extract_author(entry) -> Optional[dict]:
    """
    Extract author information from a feedparser entry dict/object.

    Tries multiple fields in priority order:
    1. entry.author — most common
    2. entry.author_detail — structured, may include email/href
    3. entry.authors[] — list form (Atom feeds)
    4. entry.dc_creator — Dublin Core

    Returns a normalized author dict or None if no author found.
    """
    raw_name = None

    # Priority 1: entry.author (string)
    if hasattr(entry, 'author') and entry.author:
        raw_name = entry.author
    elif isinstance(entry, dict) and entry.get('author'):
        raw_name = entry['author']

    # Priority 2: entry.author_detail (structured)
    if not raw_name:
        detail = None
        if hasattr(entry, 'author_detail') and entry.author_detail:
            detail = entry.author_detail
        elif isinstance(entry, dict) and entry.get('author_detail'):
            detail = entry['author_detail']
        if detail:
            if hasattr(detail, 'name') and detail.name:
                raw_name = detail.name
            elif hasattr(detail, 'email') and detail.email:
                raw_name = detail.email
            elif isinstance(detail, dict):
                raw_name = detail.get('name') or detail.get('email')

    # Priority 3: entry.authors (list — Atom)
    if not raw_name:
        authors = None
        if hasattr(entry, 'authors') and entry.authors:
            authors = entry.authors
        elif isinstance(entry, dict) and entry.get('authors'):
            authors = entry['authors']
        if authors:
            first = authors[0]
            if isinstance(first, dict):
                raw_name = first.get('name') or first.get('email')
            elif hasattr(first, 'get'):
                raw_name = first.get('name') or first.get('email')
            else:
                raw_name = str(first)

    # Priority 4: Dublin Core creator
    if not raw_name:
        if hasattr(entry, 'dc_creator') and entry.dc_creator:
            raw_name = entry.dc_creator
        elif isinstance(entry, dict) and entry.get('dc_creator'):
            raw_name = entry['dc_creator']

    if not raw_name:
        return None

    return normalize_author(raw_name)


def normalize_author(raw_name: str) -> Optional[dict]:
    """
    Normalize a raw byline string into a canonical author dict.

    Handles common RSS byline noise:
    - "By Bilal Shbair" → "Bilal Shbair"
    - "bilal.shbair@972mag.com" → "bilal shbair"
    - "Staff Writer" → None (generic — discard)
    - "Reuters" → None (wire service — discard)
    - "AP" → None (wire service — discard)
    """
    if not raw_name or not raw_name.strip():
        return None

    name = raw_name.strip()

    # Strip "By " prefix (case-insensitive)
    name = re.sub(r'^[Bb]y\s+', '', name)

    # Strip email addresses — extract username if name-like
    if '@' in name:
        username = name.split('@')[0]
        # Convert dots/underscores to spaces for display
        name = username.replace('.', ' ').replace('_', ' ').title()

    name = name.strip()

    # Discard generic bylines — not useful as author nodes
    DISCARD_NAMES = {
        'staff', 'staff writer', 'staff reporter', 'editorial', 'editors',
        'the editors', 'admin', 'administrator', 'webmaster', 'news desk',
        'foreign desk', 'reuters', 'ap', 'associated press', 'afp',
        'wire service', 'contributor', 'guest writer', 'special to',
        'correspondent', 'news', 'breaking news', 'unknown', 'anonymous',
    }
    if name.lower() in DISCARD_NAMES:
        logger.debug("Discarding generic byline: %s", name)
        return None

    # Must have at least 2 characters
    if len(name) < 2:
        return None

    author_id = make_author_id(name)
    handle = make_handle(name)

    return {
        'id': author_id,
        'display_name': name,
        'handle': handle,
        'byline_variants': [name],
        'verification_status': 'pending',
    }



def make_author_id(display_name: str) -> str:
    """
    Create a stable, unique author ID from a display name.

    Uses a slug of the normalized name with a short hash suffix
    to handle collisions (two different "John Smith" authors).

    Format: "john-smith-a3f2"
    """
    slug = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
    hash_suffix = hashlib.md5(display_name.encode()).hexdigest()[:4]
    return f"{slug}-{hash_suffix}"


def make_handle(display_name: str) -> str:
    """
    Create a social-media-style handle from a display name.
    "Bilal Shbair" → "bilalshbair"
    """
    return re.sub(r'[^a-z0-9]', '', display_name.lower())


# ── Language detection ────────────────────────────────────────────────────────

def compute_verification_status(article_count: int, source_count: int, byline_variants: list) -> str:
    """
    Compute an author's verification status based on track record.

    Rules:
      - >= 10 articles → "verified"
      - >= 5 articles and consistent byline across 2+ sources → "verified"
      - < 5 articles → "pending"
      - Conflicting metadata (3+ byline variants, or >=5 articles with <2 sources) → "suspicious"
    """
    if article_count >= 10:
        return "verified"
    if len(byline_variants) >= 3:
        return "suspicious"
    if article_count >= 5:
        if source_count >= 2:
            return "verified"
        return "suspicious"
    return "pending"


def detect_language(text: str) -> str:
    """
    Lightweight language detection based on character ranges.
    Returns ISO 639-1 code. Falls back to 'en'.

    For full accuracy, replace with langdetect library if available.
    """
    if not text:
        return 'en'

    # Arabic Unicode block: U+0600–U+06FF
    arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
    # Hebrew Unicode block: U+0590–U+05FF
    hebrew_chars = sum(1 for c in text if '\u0590' <= c <= '\u05FF')

    total = len(text)
    if total == 0:
        return 'en'

    if arabic_chars / total > 0.15:
        return 'ar'
    if hebrew_chars / total > 0.15:
        return 'he'

    return 'en'
