"""
Thin client for submitting NZB downloads to Decypharr.

Decypharr exposes a SABnzbd-compatible API at POST /api/add:
  - nzbURLs  : newline-separated URLs pointing to .nzb files
  - nzbFiles : multipart .nzb file uploads
  - arr       : category/arr name (optional)
  - downloadFolder : override download folder (optional)
  - action   : 'symlink' | 'move' | etc (optional)

Files land on the rclone mount automatically after Decypharr downloads them.
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any, Tuple

from utilities.settings import get_setting
from routes.api_tracker import api


class DecypharrClient:
    """Submits NZB URLs to Decypharr and polls for completion."""

    PROVIDER_NAME = "Decypharr (Usenet)"

    def __init__(self):
        cfg = get_setting('Usenet Provider') or {}
        self.base_url = cfg.get('url', 'http://localhost:8888').rstrip('/')
        self.api_token = cfg.get('api_token', '')
        self.download_folder = cfg.get('download_folder', '')
        self.enabled = cfg.get('enabled', False)

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

    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '') -> Optional[str]:
        """Submit NZB content directly as a file upload to avoid double-fetching."""
        if not self.is_enabled():
            logging.warning('[Decypharr] Usenet provider is disabled or not configured')
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
                        logging.error(f'[Decypharr] add_nzb_content error: {job.get("error")}')
                        return None
                    logging.info(f'[Decypharr] NZB content submitted: id={job_id} title={title!r}')
                    return str(job_id) if job_id else 'submitted'
                return 'submitted'
            logging.error(f'[Decypharr] add_nzb_content HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[Decypharr] add_nzb_content exception: {exc}')
            return None

    # Indexers known to block Decypharr's server-side URL fetches — always pre-fetch for these
    _PREFETCH_HOSTS = {'api.nzbgeek.info', 'nzbgeek.info'}

    def add_nzb(self, nzb_url: str, title: str = '', category: str = '') -> Optional[str]:
        """
        Submit an NZB URL to Decypharr.
        For indexers that block Decypharr's server-side fetches, pre-fetches the NZB
        content and uploads it directly instead of passing the URL.
        Returns the job ID string on success, None on failure.
        """
        if not self.is_enabled():
            logging.warning('[Decypharr] Usenet provider is disabled or not configured')
            return None

        # Pre-fetch for known problematic indexers using a SABnzbd User-Agent they accept
        try:
            from urllib.parse import urlparse
            host = urlparse(nzb_url).hostname or ''
            if host in self._PREFETCH_HOSTS:
                logging.info(f'[Decypharr] Pre-fetching NZB from {host} with SABnzbd User-Agent')
                _r = api.get(nzb_url, timeout=15, allow_redirects=True,
                             headers={'User-Agent': 'Sabnzbd/3.0.0'})
                if _r.status_code == 200 and '<nzb' in _r.text.lower():
                    return self.add_nzb_content(nzb_content=_r.text, title=title, category=category)
                logging.warning(f'[Decypharr] Pre-fetch failed for {host} (status={_r.status_code}), falling through to URL submit')
        except Exception as _pfe:
            logging.warning(f'[Decypharr] Pre-fetch error: {_pfe}, falling through to URL submit')

        # Decypharr's /api/add uses ParseMultipartForm — must send as multipart/form-data
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
                        logging.error(f'[Decypharr] add_nzb error: {job.get("error")}')
                        return None
                    logging.info(f'[Decypharr] NZB submitted: id={job_id} title={title!r}')
                    return str(job_id) if job_id else 'submitted'
                return 'submitted'
            logging.error(f'[Decypharr] add_nzb HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[Decypharr] add_nzb exception: {exc}')
            return None

    def get_nzb_file_info(self, job_name: str) -> Optional[Tuple[str, str]]:
        """
        Find the downloaded folder and largest video file for a completed NZB job.
        Searches /api/browse/nzbs for a folder matching job_name, then lists its contents.
        Returns (folder_name, video_filename) or None if not found.
        """
        _VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}

        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)

        try:
            # Paginate through /api/browse/nzbs to find matching folder
            folder_name = None
            page = 1
            while True:
                r = api.get(f'{self.base_url}/api/browse/nzbs',
                            headers=self._headers(), timeout=15,
                            params={'page': page})
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
                        folder_name = name
                        break
                if folder_name:
                    break
                total_pages = data.get('total_pages', 1) if isinstance(data, dict) else 1
                if page >= total_pages:
                    break
                page += 1

            if not folder_name:
                logging.debug(f'[Decypharr] No folder found for job {job_name!r}')
                return None

            # List files inside the folder
            r2 = api.get(f'{self.base_url}/api/browse/nzbs/{folder_name}',
                         headers=self._headers(), timeout=15)
            if r2.status_code != 200:
                return folder_name, None

            data2 = r2.json()
            entries2 = data2.get('entries', data2) if isinstance(data2, dict) else data2
            # Find largest video file
            best_file = None
            best_size = -1
            for entry in (entries2 or []):
                name = entry.get('name', '')
                size = entry.get('size', 0) or 0
                if not entry.get('is_dir') and os.path.splitext(name)[1].lower() in _VIDEO_EXTS:
                    if size > best_size:
                        best_size = size
                        best_file = name

            logging.info(f'[Decypharr] get_nzb_file_info: folder={folder_name!r} file={best_file!r}')
            return folder_name, best_file

        except Exception as exc:
            logging.warning(f'[Decypharr] get_nzb_file_info error for {job_name!r}: {exc}')
            return None

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Poll Decypharr queue for job status.
        Returns a dict with 'state' key: 'downloading' | 'completed' | 'failed' | 'unknown'.
        """
        try:
            r = api.get(
                f'{self.base_url}/api/torrents',
                headers=self._headers(),
                timeout=10,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            torrents = data.get('torrents', data) if isinstance(data, dict) else data
            for t in (torrents or []):
                tid = str(t.get('id') or t.get('nzo_id') or t.get('hash', ''))
                if tid == job_id:
                    state = str(t.get('state', t.get('status', ''))).lower()
                    # Decypharr progress is 0.0–1.0
                    raw_progress = t.get('progress', 0)
                    progress_pct = int(float(raw_progress) * 100)
                    return {
                        'state': _map_state(state),
                        'progress': progress_pct,
                        'name': t.get('name', ''),
                        'raw': t,
                    }
            # Not found in queue — may have completed and been removed
            return {'state': 'completed', 'progress': 100, 'raw': {}}
        except Exception as exc:
            logging.debug(f'[Decypharr] get_job_status exception: {exc}')
            return None

    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool:
        """Poll until the job completes or timeout. Returns True on success."""
        if job_id in ('submitted', ''):
            # No trackable ID — assume success (decypharr accepted it)
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if not status:
                time.sleep(poll_interval)
                continue
            state = status.get('state', 'unknown')
            if state == 'completed':
                logging.info(f'[Decypharr] Job {job_id} completed')
                return True
            if state == 'failed':
                logging.error(f'[Decypharr] Job {job_id} failed')
                return False
            logging.debug(f'[Decypharr] Job {job_id} state={state} progress={status.get("progress", 0)}%')
            time.sleep(poll_interval)
        logging.warning(f'[Decypharr] Job {job_id} timed out after {timeout}s')
        return False


def _map_state(raw: str) -> str:
    if raw in ('completed', 'downloaded', 'done', 'finished', 'cached'):
        return 'completed'
    if raw in ('failed', 'error', 'broken'):
        return 'failed'
    if raw in ('downloading', 'queued', 'processing', 'active', 'paused'):
        return 'downloading'
    return 'unknown'


_client_instance: Optional[DecypharrClient] = None


def get_decypharr_client() -> DecypharrClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = DecypharrClient()
    return _client_instance


def reset_decypharr_client() -> None:
    global _client_instance
    _client_instance = None
