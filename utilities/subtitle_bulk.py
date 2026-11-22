"""Search subtitles for many items: the "Search all missing" button and the
Bulk subtitles task. One run at a time; progress is readable while it runs.

Providers paused after a quota/login error (downsub.blocked_providers) are
skipped for the rest of the run; when every provider is paused the run stops.
"""
import logging
import threading
import time

_LOCK = threading.Lock()
_JOB = {'running': False}
_CANCEL = threading.Event()


def status():
    from utilities import downsub
    with _LOCK:
        out = dict(_JOB)
        out['recent'] = list(_JOB.get('recent') or [])
    out['paused_providers'] = downsub.blocked_providers()
    return out


def stop():
    _CANCEL.set()


def _run(items, label, pause, kind):
    work = _search_one if kind == 'search' else _sync_one
    from utilities import downsub
    downsub.config.reload()
    try:
        for i, item in enumerate(items):
            if _CANCEL.is_set():
                _set(stopped='stopped by user')
                break
            if kind == 'search' and not downsub.selected_providers():
                _set(stopped='every provider is paused: ' + '; '.join(
                    f'{k}: {v}' for k, v in downsub.blocked_providers().items()))
                break
            from utilities.subtitle_status import label as _label
            name = _label(item)
            _set(current=name)
            ok, note, worked = False, '', True
            try:
                ok, note, worked = work(item)
            except Exception as e:
                logging.warning(f"[subs bulk] {name}: {e}")
                note = str(e)
            with _LOCK:
                _JOB['done'] = i + 1
                _JOB['found'] += 1 if ok else 0
                if worked:
                    _JOB['recent'] = ([{'label': name, 'ok': ok, 'id': item.get('id'), 'note': note}]
                                      + (_JOB.get('recent') or []))[:30]
            if pause and worked:
                time.sleep(pause)
    finally:
        with _LOCK:
            _JOB['running'] = False
            _JOB['current'] = None
            _JOB['finished_at'] = time.time()
        logging.info(f"[subs bulk] {label}: {_JOB.get('found')}/{_JOB.get('done')} saved"
                     f"{' (' + _JOB['stopped'] + ')' if _JOB.get('stopped') else ''}")


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


def _set(**kw):
    with _LOCK:
        _JOB.update(kw)


def _claim(items, label, kind):
    with _LOCK:
        if _JOB.get('running'):
            return False
        _CANCEL.clear()
        _JOB.clear()
        _JOB.update({'running': True, 'label': label, 'kind': kind, 'total': len(items), 'done': 0, 'found': 0,
                     'current': None, 'stopped': None, 'started_at': time.time(), 'recent': []})
        return True


def start(items, label='manual', pause=1.0, kind='search'):
    """Run in a background thread. kind: 'search' (download missing) or
    'sync' (resync existing). False if a run is already going."""
    if not _claim(items, label, kind):
        return False
    threading.Thread(target=_run, args=(items, label, pause, kind), daemon=True, name='subs-bulk').start()
    return True


def run_now(items, label='task', pause=1.0, kind='search'):
    """Run in the calling thread (scheduled task). False if a run is going."""
    if not _claim(items, label, kind):
        logging.info("[subs bulk] a subtitle run is already going; skipping this one")
        return False
    _run(items, label, pause, kind)
    return True
