"""Migrate downloads between usenet providers (cli_mount <-> NzbDAV) over HTTP.

Powers the "Migrate between usenet providers" tool in Debug -> Library. It moves
the stored .nzb of each item from a source provider to a target provider, which
re-fetches it from usenet — no re-search, categories preserved where the source
exposes them.

Why HTTP / a mounted path (not docker): cli-debrid runs in a container with no
Docker socket, so it can't `docker cp` a provider's .nzb store. Instead:
  - NzbDAV source: list `mode=history` (each slot carries `nzb_blob_id`), fetch
    the .nzb via `GET /api/download-nzb?nzbBlobId=`.
  - cli_mount source: cli_mount has no HTTP .nzb export, so read the .nzb files
    straight from cli_mount's nzb store, which the user bind-mounts into
    cli-debrid (same data_path bind used by the cli_mount DB tools).
Submitting is always HTTP: NzbDAV `mode=addfile`, cli_mount `POST /api/add`.

This is best-effort: cli_mount trims old .nzb files, so part of an old library
may not be transferable; and the target re-downloads from usenet (subject to
your provider's retention/connection limits).
"""

import logging
import os
import threading
import time
import uuid

import requests
from requests.exceptions import RequestException

# job_id -> progress dict (polled by the UI). In-memory; cleared on restart.
_jobs = {}
_jobs_lock = threading.Lock()

NZBDAV_REQUIRED_CATEGORIES = ['movies', 'shows', 'movies_1080p_264', 'shows_1080p_264', '__unplayable__', 'music']


def _norm(url):
    url = (url or '').strip().rstrip('/')
    if url.endswith('/api'):
        url = url[:-4].rstrip('/')
    return url


# --- source: list items -----------------------------------------------------

def list_source_items(direction, source_url='', source_token='', source_nzb_path='', limit=100000):
    """Returns (items, error). item = {name, category, ref}. ref is the
    nzb_blob_id (nzbdav source) or the absolute .nzb path (climount source)."""
    if direction == 'nzbdav_to_climount':
        url = _norm(source_url)
        if not url:
            return None, 'No NzbDAV source URL.'
        try:
            r = requests.get(f'{url}/api', params={'mode': 'history', 'apikey': source_token, 'limit': limit}, timeout=20)
            if r.status_code != 200:
                return None, f'NzbDAV history HTTP {r.status_code}'
            slots = (r.json() or {}).get('history', {}).get('slots', [])
        except (RequestException, ValueError) as e:
            return None, f'NzbDAV history error: {e}'
        items = []
        for s in slots:
            blob = s.get('nzb_blob_id')
            if not blob:
                continue  # no stored .nzb to move
            items.append({'name': s.get('name') or s.get('nzb_name') or blob,
                          'category': s.get('category') or '',
                          'ref': blob,
                          'status': s.get('status') or ''})
        return items, None

    if direction == 'climount_to_nzbdav':
        path = (source_nzb_path or '').strip()
        if not path:
            return None, 'No cli_mount nzb-store path.'
        if not os.path.isdir(path):
            return None, (f'Path not found in this container: {path}. Bind cli_mount\'s '
                          f'appdata into cli-debrid and point this at its usenet/nzbs folder.')
        items = []
        try:
            for fn in sorted(os.listdir(path)):
                if fn.lower().endswith('.nzb'):
                    items.append({'name': fn[:-4], 'category': '', 'ref': os.path.join(path, fn), 'status': ''})
        except OSError as e:
            return None, f'Cannot read {path}: {e}'
        return items, None

    return None, f'Unknown direction: {direction}'


# --- read one item's .nzb bytes ---------------------------------------------

def _read_nzb(direction, item, source_url='', source_token=''):
    if direction == 'nzbdav_to_climount':
        url = _norm(source_url)
        try:
            r = requests.get(f'{url}/api/download-nzb', params={'nzbBlobId': item['ref']}, timeout=30)
            if r.status_code == 200 and r.content:
                return r.content, None
            return None, f'download-nzb HTTP {r.status_code}'
        except RequestException as e:
            return None, str(e)
    # climount source: read from mounted file
    try:
        with open(item['ref'], 'rb') as fh:
            return fh.read(), None
    except OSError as e:
        return None, str(e)


# --- submit to target --------------------------------------------------------

def _submit_nzbdav(url, token, name, category, nzb_bytes):
    params = {'mode': 'addfile', 'nzbname': name}
    if token:
        params['apikey'] = token
    if category:
        params['cat'] = category
    r = requests.post(f'{_norm(url)}/api', params=params,
                      files={'name': (f'{name}.nzb', nzb_bytes, 'application/x-nzb')}, timeout=40)
    if r.status_code == 200:
        d = r.json() or {}
        if d.get('status') is True and d.get('nzo_ids'):
            return str(d['nzo_ids'][0]), None
        return None, str(d.get('error') or 'rejected')
    return None, f'HTTP {r.status_code}: {r.text[:200]}'


def _submit_climount(url, token, name, category, nzb_bytes):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    fields = {'arr': (None, 'cli_debrid'), 'nzbFiles': (f'{name}.nzb', nzb_bytes, 'application/x-nzb')}
    if category:
        fields['downloadFolder'] = (None, category)
    r = requests.post(f'{_norm(url)}/api/add', headers=headers, files=fields, timeout=40)
    if r.status_code == 200:
        res = r.json()
        if isinstance(res, list) and res:
            job = res[0]
            if job.get('status') == 'error':
                return None, str(job.get('error') or 'rejected')
            return str(job.get('id') or job.get('nzo_id') or job.get('hash') or 'submitted'), None
        return 'submitted', None
    return None, f'HTTP {r.status_code}: {r.text[:200]}'


# --- target helpers: existing names + queue depth (throttle) -----------------

def _target_existing_names(target_kind, url, token):
    """Best-effort set of names already present at the target (to skip dupes)."""
    names = set()
    try:
        if target_kind == 'nzbdav':
            r = requests.get(f'{_norm(url)}/api', params={'mode': 'history', 'apikey': token, 'limit': 100000}, timeout=20)
            if r.status_code == 200:
                for s in (r.json() or {}).get('history', {}).get('slots', []):
                    n = s.get('name') or s.get('nzb_name')
                    if n:
                        names.add(n)
        else:  # climount
            r = requests.get(f'{_norm(url)}/api/browse/nzbs',
                             headers={'Authorization': f'Bearer {token}'} if token else {},
                             params={'limit': 100000}, timeout=20)
            if r.status_code == 200:
                data = r.json()
                entries = data if isinstance(data, list) else data.get('entries', data.get('items', []))
                for e in entries or []:
                    n = e.get('name') if isinstance(e, dict) else e
                    if n:
                        names.add(str(n))
    except (RequestException, ValueError, AttributeError):
        pass
    return names


def _nzbdav_queue_depth(url, token):
    try:
        r = requests.get(f'{_norm(url)}/api', params={'mode': 'queue', 'apikey': token}, timeout=10)
        if r.status_code == 200:
            return int((r.json() or {}).get('queue', {}).get('noofslots', 0))
    except (RequestException, ValueError, TypeError):
        pass
    return 0


# --- job runner --------------------------------------------------------------

def start_job(params):
    """params: direction, source_url, source_token, source_nzb_path, target_url,
    target_token, target_category, skip_existing(bool), max_queue(int), limit(int)."""
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {'status': 'starting', 'total': 0, 'done': 0, 'submitted': 0,
                         'skipped': 0, 'failed': 0, 'complete': False, 'log': [], 'error': None}
    t = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    t.start()
    return job_id


def get_status(job_id):
    with _jobs_lock:
        return dict(_jobs.get(job_id) or {})


def _log(job_id, msg):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is not None:
            j['log'].append(msg)
            j['log'] = j['log'][-200:]


def _set(job_id, **kw):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is not None:
            j.update(kw)


def _run_job(job_id, p):
    try:
        direction = p['direction']
        target_kind = 'nzbdav' if direction == 'climount_to_nzbdav' else 'climount'
        target_url, target_token = p.get('target_url', ''), p.get('target_token', '')
        target_cat = p.get('target_category', '')
        skip_existing = bool(p.get('skip_existing', True))
        max_queue = int(p.get('max_queue', 3) or 3)
        limit = int(p.get('limit', 0) or 0)

        items, err = list_source_items(direction, p.get('source_url', ''), p.get('source_token', ''),
                                        p.get('source_nzb_path', ''), limit=limit or 100000)
        if err:
            _set(job_id, status='error', error=err, complete=True)
            return
        if limit:
            items = items[:limit]

        existing = _target_existing_names(target_kind, target_url, target_token) if skip_existing else set()
        _set(job_id, status='running', total=len(items))
        _log(job_id, f'{len(items)} item(s) at source; {len(existing)} already at target' if skip_existing
                     else f'{len(items)} item(s) at source')

        for i, item in enumerate(items, 1):
            with _jobs_lock:
                if _jobs.get(job_id, {}).get('cancel'):
                    _set(job_id, status='cancelled', complete=True)
                    return
            name = item['name']
            if skip_existing and name in existing:
                _set(job_id, skipped=_jobs[job_id]['skipped'] + 1, done=i)
                continue

            # throttle on target queue depth (nzbdav only — has a queue endpoint)
            if target_kind == 'nzbdav':
                waited = 0
                while _nzbdav_queue_depth(target_url, target_token) >= max_queue and waited < 300:
                    time.sleep(2); waited += 2

            nzb, rerr = _read_nzb(direction, item, p.get('source_url', ''), p.get('source_token', ''))
            if not nzb:
                _set(job_id, failed=_jobs[job_id]['failed'] + 1, done=i)
                _log(job_id, f'FAIL read {name}: {rerr}')
                continue

            cat = item.get('category') or target_cat
            if target_kind == 'nzbdav':
                jid, serr = _submit_nzbdav(target_url, target_token, name, cat, nzb)
            else:
                jid, serr = _submit_climount(target_url, target_token, name, cat, nzb)

            if jid:
                _set(job_id, submitted=_jobs[job_id]['submitted'] + 1, done=i)
            else:
                _set(job_id, failed=_jobs[job_id]['failed'] + 1, done=i)
                _log(job_id, f'FAIL submit {name}: {serr}')

        with _jobs_lock:
            j = _jobs[job_id]
            j.update(status='complete', complete=True)
            _log(job_id, f"Done: {j['submitted']} submitted, {j['skipped']} skipped, {j['failed']} failed")
    except Exception as e:
        logging.exception('[provider_transfer] job failed')
        _set(job_id, status='error', error=str(e), complete=True)


def cancel_job(job_id):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]['cancel'] = True
            return True
    return False
