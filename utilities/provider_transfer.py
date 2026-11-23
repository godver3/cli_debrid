"""Move stored NZBs between usenet backends (cli_mount, NzbDAV/Zurg, built-in mount).

Powers Tools -> Usenet -> "Migrate between usenet providers". Each item's stored
.nzb goes from the source backend to the target, which fetches it from usenet
again: no re-search, categories kept where the source has them.

Sources:
  - cli_mount: reads the .nzb files from cli_mount's nzb store, bind-mounted into
    this container (cli_mount has no HTTP export). Files are named by NZB id
    (<uuid>.nzb), so the release name comes from cli_mount's library listing
    (/api/browse/__all__: info_hash == NZB id) or the NZB's <meta type="name">.
  - NzbDAV: mode=history (slots carry nzb_blob_id) + /api/download-nzb.
    Zurg's SAB emulation keeps no stored NZBs, so it can only be a target.
  - Built-in mount: GET /jobs and /jobs/{id}/nzb on the local engine.
Targets: cli_mount POST /api/add, NzbDAV/Zurg mode=addfile, built-in POST /jobs.

Best-effort: cli_mount trims old .nzb files, and the target re-downloads from
usenet (subject to retention and connection limits).
"""

import logging
import os
import re
import threading
import time
import uuid

import requests
from requests.exceptions import RequestException

# job_id -> progress dict (polled by the UI). In-memory; cleared on restart.
_jobs = {}
_jobs_lock = threading.Lock()

KINDS = ('climount', 'nzbdav', 'builtin')
SOURCE_KINDS = KINDS
TARGET_KINDS = KINDS

_META_NAME_RE = re.compile(rb'<meta\s+type="name"\s*>([^<]+)</meta>', re.I)


def _truthy(value, default=True):
    """Form posts send 'false'/'0' strings; bool('false') would be True."""
    if value is None or value == '':
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no', 'off')
    return bool(value)


def _norm(url):
    url = (url or '').strip().rstrip('/')
    if url.endswith('/api'):
        url = url[:-4].rstrip('/')
    return url


def parse_direction(direction):
    """'<source>_to_<target>' -> (source, target), or (None, None) if invalid."""
    src, sep, tgt = str(direction or '').partition('_to_')
    if not sep or src not in SOURCE_KINDS or tgt not in TARGET_KINDS or src == tgt:
        return None, None
    return src, tgt


def _builtin_client():
    from usenet.builtin_client import get_builtin_client
    return get_builtin_client()


# --- names ---------------------------------------------------------------------

def _climount_library(url, token, max_pages=2000):
    """[(name, info_hash)] from cli_mount's __all__ listing (100 per page)."""
    out = []
    if not _norm(url):
        return out
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    page = 1
    while page <= max_pages:
        try:
            r = requests.get(f'{_norm(url)}/api/browse/__all__', headers=headers,
                             params={'page': page, 'limit': 100}, timeout=20)
            if r.status_code != 200:
                break
            data = r.json() or {}
        except (RequestException, ValueError):
            break
        for e in data.get('entries') or []:
            if isinstance(e, dict) and e.get('name'):
                out.append((str(e['name']), str(e.get('info_hash') or '')))
        if page >= int(data.get('total_pages') or 0):
            break
        page += 1
    return out


def _nzb_meta_name(path):
    """<meta type="name"> from the NZB's head, if the indexer wrote one."""
    try:
        with open(path, 'rb') as fh:
            m = _META_NAME_RE.search(fh.read(16384))
        if m:
            return m.group(1).decode('utf-8', 'replace').strip()
    except OSError:
        pass
    return ''


# --- source: list items -----------------------------------------------------

def list_source_items(direction, source_url='', source_token='', source_nzb_path='', limit=100000):
    """Returns (items, error). item = {name, category, ref, status}. ref is the
    nzb_blob_id (NzbDAV), the .nzb path (cli_mount) or the job id (built-in)."""
    src, _ = parse_direction(direction)
    if src is None:
        return None, f'Unknown direction: {direction}'

    if src == 'nzbdav':
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
        if slots and not items:
            return None, ('This SAB backend lists history but keeps no stored NZBs '
                          '(Zurg does not), so it can only be a target.')
        return items, None

    if src == 'builtin':
        try:
            jobs = _builtin_client()._usenet_jobs()
        except Exception as e:
            return None, f'Built-in mount not reachable: {e}'
        items = [{'name': j.get('name') or j.get('id'), 'category': j.get('category') or '',
                  'ref': j.get('id'), 'status': j.get('status') or ''}
                 for j in jobs if j.get('id')]
        return items, None

    # cli_mount
    path = (source_nzb_path or '').strip()
    if not path:
        return None, 'No cli_mount nzb-store path.'
    if not os.path.isdir(path):
        return None, (f'Path not found in this container: {path}. Bind cli_mount\'s '
                      f'appdata into cli-debrid and point this at its usenet/nzbs folder.')
    # cli_mount names the files by NZB id: map ids to release names, or every
    # job at the target would be named after a UUID.
    names = {h: n for n, h in _climount_library(source_url, source_token) if h}
    items = []
    try:
        for fn in sorted(os.listdir(path)):
            if fn.lower().endswith('.nzb'):
                nzb_id = fn[:-4]
                full = os.path.join(path, fn)
                name = names.get(nzb_id) or _nzb_meta_name(full) or nzb_id
                items.append({'name': name, 'category': '', 'ref': full, 'status': ''})
    except OSError as e:
        return None, f'Cannot read {path}: {e}'
    return items, None


# --- read one item's .nzb bytes ---------------------------------------------

def _read_nzb(direction, item, source_url='', source_token=''):
    src, _ = parse_direction(direction)
    if src == 'nzbdav':
        url = _norm(source_url)
        params = {'nzbBlobId': item['ref']}
        headers = {}
        if source_token:
            # Without the key an auth-enabled NzbDAV answers 401 for every item.
            params['apikey'] = source_token
            headers['X-Api-Key'] = source_token
        try:
            r = requests.get(f'{url}/api/download-nzb', params=params, headers=headers, timeout=30)
            if r.status_code == 200 and r.content:
                return r.content, None
            return None, f'download-nzb HTTP {r.status_code}'
        except RequestException as e:
            return None, str(e)
    if src == 'builtin':
        try:
            client = _builtin_client()
            r = requests.get(f"{client.base_url}/jobs/{item['ref']}/nzb", headers=client._headers(), timeout=30)
            if r.status_code == 200 and r.content:
                return r.content, None
            return None, f'engine /nzb HTTP {r.status_code}'
        except Exception as e:
            return None, str(e)
    # cli_mount source: read from mounted file
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


def _submit_builtin(name, category, nzb_bytes):
    try:
        jid = _builtin_client().add_nzb_content(nzb_bytes, title=name, category=category)
    except Exception as e:
        return None, str(e)
    return (jid, None) if jid else (None, 'the engine rejected it (see the engine log)')


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
        elif target_kind == 'builtin':
            names.update(j.get('name') for j in _builtin_client()._usenet_jobs() if j.get('name'))
        else:  # climount: its library listing (the old /api/browse/nzbs was a 404)
            names.update(n for n, _ in _climount_library(url, token))
    except Exception:
        pass
    return names


def _nzbdav_queue_depth(url, token):
    try:
        r = requests.get(f'{_norm(url)}/api', params={'mode': 'queue', 'apikey': token}, timeout=10)
        if r.status_code == 200:
            q = (r.json() or {}).get('queue', {})
            return int(q.get('noofslots', q.get('noofslots_total', 0)) or 0)
    except (RequestException, ValueError, TypeError):
        pass
    return 0


# --- job runner --------------------------------------------------------------

def active_job_id():
    """The transfer still running, if any (the page resumes it after a reload)."""
    with _jobs_lock:
        for jid, j in _jobs.items():
            if not j.get('complete'):
                return jid
    return None


def start_job(params):
    """params: direction, source_url, source_token, source_nzb_path, target_url,
    target_token, target_category, skip_existing(bool), max_queue(int), limit(int).

    Returns (job_id, error). One transfer at a time: two at once submitted the
    same items twice."""
    with _jobs_lock:
        for jid, j in _jobs.items():
            if not j.get('complete'):
                return None, f'A transfer is already running ({jid}).'
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {'status': 'starting', 'total': 0, 'done': 0, 'submitted': 0,
                         'skipped': 0, 'failed': 0, 'complete': False, 'log': [], 'error': None,
                         'direction': params.get('direction')}
    t = threading.Thread(target=_run_job, args=(job_id, params), daemon=True)
    t.start()
    return job_id, None


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


def _bump(job_id, key, done):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if j is not None:
            j[key] = j.get(key, 0) + 1
            j['done'] = done


def _run_job(job_id, p):
    try:
        direction = p['direction']
        _, target_kind = parse_direction(direction)
        if target_kind is None:
            _set(job_id, status='error', error=f'Unknown direction: {direction}', complete=True)
            return
        target_url, target_token = p.get('target_url', ''), p.get('target_token', '')
        target_cat = p.get('target_category', '')
        skip_existing = _truthy(p.get('skip_existing'), True)
        max_queue = max(1, int(p.get('max_queue', 3) or 3))
        limit = int(p.get('limit', 0) or 0)

        items, err = list_source_items(direction, p.get('source_url', ''), p.get('source_token', ''),
                                        p.get('source_nzb_path', ''), limit=limit or 100000)
        if err:
            _set(job_id, status='error', error=err, complete=True)
            return
        if limit:
            items = items[:limit]

        existing = _target_existing_names(target_kind, target_url, target_token) if skip_existing else set()
        sent = set()
        _set(job_id, status='running', total=len(items))
        _log(job_id, f'{len(items)} item(s) at source; {len(existing)} already at target' if skip_existing
                     else f'{len(items)} item(s) at source')

        for i, item in enumerate(items, 1):
            with _jobs_lock:
                cancelled = bool(_jobs.get(job_id, {}).get('cancel'))
            if cancelled:
                _set(job_id, status='cancelled', complete=True)
                return
            name = item['name']
            if (skip_existing and name in existing) or name in sent:
                _bump(job_id, 'skipped', i)
                continue
            if str(item.get('status') or '').lower() == 'failed':
                # A failed source download has nothing playable to move.
                _bump(job_id, 'skipped', i)
                _log(job_id, f'SKIP failed at source: {name}')
                continue

            # throttle on target queue depth (NzbDAV/Zurg only: they have a queue endpoint)
            if target_kind == 'nzbdav':
                waited = 0
                while _nzbdav_queue_depth(target_url, target_token) >= max_queue and waited < 300:
                    time.sleep(2); waited += 2

            nzb, rerr = _read_nzb(direction, item, p.get('source_url', ''), p.get('source_token', ''))
            if not nzb:
                _bump(job_id, 'failed', i)
                _log(job_id, f'FAIL read {name}: {rerr}')
                continue

            cat = item.get('category') or target_cat
            if target_kind == 'nzbdav':
                jid, serr = _submit_nzbdav(target_url, target_token, name, cat, nzb)
            elif target_kind == 'builtin':
                jid, serr = _submit_builtin(name, cat, nzb)
            else:
                jid, serr = _submit_climount(target_url, target_token, name, cat, nzb)

            if jid:
                _bump(job_id, 'submitted', i)
                # NzbDAV history can list a name more than once (retries); don't resend it.
                sent.add(name)
            else:
                _bump(job_id, 'failed', i)
                _log(job_id, f'FAIL submit {name}: {serr}')

        with _jobs_lock:
            j = _jobs[job_id]
            j.update(status='complete', complete=True)
            done_msg = f"Done: {j['submitted']} submitted, {j['skipped']} skipped, {j['failed']} failed"
        # Outside the lock: _log takes it too, and calling it inside deadlocked
        # every finished job (and then every status poll).
        _log(job_id, done_msg)
    except Exception as e:
        logging.exception('[provider_transfer] job failed')
        _set(job_id, status='error', error=str(e), complete=True)


def cancel_job(job_id):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]['cancel'] = True
            return True
    return False
