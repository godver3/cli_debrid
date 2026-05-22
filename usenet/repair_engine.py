"""
NZB Repair Engine

Workflow per broken item:
  1. Fetch broken NZB details from Decypharr /api/repair/health
  2. Match to CLI DB item via filled_by_torrent_id = 'nzb:{info_hash}'
  3. Blacklist broken segment ID
  4. Delete from Decypharr
  5. Delete from Plex
  6. Targeted re-scrape (NZB-only, all enabled indexers by priority)
  7. Pick best alternative (different from broken segment)
  8. Submit to Decypharr → get new job_id
  9. Update existing DB item to state=Adding with new torrent ID + scrape results
 10. Log to nzb_repair_activity
"""

import json
import logging
import time
from typing import Optional

import requests

from database.core import get_db_connection
from database.nzb_repair_activity import log_repair_activity
from database.not_wanted_magnets import (
    add_to_not_wanted_nzb_segment,
    extract_nzb_segment_id,
    is_nzb_segment_not_wanted,
)
from utilities.settings import get_setting

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dcy_headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'} if token else {}


def _plex_cfg():
    url = get_setting('Plex', 'url', '').rstrip('/')
    token = get_setting('Plex', 'token', '')
    disabled = get_setting('Plex', 'disable_plex_library_checks', False)
    return url, token, disabled


def _dcy_cfg():
    url = get_setting('Usenet Provider', 'url', '').rstrip('/')
    token = get_setting('Usenet Provider', 'api_token', '')
    enabled = get_setting('Usenet Provider', 'enabled', False)
    return url, token, enabled


# ---------------------------------------------------------------------------
# Decypharr health scan
# ---------------------------------------------------------------------------

def _parse_health_entries(data) -> list:
    """Parse the flat list returned by /api/repair/health."""
    return data if isinstance(data, list) else data.get('entries', data.get('items', []))


def _entry_name(entry: dict) -> str:
    """Normalise entry name from Decypharr health record."""
    return entry.get('entry_name') or entry.get('name') or entry.get('title') or ''


def fetch_broken_items() -> list:
    """Return list of broken entry dicts from Decypharr /api/repair/health."""
    dcy_url, dcy_token, enabled = _dcy_cfg()
    if not enabled or not dcy_url:
        return []
    try:
        r = requests.get(
            f'{dcy_url}/api/repair/health',
            headers=_dcy_headers(dcy_token),
            timeout=60,
        )
        if r.status_code != 200:
            logger.warning(f'[NZBRepair] /api/repair/health returned HTTP {r.status_code}')
            return []
        broken = [e for e in _parse_health_entries(r.json()) if (e.get('status') or '').lower() == 'broken']
        logger.info(f'[NZBRepair] Found {len(broken)} broken item(s)')
        return broken
    except Exception as e:
        logger.error(f'[NZBRepair] fetch_broken_items error: {e}')
        return []


def trigger_health_scan() -> bool:
    """POST to Decypharr to trigger a fresh health scan. Returns True if accepted."""
    dcy_url, dcy_token, enabled = _dcy_cfg()
    if not enabled or not dcy_url:
        return False
    try:
        r = requests.post(
            f'{dcy_url}/api/repair/run',
            headers=_dcy_headers(dcy_token),
            timeout=30,
        )
        ok = r.status_code in (200, 202, 204)
        logger.info(f'[NZBRepair] trigger_health_scan: HTTP {r.status_code} → {"ok" if ok else "failed"}')
        return ok
    except Exception as e:
        logger.warning(f'[NZBRepair] trigger_health_scan error: {e}')
        return False


def get_health_summary() -> dict:
    """Return counts: healthy/broken/repairing/stale/unknown from Decypharr."""
    dcy_url, dcy_token, enabled = _dcy_cfg()
    if not enabled or not dcy_url:
        return {}
    try:
        r = requests.get(
            f'{dcy_url}/api/repair/health',
            headers=_dcy_headers(dcy_token),
            timeout=60,
        )
        if r.status_code != 200:
            return {}
        counts = {}
        for entry in _parse_health_entries(r.json()):
            s = (entry.get('status') or 'unknown').lower()
            counts[s] = counts.get(s, 0) + 1
        return counts
    except Exception as e:
        logger.debug(f'[NZBRepair] get_health_summary error: {e}')
        return {}


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------

def _resolve_info_hash_from_decypharr(entry_name: str) -> str:
    """
    When the health API returns hash='', look up the job UUID from /api/torrents by name.
    Returns the UUID string (which is the info_hash used in filled_by_torrent_id) or ''.
    """
    dcy_url, dcy_token, enabled = _dcy_cfg()
    if not enabled or not dcy_url or not entry_name:
        return ''
    try:
        r = requests.get(
            f'{dcy_url}/api/torrents',
            params={'search': entry_name[:60]},
            headers=_dcy_headers(dcy_token),
            timeout=10,
        )
        if r.status_code == 200:
            for t in r.json().get('torrents', []):
                if t.get('name', '').strip() == entry_name.strip():
                    return t.get('info_hash', '')
    except Exception as e:
        logger.debug(f'[NZBRepair] _resolve_info_hash_from_decypharr error: {e}')
    return ''


def _find_db_item_by_info_hash(info_hash: str) -> Optional[dict]:
    """Look up a media_items row whose filled_by_torrent_id == 'nzb:{info_hash}'."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM media_items WHERE filled_by_torrent_id = ? LIMIT 1",
            (f'nzb:{info_hash}',),
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug(f'[NZBRepair] DB lookup error for {info_hash}: {e}')
        return None
    finally:
        conn.close()


def _find_db_items_by_entry_name(entry_name: str) -> list:
    """
    Find ALL DB items whose files belong to this Decypharr entry (folder).
    Returns list of dicts. Items with state='Adding' mean repair already in progress — caller skips them.
    """
    if not entry_name:
        return []
    conn = get_db_connection()
    try:
        all_states = ('Collected', 'Checking', 'Upgrading', 'Adding')

        # Exact debrid_folder_name match
        rows = conn.execute(
            "SELECT * FROM media_items WHERE debrid_folder_name = ? AND state IN ('Collected','Checking','Upgrading')",
            (entry_name,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # filled_by_file without extension matches entry_name
        rows = conn.execute(
            """SELECT * FROM media_items
               WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(filled_by_file,'.mkv',''),'.mp4',''),'.avi',''),'.m4v',''),'.ts','') = ?
               AND state IN ('Collected','Checking','Upgrading')""",
            (entry_name,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # location_on_disk path contains the entry_name as a directory component
        rows = conn.execute(
            "SELECT * FROM media_items WHERE location_on_disk LIKE ? AND state IN ('Collected','Checking','Upgrading')",
            (f'/debrid/%/{entry_name}/%',),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Prefix match on filled_by_file
        rows = conn.execute(
            "SELECT * FROM media_items WHERE filled_by_file LIKE ? AND state IN ('Collected','Checking','Upgrading')",
            (entry_name[:40] + '%',),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        # Title + episode number fallback — parse SxxExx from entry_name
        import re as _re
        ep_match = _re.search(r'[Ss](\d{1,2})[Ee](\d{1,2})', entry_name)
        if ep_match:
            season_num = int(ep_match.group(1))
            ep_num = int(ep_match.group(2))
            # Extract show title: everything before SxxExx, replace dots/underscores with spaces
            raw_title = entry_name[:ep_match.start()].replace('.', ' ').replace('_', ' ').strip()
            # Keep only first 3 words to fuzzy-match (avoids resolution/source noise)
            title_words = raw_title.split()[:3]
            title_prefix = ' '.join(title_words)
            if title_prefix:
                rows = conn.execute(
                    """SELECT * FROM media_items
                       WHERE title LIKE ? AND season_number = ? AND episode_number = ?
                       AND state IN ('Collected','Checking','Upgrading')""",
                    (title_prefix + '%', season_num, ep_num),
                ).fetchall()
                if rows:
                    return [dict(r) for r in rows]

        # Last resort: check if already in Adding state (repair already triggered)
        rows = conn.execute(
            """SELECT * FROM media_items
               WHERE (debrid_folder_name = ? OR filled_by_file LIKE ?)
               AND state = 'Adding'""",
            (entry_name, entry_name[:40] + '%'),
        ).fetchall()
        if rows:
            logger.info(f'[NZBRepair] Entry {entry_name!r} already in Adding state — repair in progress')
            return [dict(r) for r in rows]

        return []
    except Exception as e:
        logger.debug(f'[NZBRepair] DB entry lookup error for {entry_name!r}: {e}')
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decypharr deletion
# ---------------------------------------------------------------------------

def _delete_from_decypharr(info_hash: str, entry_name: str) -> bool:
    dcy_url, dcy_token, _ = _dcy_cfg()
    if not dcy_url:
        return False
    hdrs = _dcy_headers(dcy_token)

    # Primary: DELETE /api/torrents?hashes={uuid}
    if info_hash:
        try:
            r = requests.delete(
                f'{dcy_url}/api/torrents',
                params={'hashes': info_hash},
                headers=hdrs, timeout=15,
            )
            if r.status_code in (200, 204):
                logger.info(f'[NZBRepair] Deleted from Decypharr: {info_hash}')
                return True
            if r.status_code == 404:
                logger.info(f'[NZBRepair] NZB {info_hash} already gone from Decypharr')
                return True
        except Exception as e:
            logger.debug(f'[NZBRepair] Direct delete failed: {e}')

    # Fallback: search /api/torrents by name
    if entry_name:
        try:
            r = requests.get(
                f'{dcy_url}/api/torrents',
                params={'search': entry_name[:60]},
                headers=hdrs, timeout=10,
            )
            if r.status_code == 200:
                for t in r.json().get('torrents', []):
                    h = t.get('info_hash', '')
                    if h:
                        rd = requests.delete(
                            f'{dcy_url}/api/torrents',
                            params={'hashes': h},
                            headers=hdrs, timeout=10,
                        )
                        if rd.status_code in (200, 204, 404):
                            logger.info(f'[NZBRepair] Deleted from Decypharr by name: {entry_name!r}')
                            return True
        except Exception as e:
            logger.debug(f'[NZBRepair] Name-search delete failed: {e}')

    logger.warning(f'[NZBRepair] Could not delete {entry_name!r} from Decypharr')
    return False


# ---------------------------------------------------------------------------
# Plex deletion
# ---------------------------------------------------------------------------

def _delete_from_plex(item: dict) -> bool:
    plex_url, plex_token, disabled = _plex_cfg()
    if disabled or not plex_url or not plex_token:
        return False

    title = item.get('title', '')
    media_type = item.get('type', 'movie')
    season = item.get('season_number')
    episode = item.get('episode_number')

    params = {'X-Plex-Token': plex_token}
    hdrs = {'Accept': 'application/json'}

    try:
        if media_type == 'movie':
            # Search movies
            r = requests.get(
                f'{plex_url}/library/all',
                params={**params, 'title': title, 'type': 1},
                headers=hdrs, timeout=10,
            )
            if r.status_code == 200:
                items = r.json().get('MediaContainer', {}).get('Metadata', [])
                for m in items:
                    key = m.get('ratingKey', '')
                    if key:
                        requests.delete(
                            f'{plex_url}/library/metadata/{key}',
                            params=params, timeout=10,
                        )
                        logger.info(f'[NZBRepair] Deleted from Plex (movie): {title}')
                        return True
        else:
            # Search shows → find episode
            r = requests.get(
                f'{plex_url}/library/all',
                params={**params, 'title': title, 'type': 2},
                headers=hdrs, timeout=10,
            )
            if r.status_code == 200:
                shows = r.json().get('MediaContainer', {}).get('Metadata', [])
                for show in shows:
                    show_key = show.get('ratingKey', '')
                    if not show_key:
                        continue
                    # Get seasons
                    rs = requests.get(
                        f'{plex_url}/library/metadata/{show_key}/children',
                        params=params, headers=hdrs, timeout=10,
                    )
                    if rs.status_code != 200:
                        continue
                    for season_meta in rs.json().get('MediaContainer', {}).get('Metadata', []):
                        if season_meta.get('index') != season:
                            continue
                        season_key = season_meta.get('ratingKey', '')
                        if not season_key:
                            continue
                        re_ = requests.get(
                            f'{plex_url}/library/metadata/{season_key}/children',
                            params=params, headers=hdrs, timeout=10,
                        )
                        if re_.status_code != 200:
                            continue
                        for ep in re_.json().get('MediaContainer', {}).get('Metadata', []):
                            if ep.get('index') == episode:
                                ep_key = ep.get('ratingKey', '')
                                if ep_key:
                                    requests.delete(
                                        f'{plex_url}/library/metadata/{ep_key}',
                                        params=params, timeout=10,
                                    )
                                    logger.info(f'[NZBRepair] Deleted from Plex (episode): {title} S{season:02d}E{episode:02d}')
                                    return True
    except Exception as e:
        logger.warning(f'[NZBRepair] Plex delete error for {title!r}: {e}')
    return False


# ---------------------------------------------------------------------------
# Segment ID blacklisting
# ---------------------------------------------------------------------------

def _blacklist_broken_segment(nzb_url: str) -> str:
    """Add broken NZB guid to not-wanted and optionally also segment ID. Returns guid or ''."""
    if not nzb_url:
        return ''
    # Add guid immediately — no download needed
    try:
        from database.not_wanted_magnets import add_to_not_wanted_nzb_guid
        add_to_not_wanted_nzb_guid(nzb_url)
        logger.info(f'[NZBRepair] Blacklisted NZB guid from URL: {nzb_url[:80]}')
    except Exception as e:
        logger.debug(f'[NZBRepair] Guid blacklist error: {e}')
    return nzb_url


# ---------------------------------------------------------------------------
# Targeted re-scrape
# ---------------------------------------------------------------------------

def _scrape_for_replacement(item: dict, broken_nzb_title: str, version_override: str = None) -> list:
    """
    Run a targeted scrape for this item using the item's version (or an override),
    so version filters (resolution, filter_in/out, size) are respected.
    Returns NZB-only results, sorted by score, with broken title and blacklisted segments removed.
    """
    try:
        from scraper.scraper import scrape as do_scrape

        media_type = item.get('type', 'movie')
        is_episode = media_type == 'episode'
        version = version_override or item.get('version') or 'Default'

        logger.info(f'[NZBRepair] Scraping replacement for {item.get("title")!r} using version={version!r}')

        results, _ = do_scrape(
            imdb_id=item.get('imdb_id', '') or '',
            tmdb_id=str(item.get('tmdb_id', '') or ''),
            title=item.get('title', ''),
            year=item.get('year'),
            content_type='episode' if is_episode else 'movie',
            version=version,
            season=item.get('season_number') if is_episode else None,
            episode=item.get('episode_number') if is_episode else None,
            multi=False,
            genres=json.loads(item['genres']) if item.get('genres') else None,
            skip_cache_check=True,
        )

        # NZB-only
        nzb_results = [r for r in (results or []) if r.get('protocol') == 'nzb']

        # Remove the broken release
        if broken_nzb_title:
            nzb_results = [
                r for r in nzb_results
                if r.get('title') != broken_nzb_title
                and r.get('original_title') != broken_nzb_title
            ]

        # Filter out segment-blacklisted NZBs and pre-fetch for known problem hosts
        filtered = []
        for r in nzb_results:
            nzb_url = r.get('nzb_url') or r.get('magnet', '')
            if nzb_url:
                try:
                    resp = requests.get(nzb_url, timeout=15, allow_redirects=True,
                                        headers={'User-Agent': 'Sabnzbd/3.0.0'})
                    if resp.status_code == 200 and '<nzb' in resp.text.lower():
                        if is_nzb_segment_not_wanted(resp.text):
                            logger.debug(f'[NZBRepair] Skipping blacklisted segment: {r.get("title")}')
                            continue
                        r['_prefetched_nzb'] = resp.text
                except Exception:
                    pass
            filtered.append(r)

        logger.info(f'[NZBRepair] Re-scrape for {item.get("title")!r} (version={version!r}): {len(filtered)} usable NZB results')
        return filtered

    except Exception as e:
        logger.error(f'[NZBRepair] _scrape_for_replacement error: {e}', exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Submit replacement to Decypharr
# ---------------------------------------------------------------------------

def _submit_replacement(result: dict, title: str) -> Optional[str]:
    """Submit the best NZB to Decypharr. Returns new job_id or None."""
    try:
        from usenet.decypharr_client import get_decypharr_client, reset_decypharr_client
        reset_decypharr_client()
        client = get_decypharr_client()

        nzb_content = result.get('_prefetched_nzb', '')
        nzb_url = result.get('nzb_url') or result.get('magnet', '')
        release_title = result.get('title') or title

        if nzb_content:
            job_id = client.add_nzb_content(nzb_content=nzb_content, title=release_title)
        elif nzb_url:
            job_id = client.add_nzb(nzb_url=nzb_url, title=release_title)
        else:
            return None

        return job_id
    except Exception as e:
        logger.error(f'[NZBRepair] _submit_replacement error: {e}')
        return None


# ---------------------------------------------------------------------------
# DB update for replaced item
# ---------------------------------------------------------------------------

def _update_db_for_repair(item: dict, new_job_id: str, replacement_result: dict, all_results: list) -> bool:
    """
    Update existing DB item in-place for repair:
    - state = Adding
    - filled_by_torrent_id = nzb:{new_job_id}
    - filled_by_file / filled_by_title / debrid_folder_name = new release title
    - location_on_disk = None (will be re-populated once downloaded)
    - scrape_results = remaining candidate results
    """
    try:
        from database.database_writing import update_media_item_state, update_media_item
        item_id = item['id']
        new_torrent_id = f'nzb:{new_job_id}'
        release_title = replacement_result.get('title') or item.get('title', '')

        # Scrape results: keep all candidates except the one we just submitted
        remaining = [
            r for r in all_results
            if r.get('title') != replacement_result.get('title')
        ]

        update_media_item_state(
            item_id,
            'Adding',
            filled_by_torrent_id=new_torrent_id,
            filled_by_file=release_title,
            filled_by_title=release_title,
            debrid_folder_name=None,
            scrape_results=remaining,
        )
        # Clear location_on_disk separately (not in update_media_item_state)
        update_media_item(item_id, location_on_disk=None, fall_back_to_single_scraper=False)
        logger.info(f'[NZBRepair] DB updated for item {item_id}: state=Adding, torrent_id={new_torrent_id}')
        return True
    except Exception as e:
        logger.error(f'[NZBRepair] _update_db_for_repair error: {e}')
        return False


# ---------------------------------------------------------------------------
# Main repair loop
# ---------------------------------------------------------------------------

def get_available_versions() -> list:
    """Return list of configured version names from settings."""
    try:
        versions = get_setting('Scraping', 'versions', {})
        return sorted(versions.keys()) if versions else []
    except Exception:
        return []


def get_repair_version() -> str:
    """Return the saved repair version override (empty string = use item's own version)."""
    return get_setting('Usenet Provider', 'repair_version', '') or ''


def set_repair_version(version: str) -> None:
    """Persist the repair version override to settings."""
    from utilities.settings import set_setting
    set_setting('Usenet Provider', 'repair_version', version or '')


def run_repair(triggered_by: str = 'scheduled', version_override: str = None) -> dict:
    # If no explicit override passed (e.g. scheduled run), use the saved setting
    if version_override is None:
        saved = get_repair_version()
        if saved:
            version_override = saved
            logger.info(f'[NZBRepair] Using saved repair version: {version_override!r}')
    """
    Full repair cycle. Returns summary dict with counts.
    """
    summary = {
        'broken_found': 0,
        'matched': 0,
        'replaced': 0,
        'not_found': 0,
        'plex_deleted': 0,
        'errors': 0,
    }

    broken_entries = fetch_broken_items()
    summary['broken_found'] = len(broken_entries)

    if not broken_entries:
        logger.info('[NZBRepair] No broken items found — nothing to do')
        return summary

    for entry in broken_entries:
        entry_name = _entry_name(entry)
        # Top-level info_hash is the Decypharr job UUID (matches filled_by_torrent_id in DB)
        # broken_files[].info_hash is a file-level fingerprint — NOT the job UUID, don't use for DB lookup
        info_hash = entry.get('info_hash') or entry.get('hash') or ''
        # If top-level hash missing, resolve job UUID from /api/torrents by name
        if not info_hash and entry_name:
            info_hash = _resolve_info_hash_from_decypharr(entry_name)
            if info_hash:
                logger.info(f'[NZBRepair] Resolved job UUID={info_hash!r} for {entry_name!r}')
        # Keep file-level hash separately for deletion purposes only
        file_hash = ''
        for bf in (entry.get('broken_files') or []):
            file_hash = bf.get('info_hash') or ''
            if file_hash:
                break
        nzb_url = entry.get('nzb_url') or entry.get('url') or ''

        logger.info(f'[NZBRepair] Processing broken entry: {entry_name!r} hash={info_hash!r}')

        try:
            # 1. Match to CLI DB items (may be multiple episodes from a season pack)
            # If health API returned no hash, resolve the job UUID from Decypharr /api/torrents
            if not info_hash and entry_name:
                info_hash = _resolve_info_hash_from_decypharr(entry_name)
                if info_hash:
                    logger.info(f'[NZBRepair] Resolved info_hash={info_hash!r} for {entry_name!r}')

            db_items = []
            if info_hash:
                single = _find_db_item_by_info_hash(info_hash)
                if single:
                    db_items = [single]
            if not db_items and entry_name:
                db_items = _find_db_items_by_entry_name(entry_name)

            if not db_items:
                # Orphan entry — CLI has no record of it. Delete from Decypharr so it stops showing as broken.
                logger.warning(f'[NZBRepair] No DB items found for {entry_name!r} — orphan entry, deleting from Decypharr')
                _delete_from_decypharr(info_hash, entry_name)
                log_repair_activity(
                    broken_nzb_id=info_hash,
                    broken_nzb_title=entry_name,
                    outcome='not_found',
                    triggered_by=triggered_by,
                )
                summary['not_found'] += 1
                continue

            # Skip if all matched items are already in Adding state (repair already triggered)
            if all(item.get('state') == 'Adding' for item in db_items):
                logger.info(f'[NZBRepair] {entry_name!r} — all matched items already in Adding, repair in progress, skipping')
                continue

            # Filter out items already in Adding state
            db_items = [item for item in db_items if item.get('state') != 'Adding']
            if not db_items:
                continue

            summary['matched'] += len(db_items)
            logger.info(f'[NZBRepair] Matched {len(db_items)} DB item(s) to {entry_name!r}')

            # 2. Blacklist broken segment ID (once per entry)
            _blacklist_broken_segment(nzb_url)

            # 3. Delete from Decypharr (once per entry)
            _delete_from_decypharr(info_hash, entry_name)

            # Use first item as representative for scraping (same title/IDs for season packs)
            rep = db_items[0]
            broken_nzb_title = rep.get('debrid_folder_name') or rep.get('filled_by_file') or entry_name

            # 4. Targeted re-scrape (once per entry)
            candidates = _scrape_for_replacement(rep, broken_nzb_title, version_override=version_override)

            # 5. Pick best candidate and submit once
            best = candidates[0] if candidates else None
            new_job_id = _submit_replacement(best, rep.get('title', '')) if best else None

            for db_item in db_items:
                item_id = db_item['id']

                # 6. Delete from Plex (per episode)
                plex_deleted = _delete_from_plex(db_item)

                if not best or not new_job_id:
                    reason = 'No replacement NZB found' if not best else 'Decypharr rejected replacement'
                    logger.warning(f'[NZBRepair] {reason} for item {item_id} ({db_item.get("title")!r}) — moving to Wanted')
                    try:
                        from database.database_writing import update_media_item_state, update_media_item
                        update_media_item_state(
                            item_id, 'Wanted',
                            filled_by_torrent_id=None,
                            filled_by_file=None,
                            filled_by_title=None,
                            debrid_folder_name=None,
                        )
                        update_media_item(item_id, location_on_disk=None, fall_back_to_single_scraper=False)
                    except Exception as e:
                        logger.error(f'[NZBRepair] Could not move item {item_id} to Wanted: {e}')
                    log_repair_activity(
                        item_id=item_id,
                        title=db_item.get('title'),
                        media_type=db_item.get('type'),
                        season_number=db_item.get('season_number'),
                        episode_number=db_item.get('episode_number'),
                        broken_nzb_id=info_hash,
                        broken_nzb_title=broken_nzb_title,
                        outcome='plex_deleted' if plex_deleted else 'not_found',
                        triggered_by=triggered_by,
                    )
                    if plex_deleted:
                        summary['plex_deleted'] += 1
                    elif not best:
                        summary['not_found'] += 1
                    else:
                        summary['errors'] += 1
                    continue

                # 7. Update DB in-place for this item
                _update_db_for_repair(db_item, new_job_id, best, candidates[1:])

                log_repair_activity(
                    item_id=item_id,
                    title=db_item.get('title'),
                    media_type=db_item.get('type'),
                    season_number=db_item.get('season_number'),
                    episode_number=db_item.get('episode_number'),
                    broken_nzb_id=info_hash,
                    broken_nzb_title=broken_nzb_title,
                    replacement_nzb_id=new_job_id,
                    replacement_title=best.get('title'),
                    outcome='replaced',
                    triggered_by=triggered_by,
                )
                summary['replaced'] += 1
                logger.info(
                    f'[NZBRepair] Replaced item {item_id} ({db_item.get("title")!r}) '
                    f'with {best.get("title")!r} (job_id={new_job_id})'
                )

        except Exception as e:
            logger.error(f'[NZBRepair] Unhandled error for entry {entry_name!r}: {e}', exc_info=True)
            summary['errors'] += 1

    logger.info(f'[NZBRepair] Repair cycle complete: {summary}')
    return summary
