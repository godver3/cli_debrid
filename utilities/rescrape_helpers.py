"""Helpers shared by rescrape routes and torrent reuse logic."""

import json
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


def rescrape_blocks_any_pack_reuse(item: Optional[Dict]) -> bool:
    """True when a user rescrape is active and sibling season packs must not be reused."""
    if (item or {}).get("rescrape_original_torrent_title"):
        logging.info(
            f"[{(item or {}).get('title', 'Unknown')}] Skipping all sibling season-pack reuse "
            f"while rescrape_original_torrent_title is set"
        )
        return True
    return False


def _strip_nzb_prefix(job_id) -> str:
    job_id = str(job_id or '')
    return job_id[4:] if job_id.startswith('nzb:') else job_id


def replacement_blocks_pack_reuse(item: Optional[Dict], pack_title: str, torrent_id: Optional[str] = None) -> bool:
    """True when the sibling pack is the release this item is being replaced away from.

    Move to Wanted and Plex-report replacements stash the old release in replaced_files.
    Re-binding the item to a sibling's season pack that is that same release would
    undo the replacement, so only that pack is refused; other sibling packs (and every
    row without replaced_files) reuse as before.
    """
    raw = (item or {}).get('replaced_files')
    if not raw:
        return False
    try:
        replaced = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        return False
    pack_norm = normalize_release_title(pack_title)
    pack_id = _strip_nzb_prefix(torrent_id)
    for old in replaced:
        if not isinstance(old, dict):
            continue
        old_id = _strip_nzb_prefix(old.get('filled_by_torrent_id'))
        same_id = bool(pack_id and old_id and pack_id == old_id)
        same_title = bool(pack_norm and pack_norm == normalize_release_title(old.get('filled_by_title') or ''))
        if same_id or same_title:
            logging.info(
                f"[{(item or {}).get('title', 'Unknown')}] Not reusing sibling pack "
                f"{pack_title or torrent_id!r} — it's the release this item is being replaced away from"
            )
            return True
    return False


def rescrape_blocks_pack_reuse(item: Optional[Dict], pack_title: str, torrent_id: Optional[str] = None) -> bool:
    """True when a rescrapped or replaced item must not re-bind a sibling season pack."""
    if rescrape_blocks_any_pack_reuse(item):
        return True
    if replacement_blocks_pack_reuse(item, pack_title, torrent_id):
        return True
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
