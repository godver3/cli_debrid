"""
Thin client for submitting NZB downloads to NzbDAV (https://github.com/nzbdav-dev/nzbdav).

NzbDAV exposes a SABnzbd-compatible API at /api with mode-based query params:
  - mode=version       : version check
  - mode=addfile       : POST multipart .nzb file upload (field 'name'), with cat + nzbname
  - mode=addurl        : GET/POST with name=<URL>, cat + nzbname — nzbdav fetches the URL itself
  - mode=queue         : current download queue, returns slots[]
  - mode=history       : completed entries, returns slots[] with storage path
  - mode=history&name=delete&value=<nzo_id> : delete history entry
  - mode=get_cats      : configured categories (must include cat before submit)

Files appear on the WebDAV mount under two roots:
  /content/<cat>/<nzbname>/                    : virtual files served via WebDAV
  /completed-symlinks/<cat>/<nzbname>/         : symlinks to .ids/{uuid} per file
The `storage` field in history points to the completed-symlinks root.

Designed to match the DecypharrClient class interface so cli-debrid can route
either provider transparently via the factory in usenet/__init__.py.

LIMITATIONS vs decypharr:
  * NzbDAV has no user-triggered health-check API — health checks run internally
    via HealthCheckService. trigger_health_check() / poll_health_result() return
    no-op success so callers don't break, but cannot drive on-demand repair.
  * NzbDAV stores no separate 'downloadFolder' override — destination is purely
    determined by `cat`. The category must already exist in nzbdav's
    `api.categories` config (default: audio,software,tv,movies — extendable).
  * Browse-by-folder-name is not part of nzbdav's API; we fall back to a
    filesystem listing of the mounted WebDAV root (env var NZBDAV_MOUNT_PATH or
    config 'mounted_file_location' minus '/__all__').
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any, Tuple, List

from utilities.settings import get_setting
from routes.api_tracker import api


# Video-file extensions used for "is this a media file" checks in browse helpers.
_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}


class NzbdavClient:
    """Submits NZBs to nzbdav and polls for completion.

    Mirrors the DecypharrClient interface 1:1 so cli-debrid call sites are
    drop-in compatible. Differences in behaviour are documented per method.
    """

    PROVIDER_NAME = "NzbDAV (Usenet)"

    def __init__(self):
        cfg = get_setting('Usenet Provider') or {}
        self.base_url = cfg.get('url', 'http://localhost:3000').rstrip('/')
        # nzbdav uses ?apikey= for SAB-API auth — not Bearer headers.
        self.api_key = cfg.get('api_token', '')
        # category to default to when caller passes none
        self.default_category = cfg.get('download_folder', '') or 'cli_debrid'
        self.enabled = cfg.get('enabled', False)
        # Host-side filesystem path to where the nzbdav WebDAV mount appears
        # (used for browse helpers since nzbdav has no /browse API). Default to
        # the standard rclone-sidecar mount point shipped with nzbdav docs.
        self.mount_path = cfg.get('mounted_file_location', '').rstrip('/')
        if self.mount_path.endswith('/__all__'):
            self.mount_path = self.mount_path[: -len('/__all__')]
        if not self.mount_path:
            self.mount_path = '/mnt/remote/nzbdav'
        # Flag set by add_nzb_content when nzbdav reports ARTICLE_NOT_FOUND-style
        # errors (matches DecypharrClient.last_missing_segments contract).
        self.last_missing_segments = False

    # -- internal helpers ---------------------------------------------------

    def _sab_params(self, **extra) -> Dict[str, str]:
        """Build query params for nzbdav SAB-API calls. apikey is always added."""
        p: Dict[str, str] = {}
        if self.api_key:
            p['apikey'] = self.api_key
        p.update({k: str(v) for k, v in extra.items() if v is not None})
        return p

    def _sab_url(self) -> str:
        return f'{self.base_url}/api'

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.base_url)

    # -- 1:1 interface mirror ----------------------------------------------

    def check_connectivity(self) -> tuple:
        """Returns (ok: bool, error: str|None). Calls mode=version."""
        try:
            r = api.get(self._sab_url(), params=self._sab_params(mode='version'), timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get('version'):
                    return True, None
                return False, f'unexpected response: {str(data)[:200]}'
            return False, f'HTTP {r.status_code}'
        except Exception as exc:
            return False, str(exc)

    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '') -> Optional[str]:
        """Submit NZB content directly as a file upload.

        Matches DecypharrClient.add_nzb_content signature & return contract.
        Returns the nzo_id string on success, None on failure.
        """
        self.last_missing_segments = False
        if not self.is_enabled():
            logging.warning('[NzbDAV] Usenet provider is disabled or not configured')
            return None

        cat = category or self.default_category
        # nzbdav uses `nzbname` for the resulting folder name; default to title
        nzbname = title or 'download'
        filename = f'{nzbname}.nzb'

        try:
            r = api.post(
                self._sab_url(),
                params=self._sab_params(mode='addfile', cat=cat, nzbname=nzbname),
                files={'name': (filename, nzb_content.encode('utf-8'), 'application/x-nzb')},
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json() or {}
                if data.get('status') is True and data.get('nzo_ids'):
                    job_id = data['nzo_ids'][0]
                    logging.info(f'[NzbDAV] NZB content submitted: id={job_id} title={title!r}')
                    return str(job_id)
                err_msg = str(data.get('error') or '')
                logging.error(f'[NzbDAV] add_nzb_content error: {err_msg}')
                if 'ARTICLE_NOT_FOUND' in err_msg.upper() or 'article not found' in err_msg.lower():
                    self.last_missing_segments = True
                return None
            logging.error(f'[NzbDAV] add_nzb_content HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[NzbDAV] add_nzb_content exception: {exc}')
            return None

    def add_nzb(self, nzb_url: str, title: str = '', category: str = '') -> Optional[str]:
        """Submit an NZB URL — nzbdav fetches it server-side via mode=addurl.

        Falls back to pre-fetch + add_nzb_content if the server-side fetch fails
        (e.g. if the indexer blocks nzbdav's User-Agent).
        Matches DecypharrClient.add_nzb signature & return contract.
        """
        self.last_missing_segments = False
        if not self.is_enabled():
            logging.warning('[NzbDAV] Usenet provider is disabled or not configured')
            return None

        cat = category or self.default_category
        nzbname = title or 'download'

        try:
            r = api.post(
                self._sab_url(),
                params=self._sab_params(mode='addurl', name=nzb_url, cat=cat, nzbname=nzbname),
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json() or {}
                if data.get('status') is True and data.get('nzo_ids'):
                    job_id = data['nzo_ids'][0]
                    logging.info(f'[NzbDAV] NZB URL submitted: id={job_id} title={title!r}')
                    return str(job_id)
                err_msg = str(data.get('error') or '')
                # If server-side fetch fails, retry with pre-fetched content.
                if 'fetch' in err_msg.lower() or 'received status code' in err_msg.lower():
                    logging.info(f'[NzbDAV] addurl failed ({err_msg!r}), retrying with pre-fetched content')
                    try:
                        _r = api.get(nzb_url, timeout=15, allow_redirects=True,
                                     headers={'User-Agent': 'Sabnzbd/3.0.0'})
                        if _r.status_code == 200 and '<nzb' in _r.text.lower():
                            return self.add_nzb_content(_r.text, title=title, category=category)
                    except Exception as pe:
                        logging.warning(f'[NzbDAV] Pre-fetch retry error: {pe}')
                logging.error(f'[NzbDAV] add_nzb error: {err_msg}')
                return None
            logging.error(f'[NzbDAV] add_nzb HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[NzbDAV] add_nzb exception: {exc}')
            return None

    # -- browse helpers (filesystem-backed since nzbdav has no /browse API) -

    def _find_nzb_folder(self, job_norm: str, original_name: str = '') -> Optional[str]:
        """Find an nzbdav content folder matching the job-name.

        nzbdav has no `/api/browse/nzbs/<name>` endpoint, so we list the host
        mount filesystem under <mount_path>/content/<cat>/. We try `original_name`
        as a fast direct stat first, then fall through to a normalised scan.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        content_root = os.path.join(self.mount_path, 'content')
        if not os.path.isdir(content_root):
            logging.debug(f'[NzbDAV] content root not found: {content_root}')
            return None

        # Fast path: stat the direct path for each category subdir
        if original_name:
            for cat_dir in os.listdir(content_root):
                cand = os.path.join(content_root, cat_dir, original_name)
                if os.path.isdir(cand):
                    return original_name

        # Slow path: scan all subdirs and fuzzy-match the normalised name
        try:
            for cat_dir in os.listdir(content_root):
                cat_path = os.path.join(content_root, cat_dir)
                if not os.path.isdir(cat_path):
                    continue
                for entry in os.listdir(cat_path):
                    name_norm = _norm(entry)
                    if name_norm == job_norm or job_norm in name_norm or name_norm in job_norm:
                        return entry
        except Exception as exc:
            logging.warning(f'[NzbDAV] _find_nzb_folder error: {exc}')
        return None

    def _list_nzb_folder_files(self, folder_name: str) -> list:
        """Return all video files inside an nzbdav content folder (name, size)."""
        content_root = os.path.join(self.mount_path, 'content')
        if not os.path.isdir(content_root):
            return []
        # Search every category for the folder
        try:
            for cat_dir in os.listdir(content_root):
                folder_path = os.path.join(content_root, cat_dir, folder_name)
                if not os.path.isdir(folder_path):
                    continue
                # Walk one level deep — nzbdav may double-nest (release/release/files)
                results = []
                for root, _dirs, files in os.walk(folder_path):
                    for fname in files:
                        if os.path.splitext(fname)[1].lower() not in _VIDEO_EXTS:
                            continue
                        try:
                            size = os.path.getsize(os.path.join(root, fname))
                        except OSError:
                            size = 0
                        results.append((fname, size))
                return results
        except Exception as exc:
            logging.warning(f'[NzbDAV] _list_nzb_folder_files error for {folder_name!r}: {exc}')
        return []

    def get_nzb_file_info(self, job_name: str, season: int = None, episode: int = None) -> Optional[Tuple[str, str]]:
        """Find folder + best-matching video for a completed job.

        Identical signature & return contract to DecypharrClient.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)
        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name)
            if not folder_name:
                logging.debug(f'[NzbDAV] No folder found for job {job_name!r}')
                return None

            video_files = self._list_nzb_folder_files(folder_name)
            if not video_files:
                logging.warning(f'[NzbDAV] No video files in folder {folder_name!r}')
                return folder_name, None

            best_file = None
            if season is not None and episode is not None:
                ep_pat = re.compile(
                    rf'[Ss]{season:02d}[Ee]{episode:02d}(?![0-9])',
                    re.IGNORECASE,
                )
                for name, _ in video_files:
                    if ep_pat.search(name):
                        best_file = name
                        break

            if not best_file:
                best_file = max(video_files, key=lambda x: x[1])[0]

            logging.info(f'[NzbDAV] get_nzb_file_info: folder={folder_name!r} file={best_file!r}')
            return folder_name, best_file
        except Exception as exc:
            logging.warning(f'[NzbDAV] get_nzb_file_info error for {job_name!r}: {exc}')
            return None

    def get_nzb_folder_all_files(self, job_name: str) -> Optional[Tuple[str, list]]:
        """Return all video files in folder, sorted by name."""
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)
        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name)
            if not folder_name:
                return None
            video_files = sorted([name for name, _ in self._list_nzb_folder_files(folder_name)])
            logging.info(f'[NzbDAV] get_nzb_folder_all_files: folder={folder_name!r} files={video_files}')
            return folder_name, video_files
        except Exception as exc:
            logging.warning(f'[NzbDAV] get_nzb_folder_all_files error for {job_name!r}: {exc}')
            return None

    # -- queue / removal ----------------------------------------------------

    def remove_nzb(self, info_hash: str, entry_name: str = '') -> bool:
        """Delete a history entry from nzbdav.

        info_hash here is the nzo_id (UUID). nzbdav's SAB-compatible delete:
          /api?mode=history&name=delete&value=<nzo_id>
        Returns True if removed (or already gone), False on hard error.
        """
        if not self.is_enabled():
            return False
        if not info_hash:
            logging.debug(f'[NzbDAV] remove_nzb called without info_hash for {entry_name!r}')
            return False
        try:
            r = api.delete(
                self._sab_url(),
                params=self._sab_params(mode='history', name='delete', value=info_hash),
                timeout=15,
            )
            if r.status_code in (200, 204):
                logging.info(f'[NzbDAV] Removed NZB job {info_hash}')
                return True
            if r.status_code == 404:
                logging.info(f'[NzbDAV] NZB job {info_hash} already gone (404)')
                return True
            logging.warning(f'[NzbDAV] remove_nzb returned HTTP {r.status_code}: {r.text[:200]}')
            return False
        except Exception as exc:
            logging.debug(f'[NzbDAV] remove_nzb error: {exc}')
            return False

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Poll nzbdav for a single job's state.

        Strategy:
          1. Check mode=queue — if found, returns state=downloading/queued
          2. If not in queue, check mode=history — if found, completed
          3. Otherwise unknown (likely deleted/expired)
        Returns dict matching DecypharrClient.get_job_status shape.
        """
        try:
            # Queue first
            r = api.get(self._sab_url(), params=self._sab_params(mode='queue'), timeout=10)
            if r.status_code == 200:
                q = r.json().get('queue', {}) if isinstance(r.json(), dict) else {}
                for slot in q.get('slots', []) or []:
                    if str(slot.get('nzo_id', '')) == job_id:
                        state = str(slot.get('status', '')).lower()
                        # nzbdav uses values like Downloading, Queued, Paused, Completed
                        try:
                            progress = int(float(slot.get('percentage', 0)))
                        except (ValueError, TypeError):
                            progress = 0
                        return {
                            'state': _map_state(state),
                            'progress': progress,
                            'name': slot.get('filename', ''),
                            'raw': slot,
                        }

            # History fallback
            r = api.get(
                self._sab_url(),
                params=self._sab_params(mode='history', limit=500),
                timeout=10,
            )
            if r.status_code == 200:
                h = r.json().get('history', {}) if isinstance(r.json(), dict) else {}
                for slot in h.get('slots', []) or []:
                    if str(slot.get('nzo_id', '')) == job_id:
                        state = str(slot.get('status', '')).lower()
                        return {
                            'state': _map_state(state),
                            'progress': 100 if state == 'completed' else 0,
                            'name': slot.get('name', ''),
                            'raw': slot,
                        }

            # Not in queue or history — assume completed-and-cleaned
            return {'state': 'completed', 'progress': 100, 'raw': {}}
        except Exception as exc:
            logging.debug(f'[NzbDAV] get_job_status exception: {exc}')
            return None

    # -- health-check stubs --------------------------------------------------
    # nzbdav has internal auto-health-check (HealthCheckService); there is no
    # user-callable trigger/poll endpoint. We return success/None so callers
    # don't error out, but the actual repair is driven by nzbdav itself.

    def trigger_health_check(self, entry_name: str) -> bool:
        """No-op for nzbdav — internal HealthCheckService runs automatically.

        Returns True so callers continue with their normal flow.
        """
        logging.debug(f'[NzbDAV] trigger_health_check no-op for {entry_name!r} '
                      '(nzbdav health checks run internally)')
        return True

    def poll_health_result(self, entry_name: str) -> Optional[str]:
        """No-op for nzbdav — returns None so callers treat as 'not ready'."""
        return None

    def check_entry_health(self, entry_name: str) -> Optional[str]:
        """Legacy wrapper — returns None for nzbdav (no user-driven health-API)."""
        return None

    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool:
        """Poll until the job completes or timeout."""
        if job_id in ('submitted', ''):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if not status:
                time.sleep(poll_interval)
                continue
            state = status.get('state', 'unknown')
            if state == 'completed':
                logging.info(f'[NzbDAV] Job {job_id} completed')
                return True
            if state == 'failed':
                logging.error(f'[NzbDAV] Job {job_id} failed')
                return False
            logging.debug(f'[NzbDAV] Job {job_id} state={state} progress={status.get("progress", 0)}%')
            time.sleep(poll_interval)
        logging.warning(f'[NzbDAV] Job {job_id} timed out after {timeout}s')
        return False


def _map_state(raw: str) -> str:
    """Map nzbdav SAB-API status values to cli-debrid's canonical states."""
    raw = raw.lower()
    if raw in ('completed', 'downloaded', 'done', 'finished'):
        return 'completed'
    if raw in ('failed', 'error'):
        return 'failed'
    if raw in ('downloading', 'queued', 'processing', 'active', 'paused'):
        return 'downloading'
    return 'unknown'


_client_instance: Optional[NzbdavClient] = None


def get_nzbdav_client() -> NzbdavClient:
    """Return the singleton NzbdavClient instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = NzbdavClient()
    return _client_instance


def reset_nzbdav_client() -> None:
    """Force re-creation of the singleton on next access (call after config change)."""
    global _client_instance
    _client_instance = None
