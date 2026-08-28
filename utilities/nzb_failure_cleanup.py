"""Shared NZB failure blacklisting and item cleanup.

Every path that rejects or abandons a cli_mount NZB job should call
``blacklist_and_cleanup_nzb_failure`` so guid/segment/hash blacklists,
filled_by_* clearing, health-cache eviction, and rescrape title pinning
stay consistent across ffprobe reject, health-check broken, failed-in-cli_mount,
and supersede cancel.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional


def _resolve_job_hash(item: Dict[str, Any]) -> str:
    torrent_id = str(item.get('filled_by_torrent_id') or '')
    if torrent_id.startswith('nzb:'):
        return torrent_id[4:]
    return torrent_id


def _resolve_segment_id(
    item: Dict[str, Any],
    nzb_title: Optional[str] = None,
    fetch_if_missing: bool = True,
) -> str:
    seg_id = str(item.get('nzb_segment_id') or item.get('_nzb_segment_id') or '').strip()
    if seg_id or not fetch_if_missing:
        return seg_id

    nzb_url = item.get('filled_by_magnet', '') or ''
    if nzb_url:
        try:
            from routes.api_tracker import api as _api
            from database.not_wanted_magnets import extract_nzb_segment_id
            resp = _api.get(nzb_url, timeout=15, allow_redirects=True)
            if resp.status_code == 200 and '<nzb' in resp.text.lower():
                seg_id = extract_nzb_segment_id(resp.text) or ''
                if seg_id:
                    return seg_id
        except Exception as exc:
            logging.debug(f'[NZB] Could not fetch segment ID from filled_by_magnet: {exc}')

    scrape_title = nzb_title or item.get('filled_by_title') or item.get('filled_by_file') or ''
    results_raw = item.get('scrape_results', [])
    if isinstance(results_raw, str):
        try:
            results_raw = json.loads(results_raw)
        except Exception:
            results_raw = []
    for result in results_raw or []:
        if scrape_title and result.get('title', '') != scrape_title and result.get('original_title', '') != scrape_title:
            continue
        fetch_url = result.get('nzb_url', '') or result.get('magnet', '')
        if not fetch_url:
            continue
        try:
            from routes.api_tracker import api as _api
            from database.not_wanted_magnets import extract_nzb_segment_id
            resp = _api.get(fetch_url, timeout=15, allow_redirects=True)
            if resp.status_code == 200 and '<nzb' in resp.text.lower():
                seg_id = extract_nzb_segment_id(resp.text) or ''
                if seg_id:
                    return seg_id
        except Exception:
            pass
        break
    return ''


def blacklist_and_cleanup_nzb_failure(
    item: Dict[str, Any],
    reason: str,
    *,
    clear_filled_by: bool = True,
    set_rescrape_title: bool = True,
    clear_health_cache: bool = True,
    nzb_title: Optional[str] = None,
    fetch_segment_if_missing: bool = True,
) -> None:
    """Blacklist a failed/abandoned NZB job and reset item fields that enable reuse loops."""
    item_id = item.get('id')
    job_hash = _resolve_job_hash(item)
    nzb_url = item.get('filled_by_magnet', '') or ''

    try:
        from database.not_wanted_magnets import (
            add_to_not_wanted,
            add_to_not_wanted_nzb_guid,
            add_to_not_wanted_nzb_segment,
        )
        if nzb_url:
            add_to_not_wanted_nzb_guid(nzb_url)
        seg_id = _resolve_segment_id(item, nzb_title=nzb_title, fetch_if_missing=fetch_segment_if_missing)
        if seg_id:
            add_to_not_wanted_nzb_segment(seg_id)
        if job_hash:
            add_to_not_wanted(job_hash)
        logging.info(
            f'[NZB] Blacklisted failed job for item {item_id} ({reason}): '
            f'hash={job_hash!r}, segment={seg_id!r}, guid={"yes" if nzb_url else "no"}'
        )
    except Exception as exc:
        logging.warning(f'[NZB] Failed to blacklist job for item {item_id} ({reason}): {exc}')

    if clear_health_cache and job_hash:
        try:
            from queues.run_program import clear_nzb_job_health_cache
            clear_nzb_job_health_cache(job_hash)
        except Exception as exc:
            logging.debug(f'[NZB] Could not clear health cache for job {job_hash}: {exc}')

    update_kwargs: Dict[str, Any] = {}
    if clear_filled_by:
        update_kwargs.update({
            'filled_by_torrent_id': None,
            'filled_by_file': None,
            'filled_by_title': None,
            'filled_by_magnet': None,
            'debrid_folder_name': None,
        })
        for key in update_kwargs:
            item[key] = None

    if set_rescrape_title and not item.get('rescrape_original_torrent_title'):
        bad_title = (
            item.get('original_scraped_torrent_title')
            or item.get('filled_by_title')
            or item.get('filled_by_file')
            or ''
        )
        if bad_title:
            update_kwargs['rescrape_original_torrent_title'] = bad_title
            item['rescrape_original_torrent_title'] = bad_title

    if update_kwargs and item_id:
        try:
            from database.database_writing import update_media_item
            update_media_item(item_id, **update_kwargs)
        except Exception as exc:
            logging.warning(f'[NZB] Failed to persist cleanup for item {item_id}: {exc}')
