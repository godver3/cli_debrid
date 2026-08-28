"""Helpers shared by rescrape routes and torrent reuse logic."""

import logging
import os
import re
from typing import Dict, Optional


def normalize_release_title(title: str) -> str:
    if not title:
        return ""
    normalized = title.lower()
    normalized = os.path.splitext(normalized)[0]
    return re.sub(r"[^a-z0-9]+", "", normalized)


def rescrape_blocks_pack_reuse(item: Optional[Dict], pack_title: str) -> bool:
    """True when a rescrapped item must not re-bind a sibling season pack."""
    rescrape_title = (item or {}).get("rescrape_original_torrent_title")
    if not rescrape_title or not pack_title:
        return False
    if normalize_release_title(rescrape_title) == normalize_release_title(pack_title):
        logging.info(
            f"[{(item or {}).get('title', 'Unknown')}] Skipping pack reuse for "
            f"{pack_title!r} — matches rescrape_original_torrent_title"
        )
        return True
    return False
