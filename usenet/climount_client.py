"""
Thin client for submitting NZB downloads to cli_mount.

cli_mount exposes a SABnzbd-compatible API at POST /api/add:
  - nzbURLs  : newline-separated URLs pointing to .nzb files
  - nzbFiles : multipart .nzb file uploads
  - arr       : category/arr name (optional)
  - downloadFolder : override download folder (optional)
  - action   : 'symlink' | 'move' | etc (optional)

Files land on the rclone mount automatically after cli_mount downloads them.
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any, Tuple

from utilities.settings import get_setting
from routes.api_tracker import api




class CliMountClient:
    """Submits NZB URLs to cli_mount and polls for completion."""

    PROVIDER_NAME = "cli_mount (Usenet)"

    def __init__(self):
        cfg = get_setting('Usenet Provider') or {}
        self.base_url = cfg.get('url', 'http://localhost:8888').rstrip('/')
        self.api_token = cfg.get('api_token', '')
        self.download_folder = cfg.get('download_folder', '')
        self.enabled = cfg.get('enabled', False)
        self.last_missing_segments = False  # set True by add_nzb_content on ARTICLE_NOT_FOUND

    def _headers(self) -> Dict[str, str]:
        h = {'Accept': 'application/json'}
        if self.api_token:
            h['Authorization'] = f'Bearer {self.api_token}'
        return h

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.base_url)

    def check_connectivity(self) -> tuple:
        """Returns (ok: bool, error: str|None)."""
        try:
            r = api.get(f'{self.base_url}/version', headers=self._headers(), timeout=10)
            if r.status_code == 200:
                return True, None
            return False, f'HTTP {r.status_code}'
        except Exception as exc:
            return False, str(exc)

    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '',
                        is_anime: bool = False, media_type: str = '',
                        tags=None, tags_exclusive: bool = False) -> Optional[str]:
        """Submit NZB content directly as a file upload to avoid double-fetching."""
        self.last_missing_segments = False
        if not self.is_enabled():
            logging.warning('[cli_mount] Usenet provider is disabled or not configured')
            return None

        filename = f"{title or 'download'}.nzb"
        fields = {'arr': (None, 'cli_debrid')}
        if category or self.download_folder:
            fields['downloadFolder'] = (None, category or self.download_folder)
        fields['nzbFiles'] = (filename, nzb_content.encode('utf-8'), 'application/x-nzb')

        try:
            r = api.post(
                f'{self.base_url}/api/add',
                headers=self._headers(),
                files=fields,
                timeout=30,
            )
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and result:
                    job = result[0]
                    job_id = job.get('id') or job.get('nzo_id') or job.get('hash', '')
                    if job.get('status') == 'error':
                        err_msg = job.get('error', '')
                        logging.error(f'[cli_mount] add_nzb_content error: {err_msg}')
                        if 'ARTICLE_NOT_FOUND' in err_msg or 'article not found' in err_msg.lower():
                            self.last_missing_segments = True  # callers can check this flag
                        return None
                    logging.info(f'[cli_mount] NZB content submitted: id={job_id} title={title!r}')
                    return str(job_id) if job_id else 'submitted'
                return 'submitted'
            logging.error(f'[cli_mount] add_nzb_content HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[cli_mount] add_nzb_content exception: {exc}')
            return None

    # Indexers known to block cli_mount's server-side URL fetches — always pre-fetch for these
    _PREFETCH_HOSTS = {'api.nzbgeek.info', 'nzbgeek.info'}

    def add_nzb(self, nzb_url: str, title: str = '', category: str = '',
                is_anime: bool = False, media_type: str = '',
                tags=None, tags_exclusive: bool = False) -> Optional[str]:
        """
        Submit an NZB URL to cli_mount.
        For indexers that block cli_mount's server-side fetches, pre-fetches the NZB
        content and uploads it directly instead of passing the URL.
        Returns the job ID string on success, None on failure.
        """
        if not self.is_enabled():
            logging.warning('[cli_mount] Usenet provider is disabled or not configured')
            return None

        # Pre-fetch for known problematic indexers using a SABnzbd User-Agent they accept
        try:
            from urllib.parse import urlparse
            host = urlparse(nzb_url).hostname or ''
            if host in self._PREFETCH_HOSTS:
                logging.info(f'[cli_mount] Pre-fetching NZB from {host} with SABnzbd User-Agent')
                _r = api.get(nzb_url, timeout=15, allow_redirects=True,
                             headers={'User-Agent': 'Sabnzbd/3.0.0'})
                if _r.status_code == 200 and '<nzb' in _r.text.lower():
                    return self.add_nzb_content(nzb_content=_r.text, title=title, category=category)
                logging.warning(f'[cli_mount] Pre-fetch failed for {host} (status={_r.status_code}), falling through to URL submit')
        except Exception as _pfe:
            logging.warning(f'[cli_mount] Pre-fetch error: {_pfe}, falling through to URL submit')

        # cli_mount's /api/add uses ParseMultipartForm — must send as multipart/form-data
        fields = [('nzbURLs', nzb_url)]
        if category or self.download_folder:
            fields.append(('downloadFolder', category or self.download_folder))
        fields.append(('arr', 'cli_debrid'))

        try:
            r = api.post(
                f'{self.base_url}/api/add',
                headers=self._headers(),
                files={k: (None, v) for k, v in fields},
                timeout=30,
            )
            if r.status_code == 200:
                result = r.json()
                if isinstance(result, list) and result:
                    job = result[0]
                    job_id = job.get('id') or job.get('nzo_id') or job.get('hash', '')
                    if job.get('status') == 'error':
                        logging.error(f'[cli_mount] add_nzb error: {job.get("error")}')
                        return None
                    logging.info(f'[cli_mount] NZB submitted: id={job_id} title={title!r}')
                    return str(job_id) if job_id else 'submitted'
                return 'submitted'
            logging.error(f'[cli_mount] add_nzb HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[cli_mount] add_nzb exception: {exc}')
            return None

    def _find_nzb_folder(self, job_norm: str, original_name: str = '', fast_check: bool = False) -> Optional[str]:
        """Find a cli_mount NZB folder by name.
        Tries direct URL lookup first (fast, single request).
        Falls back to paginated search only if direct lookup fails and fast_check=False.
        fast_check=True skips the paginated scan — use when folder may not exist yet
        (e.g. health check polling before rclone/DFS mount has reflected the new folder).
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        # Fast path: try direct URL with the original (un-normalised) name
        if original_name:
            try:
                r = api.get(f'{self.base_url}/api/browse/nzbs/{original_name}',
                            headers=self._headers(), timeout=10)
                if r.status_code == 200:
                    return original_name
            except Exception:
                pass

        # Skip paginated scan when fast_check=True — folder isn't visible yet on the mount
        if fast_check:
            return None

        # Slow path: paginate /api/browse/nzbs (only reached if direct lookup missed)
        try:
            page = 1
            while True:
                r = api.get(f'{self.base_url}/api/browse/nzbs',
                            headers=self._headers(), timeout=15,
                            params={'page': page, 'limit': 100})
                if r.status_code != 200:
                    break
                data = r.json()
                entries = data.get('entries', data) if isinstance(data, dict) else data
                for entry in (entries or []):
                    name = entry.get('name', '')
                    if not entry.get('is_dir'):
                        continue
                    name_norm = _norm(name)
                    if name_norm == job_norm or job_norm in name_norm or name_norm in job_norm:
                        return name
                total_pages = data.get('total_pages', 1) if isinstance(data, dict) else 1
                if page >= total_pages:
                    break
                page += 1
        except Exception as exc:
            logging.warning(f'[cli_mount] _find_nzb_folder error: {exc}')
        return None

    def _list_nzb_folder_files(self, folder_name: str) -> list:
        """Return all video files inside a cli_mount NZB folder."""
        _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
        try:
            r = api.get(f'{self.base_url}/api/browse/nzbs/{folder_name}',
                        headers=self._headers(), timeout=15)
            if r.status_code != 200:
                return []
            data = r.json()
            entries = data.get('entries', data) if isinstance(data, dict) else data
            return [
                (e.get('name', ''), e.get('size', 0) or 0)
                for e in (entries or [])
                if not e.get('is_dir') and os.path.splitext(e.get('name', ''))[1].lower() in _VIDEO_EXTS
            ]
        except Exception as exc:
            logging.warning(f'[cli_mount] _list_nzb_folder_files error for {folder_name!r}: {exc}')
        return []

    def get_nzb_file_info(self, job_name: str, season: int = None, episode: int = None, fast_check: bool = False) -> Optional[Tuple[str, str]]:
        """
        Find the downloaded folder and best-matching video file for a completed NZB job.
        If season/episode are provided, picks the file matching that episode.
        Otherwise picks the largest video file.
        Returns (folder_name, video_filename) or None if not found.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)

        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name, fast_check=fast_check)
            if not folder_name:
                logging.debug(f'[cli_mount] No folder found for job {job_name!r}')
                return None

            video_files = self._list_nzb_folder_files(folder_name)
            if not video_files:
                logging.warning(f'[cli_mount] No video files in folder {folder_name!r}')
                return folder_name, None

            # If season/episode provided, find the matching file
            best_file = None
            if season is not None and episode is not None:
                ep_pat = re.compile(
                    rf'[Ss]{season:02d}[Ee]{episode:02d}(?![0-9])',
                    re.IGNORECASE
                )
                for name, _ in video_files:
                    if ep_pat.search(name):
                        best_file = name
                        break

            # Fallback: largest file
            if not best_file:
                best_file = max(video_files, key=lambda x: x[1])[0]

            logging.info(f'[cli_mount] get_nzb_file_info: folder={folder_name!r} file={best_file!r}')
            return folder_name, best_file

        except Exception as exc:
            logging.warning(f'[cli_mount] get_nzb_file_info error for {job_name!r}: {exc}')
            return None

    def get_nzb_folder_all_files(self, job_name: str) -> Optional[Tuple[str, list]]:
        """
        Like get_nzb_file_info but returns ALL video files in the folder sorted by name.
        Returns (folder_name, [filename, ...]) or None if not found.
        Used for aggregate NZB packs where multiple episodes are in the same folder.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)
        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name)
            if not folder_name:
                return None
            video_files = sorted([name for name, _ in self._list_nzb_folder_files(folder_name)])
            logging.info(f'[cli_mount] get_nzb_folder_all_files: folder={folder_name!r} files={video_files}')
            return folder_name, video_files
        except Exception as exc:
            logging.warning(f'[cli_mount] get_nzb_folder_all_files error for {job_name!r}: {exc}')
            return None

    def remove_nzb(self, info_hash: str, entry_name: str = '') -> bool:
        """
        Delete a completed NZB entry from cli_mount — removes from entries.db AND mount.
        Uses DELETE /api/browse/torrents/{hash} which calls manager.DeleteEntry()
        and removes the entry from storage and the filesystem mount.
        Falls back to queue-only DELETE /api/torrents?hashes= if browse delete fails.
        Returns True if removed (or already gone), False on error.
        """
        if not self.is_enabled():
            return False

        # Primary: DELETE /api/browse/torrents/{hash} — removes from entries.db AND mount
        if info_hash:
            try:
                r = api.delete(
                    f'{self.base_url}/api/browse/torrents/{info_hash}',
                    headers=self._headers(), timeout=15,
                )
                if r.status_code in (200, 204):
                    logging.info(f'[cli_mount] Removed NZB entry {info_hash} from storage and mount')
                    return True
                if r.status_code == 404:
                    logging.info(f'[cli_mount] NZB entry {info_hash} already gone (404)')
                    return True
                logging.debug(f'[cli_mount] remove_nzb browse endpoint returned {r.status_code}')
            except Exception as e:
                logging.debug(f'[cli_mount] remove_nzb browse delete error: {e}')

        # Fallback: queue-only delete via hashes param
        if info_hash:
            try:
                r = api.delete(
                    f'{self.base_url}/api/torrents',
                    params={'hashes': info_hash},
                    headers=self._headers(), timeout=15,
                )
                if r.status_code in (200, 204):
                    logging.info(f'[cli_mount] Removed NZB job {info_hash} from queue')
                    return True
                if r.status_code == 404:
                    logging.info(f'[cli_mount] NZB job {info_hash} already gone (404)')
                    return True
            except Exception as e:
                logging.debug(f'[cli_mount] remove_nzb queue delete error: {e}')

        # Last resort: search by name and delete
        if entry_name:
            try:
                r = api.get(
                    f'{self.base_url}/api/torrents',
                    params={'search': entry_name[:60]},
                    headers=self._headers(), timeout=10,
                )
                if r.status_code == 200:
                    for t in r.json().get('torrents', []):
                        h = t.get('info_hash', '')
                        if h:
                            d = api.delete(
                                f'{self.base_url}/api/browse/torrents/{h}',
                                headers=self._headers(), timeout=10,
                            )
                            if d.status_code in (200, 204, 404):
                                logging.info(f'[cli_mount] Removed NZB by name search: {entry_name!r}')
                                return True
            except Exception as e:
                logging.debug(f'[cli_mount] remove_nzb name-search error: {e}')

        logging.warning(f'[cli_mount] Could not remove NZB hash={info_hash!r} name={entry_name!r}')
        return False

    def register_cli_ids(self, info_hash: str, ids: dict) -> bool:
        """
        Register cli_debrid item IDs on a cli_mount Entry.
        ids: {episode_filename: media_items_id} e.g. {"S01E01.mkv": 36810}
        Uses PATCH /api/entries/{hash}/cli_ids — merges into existing map.
        Returns True on success, False otherwise.
        """
        if not self.is_enabled() or not info_hash or not ids:
            return False
        try:
            import requests as _req_patch
            r = _req_patch.patch(
                f'{self.base_url}/api/entries/{info_hash}/cli_ids',
                json=ids,
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code in (200, 204):
                logging.debug(f'[cli_mount] Registered {len(ids)} cli_debrid IDs for {info_hash}')
                return True
            logging.debug(f'[cli_mount] register_cli_ids returned {r.status_code} for {info_hash}')
            return False
        except Exception as e:
            logging.debug(f'[cli_mount] register_cli_ids error for {info_hash}: {e}')
            return False

    def register_cli_ids_for_item(self, info_hash: str, item_id: int) -> bool:
        """
        Convenience wrapper: look up all siblings sharing item_id's filled_by_torrent_id,
        build the {filename: id} map, and call register_cli_ids.
        Safe to call from any rename path — errors are suppressed.
        """
        if not self.is_enabled() or not info_hash or not item_id:
            return False
        try:
            import os as _os
            from database.core import get_db_connection as _gdb
            _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
            with _gdb() as conn:
                row = conn.execute(
                    'SELECT filled_by_torrent_id FROM media_items WHERE id = ?', (item_id,)
                ).fetchone()
                if not row or not row[0]:
                    return False
                torrent_id = row[0]
                sibs = conn.execute(
                    "SELECT id, filled_by_file FROM media_items "
                    "WHERE (filled_by_torrent_id = ? OR filled_by_magnet LIKE ?) "
                    "AND state IN ('Checking','Collected','Upgrading')",
                    (torrent_id, f'%{info_hash}%')
                ).fetchall()
            ids = {
                s[1]: s[0] for s in sibs
                if s[1] and _os.path.splitext(s[1])[1].lower() in _VIDEO_EXTS
            }
            if ids:
                return self.register_cli_ids(info_hash, ids)
        except Exception as e:
            logging.debug(f'[cli_mount] register_cli_ids_for_item error: {e}')
        return False

    def push_tags_for_item(self, info_hash: str, item_id: int) -> bool:
        """
        Convenience wrapper: look up item_id's tags column and push immediately,
        instead of waiting for the next periodic climount_sync cycle.
        Safe to call from any rename/register path — errors are suppressed.
        """
        if not self.is_enabled() or not info_hash or not item_id:
            return False
        try:
            from database.core import get_db_connection as _gdb
            with _gdb() as conn:
                row = conn.execute(
                    'SELECT tags FROM media_items WHERE id = ?', (item_id,)
                ).fetchone()
                if row and row[0]:
                    ok = self.push_tags(info_hash, row[0])
                    if ok:
                        import datetime as _dt
                        conn.execute(
                            'UPDATE media_items SET tags_pushed_at = ? WHERE id = ?',
                            (_dt.datetime.now(), item_id)
                        )
                        conn.commit()
                    return ok
        except Exception as e:
            logging.debug(f'[cli_mount] push_tags_for_item error: {e}')
        return False

    def _plex_mode_enabled(self) -> bool:
        try:
            from utilities.settings import get_setting as _gs
            return _gs('File Management', 'file_collection_management', 'Plex') == 'Plex'
        except Exception:
            return False

    def push_tags(self, info_hash: str, tags) -> bool:
        """
        Push tags onto a cli_mount Entry (NZB or debrid torrent) by its info_hash.
        Uses the qBittorrent-compat POST /api/v2/torrents/addTags — cli_mount merges
        the given tags into the entry's existing Tags list (dedup, no overwrite).
        tags: comma-separated string or list/tuple of tag strings.
        Plex-mode-only feature — no-op under Symlinked/Local.
        Returns True on success, False otherwise.
        """
        if not self._plex_mode_enabled() or not self.is_enabled() or not info_hash or not tags:
            return False
        if isinstance(tags, (list, tuple, set)):
            tags_str = ','.join(str(t).strip() for t in tags if str(t).strip())
        else:
            tags_str = str(tags).strip()
        if not tags_str:
            return False
        try:
            import requests as _req_tags
            r = _req_tags.post(
                f'{self.base_url}/api/v2/torrents/addTags',
                headers=self._headers(),
                data={'hashes': info_hash, 'tags': tags_str},
                timeout=10,
            )
            if r.status_code in (200, 204):
                logging.debug(f'[cli_mount] Pushed tags {tags_str!r} for {info_hash}')
                return True
            logging.debug(f'[cli_mount] push_tags returned {r.status_code} for {info_hash}')
            return False
        except Exception as e:
            logging.debug(f'[cli_mount] push_tags error for {info_hash}: {e}')
            return False

    def remove_tags(self, info_hash: str, tags) -> bool:
        """
        Remove tags from a cli_mount Entry by its info_hash.
        Uses the qBittorrent-compat POST /api/v2/torrents/removeTags — cli_mount
        strips only the given tags, leaving any other existing tags intact.
        tags: comma-separated string or list/tuple of tag strings.
        Plex-mode-only feature — no-op under Symlinked/Local.
        Returns True on success, False otherwise.
        """
        if not self._plex_mode_enabled() or not self.is_enabled() or not info_hash or not tags:
            return False
        if isinstance(tags, (list, tuple, set)):
            tags_str = ','.join(str(t).strip() for t in tags if str(t).strip())
        else:
            tags_str = str(tags).strip()
        if not tags_str:
            return False
        try:
            import requests as _req_tags
            r = _req_tags.post(
                f'{self.base_url}/api/v2/torrents/removeTags',
                headers=self._headers(),
                data={'hashes': info_hash, 'tags': tags_str},
                timeout=10,
            )
            if r.status_code in (200, 204):
                logging.debug(f'[cli_mount] Removed tags {tags_str!r} for {info_hash}')
                return True
            logging.debug(f'[cli_mount] remove_tags returned {r.status_code} for {info_hash}')
            return False
        except Exception as e:
            logging.debug(f'[cli_mount] remove_tags error for {info_hash}: {e}')
            return False

    def rename_nzb(self, info_hash: str, new_name: str) -> bool:
        """
        Rename a cli_mount entry (NZB or debrid torrent) by its info_hash.
        Uses PATCH /api/browse/torrents/{hash}/rename — works for both ProtocolNZB
        and ProtocolTorrent entries since GetEntry() is protocol-agnostic.
        Returns True on success, False otherwise.
        """
        success, _not_found = self.rename_nzb_with_status(info_hash, new_name)
        return success

    def rename_nzb_with_status(self, info_hash: str, new_name: str) -> tuple:
        """
        Same as rename_nzb(), but also reports whether the failure was a 404
        (entry doesn't exist in cli_mount at all — retrying won't help) versus
        any other failure (worth retrying). Returns (success: bool, not_found: bool).
        not_found is only meaningful when success is False.
        """
        try:
            import json as _json
            import requests as _requests
            r = _requests.patch(
                f'{self.base_url}/api/browse/torrents/{info_hash}/rename',
                headers={**self._headers(), 'Content-Type': 'application/json'},
                data=_json.dumps({'name': new_name}),
                timeout=15,
            )
            if r.status_code == 200:
                logging.info(f'[cli_mount] Renamed entry {info_hash!r} -> {new_name!r}')
                return True, False
            logging.warning(f'[cli_mount] rename_nzb failed for {info_hash!r}: HTTP {r.status_code} {r.text[:100]}')
            return False, r.status_code == 404
        except Exception as exc:
            logging.warning(f'[cli_mount] rename_nzb error for {info_hash!r}: {exc}')
            return False, False

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Poll cli_mount queue for a single job by its ID/hash.
        Uses ?search= param which correctly filters by info_hash.
        The ?hash= param is unreliable and ignored by some cli_mount versions.
        Returns a dict with 'state' key: 'downloading' | 'completed' | 'failed' | 'unknown'.
        """
        try:
            r = api.get(
                f'{self.base_url}/api/torrents',
                params={'search': job_id, 'limit': 1},
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            torrents = data.get('torrents', data) if isinstance(data, dict) else data
            for t in (torrents or []):
                tid = str(t.get('info_hash') or t.get('id') or t.get('nzo_id') or t.get('hash', ''))
                if tid == job_id:
                    state = str(t.get('state', t.get('status', ''))).lower()
                    raw_progress = t.get('progress', 0)
                    progress_pct = int(float(raw_progress) * 100)
                    return {
                        'state': _map_state(state),
                        'progress': progress_pct,
                        'name': t.get('name', ''),
                        'raw': t,
                    }
            # Not found — may have completed and been removed
            return {'state': 'completed', 'progress': 100, 'raw': {}}
        except Exception as exc:
            logging.debug(f'[cli_mount] get_job_status exception: {exc}')
            return None

    def trigger_health_check(self, entry_name: str) -> bool:
        """POST to start a repair health check. Returns True if accepted, False otherwise."""
        try:
            import urllib.parse
            encoded = urllib.parse.quote(entry_name, safe='')
            r = api.post(
                f'{self.base_url}/api/repair/health/{encoded}/check',
                headers=self._headers(), timeout=5,
            )
            if r.status_code in (200, 202):
                return True
            log_fn = logging.debug if r.status_code == 400 else logging.warning
            log_fn(f'[cli_mount] health check trigger failed for {entry_name!r}: HTTP {r.status_code}')
            return False
        except Exception as exc:
            _msg = str(exc)
            _log = logging.debug if '400' in _msg else logging.warning
            _log(f'[cli_mount] trigger_health_check error for {entry_name!r}: {exc}')
            return False

    def poll_health_result(self, entry_name: str) -> Optional[str]:
        """GET the current health check result. Returns 'healthy', 'broken', or None if not ready."""
        try:
            import urllib.parse
            encoded = urllib.parse.quote(entry_name, safe='')
            r = api.get(
                f'{self.base_url}/api/repair/health/{encoded}',
                headers=self._headers(), timeout=4,
            )
            if r.status_code == 200:
                data = r.json()
                status = data.get('status') if isinstance(data, dict) else None
                if status in ('healthy', 'broken'):
                    logging.info(f'[cli_mount] health check for {entry_name!r}: {status}')
                    return status
            return None
        except Exception as exc:
            logging.debug(f'[cli_mount] poll_health_result error for {entry_name!r}: {exc}')
            return None

    def check_entry_health(self, entry_name: str) -> Optional[str]:
        """Trigger + poll in one call (legacy, used by non-health-check paths)."""
        if self.trigger_health_check(entry_name):
            return self.poll_health_result(entry_name)
        return None

    # -- repair-support (climount /api/repair/* + /api/torrents) ------------
    # Provider-agnostic repair interface (mirrored by NzbdavClient). Lets
    # repair_engine delegate instead of issuing raw HTTP itself.

    def _parse_health_entries(self, data) -> list:
        return data if isinstance(data, list) else data.get('entries', data.get('items', []))

    def _debrid_naming_enabled(self) -> bool:
        try:
            from utilities.settings import get_setting as _gs
            return bool(_gs('Debrid Provider', 'enable_debrid_naming', False))
        except Exception:
            return False

    def fetch_broken_items(self) -> list:
        """Return broken entries from cli_mount /api/repair/health.
        When debrid naming is enabled, filters out torrent protocol entries
        client-side to avoid false positives from renamed debrid torrents.
        """
        if not self.is_enabled():
            return []
        try:
            r = api.get(f'{self.base_url}/api/repair/health', headers=self._headers(), timeout=60)
            if r.status_code != 200:
                logging.warning(f'[cli_mount] /api/repair/health HTTP {r.status_code}')
                return []
            all_entries = self._parse_health_entries(r.json())
            broken = [e for e in all_entries if (e.get('status') or '').lower() == 'broken']
            if self._debrid_naming_enabled():
                before = len(broken)
                broken = [e for e in broken if (e.get('protocol') or '').lower() != 'torrent']
                if len(broken) < before:
                    logging.info(f'[cli_mount] fetch_broken_items: skipped {before - len(broken)} torrent entries (debrid naming)')
            logging.info(f'[cli_mount] fetch_broken_items: {len(broken)} broken')
            return broken
        except Exception as exc:
            logging.error(f'[cli_mount] fetch_broken_items error: {exc}')
            return []

    def get_health_summary(self) -> dict:
        """Counts by status from cli_mount /api/repair/health.
        When debrid naming is enabled, excludes torrent protocol entries
        to avoid showing false positives in the broken count.
        """
        if not self.is_enabled():
            return {}
        try:
            r = api.get(f'{self.base_url}/api/repair/health', headers=self._headers(), timeout=60)
            if r.status_code != 200:
                return {}
            entries = self._parse_health_entries(r.json())
            if self._debrid_naming_enabled():
                entries = [e for e in entries if (e.get('protocol') or '').lower() != 'torrent']
            counts: Dict[str, int] = {}
            for e in entries:
                s = (e.get('status') or 'unknown').lower()
                counts[s] = counts.get(s, 0) + 1
            return counts
        except Exception as exc:
            logging.debug(f'[cli_mount] get_health_summary error: {exc}')
            return {}

    def trigger_health_scan(self, full: bool = False, wait: bool = False, timeout: int = 300) -> bool:
        """POST /api/repair/run?protocol=nzb to scan NZB entries.
        full=True scans all files; full=False (default) only scans unchecked files.
        wait=True blocks until the scan completes (up to timeout seconds)."""
        if not self.is_enabled():
            return False
        try:
            import time as _t
            params = 'protocol=nzb'
            if full:
                params += '&ignore_last_checked=true'
            r = api.post(f'{self.base_url}/api/repair/run?{params}', headers=self._headers(), timeout=30)
            if r.status_code not in (200, 202, 204):
                return False
            if not wait:
                return True
            # Poll until the run completes
            run_id = (r.json() or {}).get('run_id', '')
            if not run_id:
                return True
            deadline = _t.time() + timeout
            while _t.time() < deadline:
                _t.sleep(30)
                try:
                    sr = api.get(f'{self.base_url}/api/repair/runs/{run_id}', headers=self._headers(), timeout=10)
                    if sr.status_code == 200:
                        status = (sr.json() or {}).get('status', '')
                        if status in ('completed', 'failed', 'error'):
                            logging.info(f'[cli_mount] NZB health scan completed: status={status}')
                            return True
                except Exception:
                    pass
            logging.warning('[cli_mount] NZB health scan wait timed out')
            return True
        except Exception as exc:
            logging.warning(f'[cli_mount] trigger_health_scan error: {exc}')
            return False

    def resolve_job_id(self, entry_name: str) -> str:
        """Resolve a job UUID from /api/torrents by exact name match."""
        if not self.is_enabled() or not entry_name:
            return ''
        try:
            r = api.get(f'{self.base_url}/api/torrents',
                        params={'search': entry_name[:60]},
                        headers=self._headers(), timeout=10)
            if r.status_code == 200:
                for t in r.json().get('torrents', []):
                    if t.get('name', '').strip() == entry_name.strip():
                        return t.get('info_hash', '')
        except Exception as exc:
            logging.debug(f'[cli_mount] resolve_job_id error: {exc}')
        return ''

    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool:
        """Poll until the job completes or timeout. Returns True on success."""
        if job_id in ('submitted', ''):
            # No trackable ID — assume success (climount accepted it)
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if not status:
                time.sleep(poll_interval)
                continue
            state = status.get('state', 'unknown')
            if state == 'completed':
                logging.info(f'[cli_mount] Job {job_id} completed')
                return True
            if state == 'failed':
                logging.error(f'[cli_mount] Job {job_id} failed')
                return False
            logging.debug(f'[cli_mount] Job {job_id} state={state} progress={status.get("progress", 0)}%')
            time.sleep(poll_interval)
        logging.warning(f'[cli_mount] Job {job_id} timed out after {timeout}s')
        return False

    def inject_sidecar_file(self, folder_name: str, filename: str, content: bytes) -> bool:
        """Inject a static file (e.g. subtitle) into a cli_mount DFS/WebDAV folder."""
        try:
            import urllib.parse
            encoded_folder = urllib.parse.quote(folder_name, safe='')
            encoded_file = urllib.parse.quote(filename, safe='')
            r = api.put(
                f'{self.base_url}/api/browse/nzbs/{encoded_folder}/{encoded_file}',
                data=content,
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code == 200:
                return True
            logging.warning(f'[cli_mount] inject_sidecar_file failed for {folder_name!r}/{filename!r}: HTTP {r.status_code}')
            return False
        except Exception as exc:
            logging.warning(f'[cli_mount] inject_sidecar_file error: {exc}')
            return False


def _map_state(raw: str) -> str:
    if raw in ('completed', 'downloaded', 'done', 'finished', 'cached'):
        return 'completed'
    if raw in ('failed', 'error', 'broken'):
        return 'failed'
    if raw in ('downloading', 'queued', 'processing', 'active', 'paused'):
        return 'downloading'
    return 'unknown'

# ---- Provider-switch factory (nzbdav-compat patch 2026-05-26) ----
# Delegates to nzbdav_client when 'Usenet Provider.provider' == 'nzbdav'.
# The CliMountClient class above is unchanged.

_client_instance = None


def _provider_key() -> str:
    try:
        from utilities.settings import get_setting
        cfg = get_setting('Usenet Provider') or {}
        return (cfg.get('provider') or 'climount').strip().lower()
    except Exception:
        return 'climount'


def get_climount_client():
    global _client_instance
    if _provider_key() == 'nzbdav':
        from .nzbdav_client import get_nzbdav_client
        return get_nzbdav_client()
    if _client_instance is None:
        _client_instance = CliMountClient()
    return _client_instance


def reset_climount_client() -> None:
    global _client_instance
    _client_instance = None
    if _provider_key() == 'nzbdav':
        from .nzbdav_client import reset_nzbdav_client
        reset_nzbdav_client()
