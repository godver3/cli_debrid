"""Detect country / language region markers in media titles.

Franchise remakes (e.g. The Floor US vs PT vs AU) often carry a two-letter
code in parentheses or as a standalone token. cli_debrid uses those markers to:
  - keep the content-source title when battery metadata has the wrong region
  - hard-filter scrape results that target a different region
"""

from __future__ import annotations

import re
from typing import List, Optional

# Codes accepted inside parentheses: "(US)", "(PT)", "(GB)", etc.
KNOWN_TITLE_COUNTRY_CODES = frozenset({
    'UK', 'US', 'AU', 'CA', 'NZ', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'NO', 'DK',
    'FI', 'PL', 'CZ', 'HU', 'RO', 'BG', 'HR', 'RS', 'SI', 'SK', 'EE', 'LV', 'LT',
    'PT', 'GR', 'JP', 'KR', 'CN', 'IN', 'BR', 'MX', 'AR', 'CL', 'PE', 'CO', 'VE',
    'EC', 'BO', 'PY', 'UY', 'IE', 'BE', 'AT', 'CH', 'ZA', 'PH', 'TW', 'HK', 'SG',
    'MY', 'TH', 'ID', 'VN', 'TR', 'IL', 'SA', 'AE', 'EG', 'NG', 'KE', 'GH',
})

# Standalone tokens in release names ("The Floor AU S01E01"). Exclude codes that
# are common English words (IN, NO) so enabling detection always does not
# false-trigger language filtering on ordinary titles.
SAFE_STANDALONE_COUNTRY_CODES = frozenset({
    'UK', 'US', 'AU', 'CA', 'NZ', 'DE', 'FR', 'ES', 'IT', 'NL', 'SE', 'DK', 'FI',
    'PL', 'PT', 'BR', 'MX', 'JP', 'KR', 'CN', 'IE', 'BE', 'AT', 'CH', 'ZA', 'PH',
    'TW', 'HK', 'SG',
})

# PTT / ISO-style aliases → the codes we compare with in filters.
_COUNTRY_ALIASES = {
    'GB': 'UK',
}

_PAREN_CODE_RE = re.compile(r'\(([A-Za-z]{2})\)')


def normalize_title_country_code(code: Optional[str]) -> Optional[str]:
    """Normalize a two-letter country/language code for comparison."""
    if not code:
        return None
    upper = str(code).strip().upper()
    if len(upper) != 2 or not upper.isalpha():
        return None
    mapped = _COUNTRY_ALIASES.get(upper, upper)
    if mapped in KNOWN_TITLE_COUNTRY_CODES:
        return mapped
    return None


def extract_title_country_codes(text: Optional[str]) -> List[str]:
    """Return ordered unique country codes found in a title or release name."""
    if not text:
        return []

    detected: List[str] = []

    for match in _PAREN_CODE_RE.finditer(text):
        code = normalize_title_country_code(match.group(1))
        if code and code not in detected:
            detected.append(code)

    for word in text.upper().split():
        clean = re.sub(r'[^\w]', '', word)
        code = normalize_title_country_code(clean)
        if code and code in SAFE_STANDALONE_COUNTRY_CODES and code not in detected:
            detected.append(code)

    return detected


def primary_title_country_code(text: Optional[str]) -> Optional[str]:
    """First country code in a title, if any."""
    codes = extract_title_country_codes(text)
    return codes[0] if codes else None


def prefer_source_title_on_country_conflict(
    source_title: Optional[str],
    metadata_title: Optional[str],
    metadata_country: Optional[str] = None,
) -> Optional[str]:
    """Keep the content-source title when battery title's region disagrees.

    Example: Plex watchlist "The Floor (US)" + battery "The Floor (PT)" for the
    same IMDb → return the source title so scrape/queue use the requested region.
    """
    source = (source_title or '').strip()
    meta = (metadata_title or '').strip()
    if not source:
        return metadata_title
    if not meta:
        return source_title

    source_code = primary_title_country_code(source)
    meta_code = primary_title_country_code(meta)
    country_field = normalize_title_country_code(metadata_country)

    if source_code and meta_code and source_code != meta_code:
        return source_title

    # Battery title region conflicts with the metadata country field (e.g. title
    # says PT but country is us) — prefer a source title that matches the field
    # or that carries no conflicting region marker.
    if meta_code and country_field and meta_code != country_field:
        if not source_code or source_code == country_field:
            return source_title

    return metadata_title
