"""Background subtitle work queue: the page's Search/Sync buttons (per item,
season, show or the whole filter) and the Bulk subtitles task.

One worker handles a queue of (item, kind) entries, kind 'search' (download
missing languages) or 'sync' (resync existing subtitles). Adding work while it
runs appends to the queue. Providers paused after a quota/login error
(downsub.blocked_providers) are skipped; searches stop when all are paused.
"""
import collections
import logging
import threading
import time

_LOCK = threading.Lock()
_QUEUE = collections.deque()
_JOB = {'running': False}
_CANCEL = threading.Event()


def status():
    from utilities import downsub
    with _LOCK:
        out = dict(_JOB)
        out['recent'] = list(_JOB.get('recent') or [])
        out['queued'] = len(_QUEUE)
    out['paused_providers'] = downsub.blocked_providers()
    return out


def stop():
    """Stop after the current item and drop everything queued."""
    _CANCEL.set()
    with _LOCK:
        _QUEUE.clear()


def _search_one(item):
    """(saved, note, did_work)"""
    from utilities import downsub
    path = downsub.resolve_item_path(item)
    if not path:
        return False, 'no file path', True
    ok = downsub.download_subtitles_for_video(path, item=item)
    return bool(ok), 'saved' if ok else 'nothing passed the match threshold', True


def _sync_one(item):
    from utilities import subtitle_resync
    changed, checked, notes = subtitle_resync.resync_item(item, skip_checked=True)
    if not checked:
        return False, 'nothing new to check', False
    return changed > 0, '; '.join(notes), True


def _enqueue(items, kind, label):
    """Add work; (True if a new worker must be started, number added)."""
    with _LOCK:
        have = {(e[0].get('id'), e[1]) for e in _QUEUE}
        new = [(it, kind) for it in items if (it.get('id'), kind) not in have]
        _QUEUE.extend(new)
        if _JOB.get('running'):
            _JOB['total'] = _JOB.get('total', 0) + len(new)
            return False, len(new)
        _CANCEL.clear()
        _JOB.clear()
        _JOB.update({'running': True, 'label': label, 'kind': kind, 'total': len(new), 'done': 0,
                     'found': 0, 'current': None, 'stopped': None, 'started_at': time.time(), 'recent': []})
        return True, len(new)


def _worker(pause):
    from utilities import downsub
    from utilities.subtitle_status import label as _label
    downsub.config.reload()
    t0 = time.time()
    logging.info(f"[subs bulk] {_JOB.get('label')}: starting, {_JOB.get('total')} item(s)")
    try:
        while True:
            if _CANCEL.is_set():
                with _LOCK:
                    _JOB['stopped'] = 'stopped by user'
                break
            with _LOCK:
                if not _QUEUE:
                    break
                item, kind = _QUEUE.popleft()
                _JOB['kind'] = kind
            if kind == 'search' and not downsub.selected_providers():
                with _LOCK:
                    _QUEUE.clear()
                    _JOB['stopped'] = 'every provider is paused: ' + '; '.join(
                        f'{k}: {v}' for k, v in downsub.blocked_providers().items())
                break
            name = _label(item)
            with _LOCK:
                _JOB['current'] = f"{'Syncing' if kind == 'sync' else 'Searching'} {name}"
            ok, note, worked = False, '', True
            try:
                ok, note, worked = (_sync_one if kind == 'sync' else _search_one)(item)
            except Exception as e:
                logging.warning(f"[subs bulk] {name}: {e}")
                note = str(e)
            with _LOCK:
                _JOB['done'] = _JOB.get('done', 0) + 1
                _JOB['found'] += 1 if ok else 0
                n, total = _JOB['done'], _JOB['total']
                if worked:
                    _JOB['recent'] = ([{'label': name, 'ok': ok, 'id': item.get('id'), 'note': note, 'kind': kind}]
                                      + (_JOB.get('recent') or []))[:30]
            if worked:
                logging.info(f"[subs bulk] {n}/{total} {'sync' if kind == 'sync' else 'search'} {name}: {note}")
                if pause:
                    time.sleep(pause)
    finally:
        with _LOCK:
            _JOB['running'] = False
            _JOB['current'] = None
            _JOB['finished_at'] = time.time()
            done, found, stopped = _JOB.get('done'), _JOB.get('found'), _JOB.get('stopped')
        logging.info(f"[subs bulk] done: {found} saved/fixed out of {done} in {time.time() - t0:.0f}s"
                     f"{' (' + stopped + ')' if stopped else ''}")


def start(items, label='manual', pause=1.0, kind='search'):
    """Queue work and make sure the worker runs. Returns ('started'|'queued', n added)."""
    first, n = _enqueue(items, kind, label)
    if first:
        threading.Thread(target=_worker, args=(pause,), daemon=True, name='subs-bulk').start()
        return 'started', n
    return 'queued', n


def run_now(items, label='task', pause=1.0, kind='search'):
    """Scheduled task: run in the calling thread, or queue behind a run
    already going."""
    first, n = _enqueue(items, kind, label)
    if first:
        _worker(pause)
        return True
    logging.info(f"[subs bulk] {label}: queued {n} item(s) behind the run already going")
    return False
