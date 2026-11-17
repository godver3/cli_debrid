"""
Client for the optional built-in mount engine (engine/, `clid-engine`).

Selected with `Usenet Provider.provider = builtin`. The engine runs on the
same machine (supervised by cli_debrid) and serves a localhost JSON API;
this client translates it into the same method set and return shapes as
CliMountClient, so every caller works unchanged.

Not supported by the engine (capabilities left out): debrid torrent sync,
sidecar uploads (the mount is read-only) and log sharing.
"""

import collections
import gzip
import logging
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from utilities.settings import get_setting
from usenet.base import (
    UnsupportedFeaturesMixin, RENAME, TAGS, CLI_IDS, EXACT_REMOVE, QUEUE,
    REPAIR_API, STATS, SYNC_FEED, LOGS,
)

_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}
_LOG = '[builtin]'

# Engine log lines are Go slog text: time=... level=INFO msg="..." key=value
_SLOG_RE = re.compile(r'^time=(\S+) level=(\S+) msg=("(?:[^"\\]|\\.)*"|\S+)\s*(.*)$')


def engine_log_path() -> str:
    return os.path.join(os.environ.get('USER_LOGS', '/user/logs'), 'mount_engine.log')


def format_engine_log_line(line: str) -> str:
    """slog text -> cli_mount's "date time | LEVEL | [mount] message" so the
    Logs page's cli_mount tab renders, colours and filters it unchanged."""
    m = _SLOG_RE.match(line)
    if not m:
        return line
    ts, level, msg, rest = m.groups()
    if msg.startswith('"'):
        msg = msg[1:-1].replace('\\"', '"')
    ts = ts.replace('T', ' ', 1)[:23]
    return f'{ts} | {level.upper()} | [mount] {msg}' + (f' {rest}' if rest else '')


def _state(status: str) -> str:
    """Engine job status -> CliMountClient state."""
    return {'ready': 'completed', 'failed': 'failed',
            'queued': 'downloading', 'importing': 'downloading'}.get(status, 'unknown')


def _folder(job: dict) -> str:
    path = job.get('path') or ''
    return path.rsplit('/', 1)[-1] if path else ''


class BuiltinClient(UnsupportedFeaturesMixin):
    PROVIDER_NAME = "Built-in mount (Usenet)"
    CAPABILITIES = frozenset({RENAME, TAGS, CLI_IDS, EXACT_REMOVE, QUEUE, REPAIR_API, STATS, SYNC_FEED, LOGS})

    def __init__(self):
        up = get_setting('Usenet Provider') or {}
        cfg = get_setting('Built-in Mount') or {}
        self.enabled = bool(up.get('enabled')) and (up.get('provider') or '').strip().lower() == 'builtin'
        self.base_url = f"http://127.0.0.1:{int(cfg.get('api_port') or 8283)}"
        self._configured_token = cfg.get('api_token') or ''
        self.mount_path = (cfg.get('mount_path') or '/mnt/clid').rstrip('/')
        self.download_folder = up.get('download_folder', '')
        self.last_missing_segments = False
        self.last_failed_job_id = ''

    # ---- plumbing --------------------------------------------------------

    @property
    def api_token(self) -> str:
        """The configured token, else the one utilities/builtin_engine.py
        generated into engine.json. Read per call, not cached: the client is
        often created before startup sync has written the engine config."""
        if self._configured_token:
            return self._configured_token
        try:
            from utilities.builtin_engine import current_token
            return current_token()
        except Exception:
            return ''

    def _headers(self) -> Dict[str, str]:
        h = {'Accept': 'application/json'}
        token = self.api_token
        if token:
            h['Authorization'] = f'Bearer {token}'
        return h

    def _call(self, method: str, path: str, timeout: float = 15, **kw) -> Tuple[Optional[int], Any]:
        """(status, json) for an engine call; (None, None) when unreachable."""
        try:
            r = requests.request(method, f'{self.base_url}{path}', headers=self._headers(), timeout=timeout, **kw)
        except Exception as exc:
            logging.debug(f'{_LOG} {method} {path} failed: {exc}')
            return None, None
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = None
        return r.status_code, body

    def _jobs(self, status: str = '') -> List[dict]:
        code, body = self._call('GET', '/jobs', params={'status': status} if status else None)
        return body if code == 200 and isinstance(body, list) else []

    def _job_by_folder(self, folder: str) -> Optional[dict]:
        folder = (folder or '').strip()
        if not folder:
            return None
        for j in self._jobs():
            if _folder(j) == folder or (j.get('name') or '').strip() == folder:
                return j
        return None

    # ---- core --------------------------------------------------------------

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.base_url)

    def check_connectivity(self) -> tuple:
        code, body = self._call('GET', '/status', timeout=10)
        if code == 200:
            return True, None
        return False, ('built-in mount engine is not running' if code is None else f'HTTP {code}')

    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '',
                        is_anime: bool = False, media_type: str = '',
                        tags=None, tags_exclusive: bool = False) -> Optional[str]:
        # The engine imports in the background: a missing article shows up
        # later as a failed job (get_job_status), not here.
        self.last_missing_segments = False
        self.last_failed_job_id = ''
        if not self.is_enabled():
            logging.warning(f'{_LOG} Usenet provider is disabled or not configured')
            return None
        params = {'name': title or '', 'category': category or self.download_folder or ''}
        data = nzb_content.encode('utf-8') if isinstance(nzb_content, str) else nzb_content
        code, body = self._call('POST', '/jobs', timeout=30, params=params, data=data)
        if code == 201 and isinstance(body, dict) and body.get('id'):
            logging.info(f'{_LOG} NZB submitted: id={body["id"]} title={title!r}')
            if tags:
                self.push_tags(body['id'], tags)
            return str(body['id'])
        logging.error(f'{_LOG} add_nzb_content failed: HTTP {code} {body}')
        return None

    def add_nzb(self, nzb_url: str, title: str = '', category: str = '',
                is_anime: bool = False, media_type: str = '',
                tags=None, tags_exclusive: bool = False) -> Optional[str]:
        """The engine never fetches URLs itself: download here (through the
        API tracker, which also counts the indexer grab) and upload."""
        self.last_failed_job_id = ''
        if not self.is_enabled():
            return None
        try:
            from routes.api_tracker import api
            r = api.get(nzb_url, timeout=20, allow_redirects=True, headers={'User-Agent': 'Sabnzbd/3.0.0'})
            if r.status_code != 200 or '<nzb' not in r.text.lower():
                logging.error(f'{_LOG} NZB download failed: HTTP {r.status_code}')
                return None
        except Exception as exc:
            logging.error(f'{_LOG} NZB download failed: {exc}')
            return None
        return self.add_nzb_content(r.text, title=title, category=category, tags=tags)

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        code, body = self._call('GET', f'/jobs/{job_id}', timeout=10)
        if code == 404:
            # Same "not found" signal as CliMountClient: empty raw.
            return {'state': 'completed', 'progress': 100, 'raw': {}}
        if code != 200 or not isinstance(body, dict):
            return None
        state = _state(body.get('status', ''))
        raw = dict(body)
        raw.setdefault('info_hash', body.get('id'))
        if state == 'failed' and body.get('error'):
            raw['error'] = body['error']
        return {'state': state, 'progress': 100 if state == 'completed' else 0,
                'name': _folder(body) or body.get('name', ''), 'raw': raw}

    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if status and status.get('raw'):
                if status['state'] == 'completed':
                    return True
                if status['state'] == 'failed':
                    return False
            time.sleep(poll_interval)
        return False

    def remove_nzb(self, info_hash: str, entry_name: str = '') -> bool:
        if not self.is_enabled():
            return False
        if info_hash:
            code, _ = self._call('DELETE', f'/jobs/{info_hash}')
            if code in (204, 404):
                _mark_deleted(info_hash)
                return True
        if entry_name:
            job = self._job_by_folder(entry_name)
            if job is None:
                return True
            code, _ = self._call('DELETE', f'/jobs/{job["id"]}')
            if code in (204, 404):
                _mark_deleted(job['id'])
                return True
        logging.warning(f'{_LOG} Could not remove job hash={info_hash!r} name={entry_name!r}')
        return False

    def remove_nzb_exact(self, info_hash: str) -> bool:
        if not self.is_enabled() or not info_hash:
            return False
        code, _ = self._call('DELETE', f'/jobs/{info_hash}')
        if code in (204, 404):
            _mark_deleted(info_hash)
            return True
        return False

    def resolve_job_id(self, entry_name: str) -> str:
        return self.find_job_hash_by_name(entry_name)

    # ---- files (shared selection logic from CliMountClient) -----------------

    def _find_nzb_folder(self, job_norm: str, original_name: str = '', fast_check: bool = False) -> Optional[str]:
        jobs = [j for j in self._jobs('ready') if _folder(j)]
        if original_name:
            for j in jobs:
                if _folder(j) == original_name or j.get('name') == original_name:
                    return _folder(j)
        if fast_check:
            return None
        norm = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
        for j in jobs:
            n = norm(_folder(j))
            if n and (n == job_norm or job_norm in n or n in job_norm):
                return _folder(j)
        return None

    def _list_nzb_folder_files(self, folder_name: str) -> list:
        job = self._job_by_folder(folder_name)
        if not job:
            return []
        code, body = self._call('GET', f'/jobs/{job["id"]}')
        if code != 200 or not isinstance(body, dict):
            return []
        return [(e['name'], e.get('size', 0) or 0) for e in body.get('entries') or []
                if os.path.splitext(e.get('name', ''))[1].lower() in _VIDEO_EXTS]

    # ---- rename / ids / tags -------------------------------------------------

    def rename_nzb(self, info_hash: str, new_name: str) -> bool:
        return self.rename_nzb_with_status(info_hash, new_name)[0]

    def rename_nzb_with_status(self, info_hash: str, new_name: str) -> tuple:
        code, _ = self._call('PATCH', f'/jobs/{info_hash}', json={'name': new_name})
        if code == 200:
            logging.info(f'{_LOG} Renamed {info_hash!r} -> {new_name!r}')
            try:
                from utilities.debrid_rename_reconcile import repoint_symlinks_after_rename_async
                repoint_symlinks_after_rename_async(info_hash, new_name)
            except Exception as exc:
                logging.debug(f'{_LOG} symlink re-point not started for {info_hash!r}: {exc}')
            return True, False
        return False, code == 404

    def register_cli_ids(self, info_hash: str, ids: dict) -> bool:
        if not self.is_enabled() or not info_hash or not ids:
            return False
        code, _ = self._call('PATCH', f'/jobs/{info_hash}', json={'cli_ids': {str(k): int(v) for k, v in ids.items()}})
        return code == 200

    def _plex_mode_enabled(self) -> bool:
        return get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'

    @staticmethod
    def _tag_list(tags) -> List[str]:
        if isinstance(tags, (list, tuple, set)):
            return [str(t).strip() for t in tags if str(t).strip()]
        return [t.strip() for t in str(tags or '').split(',') if t.strip()]

    def _set_tags(self, info_hash: str, change) -> bool:
        code, body = self._call('GET', f'/jobs/{info_hash}')
        if code != 200 or not isinstance(body, dict):
            return False
        tags = change(list(body.get('tags') or []))
        code, _ = self._call('PATCH', f'/jobs/{info_hash}', json={'tags': tags})
        return code == 200

    def push_tags(self, info_hash: str, tags) -> bool:
        """Merge tags onto a job (Plex-mode only, like cli_mount)."""
        new = self._tag_list(tags)
        if not self._plex_mode_enabled() or not self.is_enabled() or not info_hash or not new:
            return False
        return self._set_tags(info_hash, lambda cur: cur + [t for t in new if t not in cur])

    def remove_tags(self, info_hash: str, tags) -> bool:
        gone = set(self._tag_list(tags))
        if not self._plex_mode_enabled() or not self.is_enabled() or not info_hash or not gone:
            return False
        return self._set_tags(info_hash, lambda cur: [t for t in cur if t not in gone])

    # ---- queue listing -------------------------------------------------------

    def iter_queue_jobs(self, search: str = '', page_size: int = 50, sort: bool = True,
                        timeout: int = 10, retries: int = 1) -> Iterator[dict]:
        needle = (search or '').lower()
        for j in self._jobs():
            name = _folder(j) or j.get('name', '')
            if needle and needle not in name.lower() and needle not in j.get('id', '').lower():
                continue
            yield {'name': name, 'info_hash': j.get('id', ''), 'state': _state(j.get('status', '')),
                   'status': j.get('status', ''), 'progress': 1.0 if j.get('status') == 'ready' else 0.0,
                   'original_filename': j.get('name', '')}

    def find_job_hash_by_name(self, name: str, page_size: int = 50, sort: bool = True) -> str:
        name = (name or '').strip()
        if not name:
            return ''
        for t in self.iter_queue_jobs():
            if t['name'].strip() == name or t['original_filename'].strip() == name:
                return t['info_hash']
        return ''

    def delete_queue_job(self, info_hash: str) -> Optional[int]:
        code, _ = self._call('DELETE', f'/jobs/{info_hash}')
        if code in (204, 404):
            _mark_deleted(info_hash)
            return 200  # callers compare with cli_mount's 200
        return code

    def iter_library_nzbs(self, page_size: int = 100, timeout: int = 10) -> Iterator[dict]:
        for j in self._jobs('ready'):
            if _folder(j):
                yield {'name': _folder(j), 'is_dir': True}

    # ---- health / repair -----------------------------------------------------

    def _broken_entries(self) -> List[dict]:
        """Broken files in cli_mount's /api/repair/health shape."""
        code, body = self._call('GET', '/health', params={'status': 'broken'})
        if code != 200 or not isinstance(body, list):
            return []
        out = []
        for j in body:
            health = j.get('health') or {}
            files = [{'file_name': b.get('file_name', ''), 'info_hash': j.get('id', ''),
                      'cli_debrid_id': (j.get('cli_ids') or {}).get(b.get('file_name', '')),
                      'reason': b.get('reason', '')} for b in health.get('broken_files') or []]
            out.append({'entry_name': _folder(j) or j.get('name', ''), 'name': _folder(j) or j.get('name', ''),
                        'info_hash': j.get('id', ''), 'protocol': 'nzb', 'status': 'broken',
                        'failure_reason': files[0]['reason'] if files else '', 'broken_files': files})
        return out

    def list_repair_health(self, timeout: int = 60, status: str = '') -> Optional[list]:
        code, body = self._call('GET', '/health', timeout=timeout, params={'status': status} if status else None)
        if code != 200 or not isinstance(body, list):
            return None
        if status == 'broken':
            return self._broken_entries()
        return [{'entry_name': _folder(j) or j.get('name', ''), 'info_hash': j.get('id', ''), 'protocol': 'nzb',
                 'status': (j.get('health') or {}).get('status', 'unknown')} for j in body]

    def fetch_broken_items(self) -> list:
        """One item per broken file, like CliMountClient.fetch_broken_items."""
        if not self.is_enabled():
            return []
        items = []
        for e in self._broken_entries():
            for f in e['broken_files'] or [{}]:
                item = dict(e)
                item['file_name'] = f.get('file_name', '')
                item['info_hash'] = f.get('info_hash') or e['info_hash']
                item['cli_debrid_id'] = f.get('cli_debrid_id')
                item['failure_reason'] = f.get('reason') or e['failure_reason']
                item['hash_is_authoritative'] = bool(item['info_hash'])
                items.append(item)
        logging.info(f'{_LOG} fetch_broken_items: {len(items)} broken')
        return items

    def get_health_summary(self) -> dict:
        code, body = self._call('GET', '/health')
        if code != 200 or not isinstance(body, list):
            return {}
        counts: Dict[str, int] = {}
        for j in body:
            s = (j.get('health') or {}).get('status', 'unknown')
            counts[s] = counts.get(s, 0) + 1
        return counts

    def trigger_health_check(self, entry_name: str) -> bool:
        job = self._job_by_folder(entry_name)
        if not job:
            return False
        code, _ = self._call('POST', f'/jobs/{job["id"]}/health')
        return code == 202

    def poll_health_result(self, entry_name: str) -> Optional[str]:
        job = self._job_by_folder(entry_name)
        if not job:
            return None
        code, body = self._call('GET', f'/jobs/{job["id"]}/health', timeout=4)
        status = (body or {}).get('status') if code == 200 else None
        return status if status in ('healthy', 'broken') else None

    def check_entry_health(self, entry_name: str) -> Optional[str]:
        if self.trigger_health_check(entry_name):
            return self.poll_health_result(entry_name)
        return None

    def run_health_scan(self, protocol: str, full: bool = False, wait: bool = False,
                        timeout: int = 300, log_label: str = '') -> bool:
        if protocol != 'nzb':
            return False  # the engine holds no debrid torrents yet
        q = {'full': 'true' if full else 'false', 'ignore_last_checked': 'true' if full else 'false'}
        code, run = self._call('POST', '/health/run', params=q)
        if code != 202:
            return False
        deadline = time.time() + timeout
        while wait and time.time() < deadline:
            time.sleep(5)
            c, cur = self._call('GET', '/health/run')
            if c == 200 and isinstance(cur, dict) and cur.get('status') in ('completed', 'stopped'):
                break
        return True

    def trigger_health_scan(self, full: bool = False, wait: bool = False, timeout: int = 300) -> bool:
        return self.is_enabled() and self.run_health_scan('nzb', full=full, wait=wait, timeout=timeout)

    def get_repair_status(self) -> Optional[dict]:
        code, run = self._call('GET', '/health/run', timeout=10)
        if code != 200 or not isinstance(run, dict):
            return None
        return {'active_run': run if run.get('status') == 'running' else None}

    def stop_repair(self) -> Optional[int]:
        code, _ = self._call('POST', '/health/stop', timeout=10)
        return code

    def delete_repair_health(self, entry_name: str) -> Optional[int]:
        job = self._job_by_folder(entry_name)
        if not job:
            return 404
        code, _ = self._call('DELETE', f'/jobs/{job["id"]}/health')
        return 200 if code == 204 else code

    def recheck_entry_health(self, entry_name: str, timeout: int = 30) -> Optional[int]:
        job = self._job_by_folder(entry_name)
        if not job:
            return 404
        code, _ = self._call('POST', f'/jobs/{job["id"]}/health', timeout=timeout)
        return 200 if code == 202 else code

    def post_repair_replacement(self, action: str, payload: dict, timeout: int = 20):
        if action not in ('ack', 'verify'):
            raise ValueError(f'unknown replacement action {action!r}')
        try:
            r = requests.post(f'{self.base_url}/replacements/{action}', json=payload,
                              headers=self._headers(), timeout=timeout)
            try:
                body = r.json()
            except ValueError:
                body = {}
            return r.status_code, body if isinstance(body, dict) else {}, ''
        except Exception as exc:
            return 0, {}, str(exc)

    # ---- stats / sync feed ---------------------------------------------------

    def get_streams(self) -> List[dict]:
        """Files open on the mount right now (engine GET /streams)."""
        code, body = self._call('GET', '/streams', timeout=5)
        return body if code == 200 and isinstance(body, list) else []

    def prewarm(self, job_id: str, file: str, nbytes: int) -> bool:
        """Ask the engine to fetch the start of a file in the background."""
        code, _ = self._call('POST', '/prewarm', timeout=5, json={'job_id': job_id, 'file': file, 'bytes': int(nbytes)})
        return code == 202

    # ---- logs (capability LOGS) -------------------------------------------

    def fetch_logs(self, lines: int = 1000, level: str = 'all') -> dict:
        """The engine's recent log lines (its supervisord log file, in this
        container), in the shape the Logs page's mount tab expects."""
        lines = max(1, min(int(lines or 1000), 10000))
        want = '' if level in ('', 'all') else level.upper()
        if want == 'WARNING':
            want = 'WARN'
        tail = collections.deque(maxlen=lines)
        try:
            with open(engine_log_path(), encoding='utf-8', errors='replace') as f:
                for raw in f:
                    raw = raw.rstrip('\n')
                    if not raw:
                        continue
                    line = format_engine_log_line(raw)
                    if want and f'| {want} |' not in line:
                        continue
                    tail.append(line)
        except FileNotFoundError:
            pass
        return {'lines': list(tail), 'total': len(tail)}

    def share_logs(self) -> str:
        """Upload the engine log (and its rotated copies) like cli_debrid's own
        Share Logs; returns the URL. Raises on failure."""
        path = engine_log_path()
        parts = []
        for p in [f'{path}.{i}' for i in range(5, 0, -1)] + [path]:  # oldest first
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    parts.append(f.read())
        if not parts:
            raise Exception('the mount engine has not written a log yet')
        from routes.log_viewer_routes import upload_to_paste_cnet
        url = upload_to_paste_cnet(gzip.compress(b''.join(parts)))
        if not url:
            raise Exception('upload failed')
        return url

    def get_debug_stats(self, timeout: int = 10) -> Optional[dict]:
        """The engine's stats in the /debug/stats shape the stats page reads."""
        code, st = self._call('GET', '/stats', timeout=timeout)
        if code != 200 or not isinstance(st, dict):
            return None
        pools = st.get('pools') or []
        broken = len(self._broken_entries())
        return {
            'usenet': {'pool': {
                'active': sum(max(0, p.get('open', 0) - p.get('idle', 0)) for p in pools),
                'idle': sum(p.get('idle', 0) for p in pools),
                'max_connections': sum(p.get('max', 0) for p in pools),
            }},
            'repair': {'health': {'broken': broken}},
            'active_streams': {'streams': []},
            'engine': st,
        }

    def fetch_sync_changes_page(self, page: int, limit: int, since_ts: int = 0, timeout: int = 60):
        """cli_mount sync-feed page. The engine answers everything at once,
        so page 1 carries all changes and later pages are empty."""
        if page > 1:
            return 200, {'changes': [], 'has_more': False}
        params = {'since': int(since_ts) * 1000} if since_ts > 0 else None
        code, body = self._call('GET', '/changes', timeout=timeout, params=params)
        if code != 200 or not isinstance(body, dict):
            return code, None
        changes = []
        for j in body.get('jobs') or []:
            if j.get('status') != 'ready' or not _folder(j):
                continue
            changes.append({
                'info_hash': j['id'], 'protocol': 'nzb', 'provider_id': f'nzb:{j["id"]}',
                'folder_name': _folder(j), 'original_filename': j.get('name', ''),
                'files': [{'name': e['name'], 'size': e.get('size', 0)} for e in j.get('entries') or []],
                'file_count': len(j.get('entries') or []), 'cli_debrid_ids': j.get('cli_ids') or {},
                'bad': False, 'complete': True,
            })
        return 200, {'changes': changes, 'has_more': False}


def _mark_deleted(job_id: str) -> None:
    """Share CliMountClient's "deleted moments ago" memory so job-reuse
    checks treat a job we just removed as gone without asking again."""
    try:
        from usenet.climount_client import _mark_job_deleted
        _mark_job_deleted(job_id)
    except Exception:
        pass


# Selection logic is the same as CliMountClient's; it only calls the
# _find_nzb_folder/_list_nzb_folder_files/register_cli_ids/push_tags
# methods this class implements. Imported at call time: importing
# climount_client at load time pulls in routes/ and can hit an import cycle.
def _share(name):
    def method(self, *args, **kwargs):
        from usenet.climount_client import CliMountClient
        return getattr(CliMountClient, name)(self, *args, **kwargs)
    method.__name__ = name
    method.__doc__ = f'Same as CliMountClient.{name}.'
    setattr(BuiltinClient, name, method)


for _n in ('get_nzb_file_info', 'get_nzb_folder_all_files', 'register_cli_ids_for_item', 'push_tags_for_item'):
    _share(_n)


_client_instance = None


def get_builtin_client() -> BuiltinClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = BuiltinClient()
    return _client_instance


def reset_builtin_client() -> None:
    global _client_instance
    _client_instance = None
