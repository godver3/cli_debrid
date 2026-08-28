"""Conservative post-download size validation for individual NZB releases."""

import json
import re
from typing import Any, Dict, Optional, Tuple


MIN_ACTUAL_TO_ADVERTISED_RATIO = 0.10
_INDIVIDUAL_EPISODE_RE = re.compile(r"[Ss]\d{2}[Ee]\d{2}(?!\d)")
_BYTES_PER_GIB = 1024 ** 3


def _scrape_results(item: Dict[str, Any]) -> list:
    results = item.get("scrape_results") or []
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except (TypeError, ValueError):
            return []
    return results if isinstance(results, list) else []


def advertised_size_bytes(item: Dict[str, Any], nzb_title: str) -> Optional[int]:
    """Return the selected result's advertised size, preferring its exact URL."""
    selected_url = item.get("filled_by_magnet") or ""
    title_match = None
    for result in _scrape_results(item):
        if not isinstance(result, dict):
            continue
        result_url = result.get("nzb_url") or result.get("magnet") or ""
        result_title = result.get("title") or result.get("original_title") or ""
        if selected_url and result_url == selected_url:
            title_match = result
            break
        if result_title == nzb_title and title_match is None:
            title_match = result

    if not title_match:
        return None
    raw_size = title_match.get("total_size_gb", title_match.get("size"))
    try:
        size_gib = float(raw_size)
    except (TypeError, ValueError):
        return None
    if size_gib <= 0:
        return None
    return int(size_gib * _BYTES_PER_GIB)


def individual_nzb_size_mismatch(
    item: Dict[str, Any],
    nzb_title: str,
    actual_size_bytes: Any,
    minimum_ratio: float = MIN_ACTUAL_TO_ADVERTISED_RATIO,
) -> Optional[Tuple[int, int, float]]:
    """Describe an extreme size mismatch, or return None when safe/unknown.

    The check intentionally fails open. It applies only to movies and release
    names that identify one episode, never aggregate/season packs.
    """
    media_type = str(item.get("type") or "").lower()
    if media_type == "episode" and not _INDIVIDUAL_EPISODE_RE.search(nzb_title or ""):
        return None
    if media_type not in ("movie", "episode"):
        return None
    try:
        actual_size = int(actual_size_bytes)
    except (TypeError, ValueError):
        return None
    if actual_size <= 0:
        return None

    expected_size = advertised_size_bytes(item, nzb_title)
    if not expected_size:
        return None
    ratio = actual_size / expected_size
    if ratio >= minimum_ratio:
        return None
    return actual_size, expected_size, ratio
