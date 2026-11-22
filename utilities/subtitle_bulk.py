"""Background subtitle work: the page's Search/Sync buttons (per item, season,
show or the whole filter) and the Bulk subtitles task. A search that will
sync the download reads the video, so it waits while something plays.

Two lanes run side by side, each with its own queue, progress and Stop:
'search' (download missing languages: provider lookups and a tiny hash read,
so it's quick) and 'sync' (resync existing subtitles: reads samples from the
file, slow on a debrid/usenet mount). A search never waits behind syncs.
Providers paused after a quota/login error (downsub.blocked_providers) are
skipped; a search lane stops when every provider is paused.
"""
import collections
import logging
import threading
import time

KINDS = ('search', 'sync')
_LOCK = threading.Lock()
_QUEUE = {k: collections.deque() for k in KINDS}
_JOB = {k: {'running': False} for k in KINDS}
_CANCEL = {k: threading.Event() for k in KINDS}


def status():
    from utilities import downsub
    out = {}
    with _LOCK:
        for k in KINDS:
            job = dict(_JOB[k])
            job['recent'] = list(_JOB[k].get('recent') or [])
            job['queued'] = len(_QUEUE[k])
            out[k] = job
    out['paused_providers'] = downsub.blocked_providers()
    return out


def stop(kind=None):
    """Stop a lane (or both) after its current item; drop what it had queued."""
    for k in ([kind] if kind in KINDS else KINDS):
        # Together under the lock: the worker reads "cancel set, queue not
        # empty" as work queued after the stop.
        with _LOCK:
            _QUEUE[k].clear()
            _CANCEL[k].set()


def _search_one(item):
    """(saved, note, did_work)"""
    from utilities import downsub
    path = downsub.resolve_item_path(item)
    if not path:
        return False, 'no file path', True
    ok = downsub.download_subtitles_for_video(path, item=item)
    if ok:
        return True, downsub.last_result_note(item) or 'saved', True
    return False, downsub.last_result_note(item) or 'nothing passed the match threshold', True


def _sync_one(item):
    from utilities import subtitle_resync
    changed, checked, notes = subtitle_resync.resync_item(item, skip_checked=True)
    if not checked:
        return False, 'nothing new to check', False
    return changed > 0, '; '.join(notes), True


def _enqueue(items, kind, label):
    """Add work to a lane; (True if its worker must be started, number added)."""
    with _LOCK:
        have = {e.get('id') for e in _QUEUE[kind]}
        cur = _JOB[kind].get('current_id')
        new = [it for it in items if it.get('id') not in have and it.get('id') != cur]
        _QUEUE[kind].extend(new)
        if _JOB[kind].get('running'):
            _JOB[kind]['total'] = _JOB[kind].get('total', 0) + len(new)
            return False, len(new)
        _CANCEL[kind].clear()
        _JOB[kind] = {'running': True, 'label': label, 'kind': kind, 'total': len(new), 'done': 0,
                      'found': 0, 'current': None, 'stopped': None, 'started_at': time.time(), 'recent': []}
        return True, len(new)


PLAYBACK_CHECK_EVERY = 15.0   # seconds a "something is playing" answer is reused
PLAYBACK_WAIT_STEP = 30.0     # how long a lane sleeps between checks while one plays
_playing_cache = {'at': 0.0, 'n': 0}


def _streaming(s):
    """A session pulling video now: movies/episodes that play or buffer.
    A paused player left open (overnight) or music doesn't hold the lanes."""
    state = ((s.get('Player') or {}).get('state') or 'playing').lower()
    kind = (s.get('type') or '').lower()
    return state != 'paused' and kind not in ('track', 'audio', 'photo')


def playing_sessions():
    """Video sessions playing on Plex/Jellyfin/Emby now (0 when no server is
    set or it doesn't answer: never blocks work on a broken server)."""
    now = time.monotonic()
    if now - _playing_cache['at'] < PLAYBACK_CHECK_EVERY:
        return _playing_cache['n']
    try:
        from utilities.settings import get_setting
        from utilities.media_sessions import sessions
        n = sum(1 for s in sessions(get_setting) if _streaming(s))
    except Exception as e:
        logging.debug(f"[subs] playback check failed: {e}")
        n = 0
    _playing_cache.update(at=now, n=n)
    return n


def _reads_files(kind):
    """The lane reads samples of the video over the mount: only a search
    with sync-after-download on. Otherwise both lanes read just the file's
    two 64 KB hash blocks (Fix existing never checks timing; that is the
    per-subtitle Sync button)."""
    if kind != 'search':
        return False
    from utilities import downsub
    return getattr(downsub.config, 'SYNC_SUBTITLES', 'off') in ('unless_hash', 'always')


def _hold_for_playback(kind, job, wait):
    """While something plays, a lane that reads files waits (page runs) or
    stops, leaving the rest for the next run (the scheduled task, which
    must not hold its thread for a whole movie). False = stop the lane."""
    if not _reads_files(kind):
        return True
    logged = False
    while (n := playing_sessions()) > 0:
        if not wait:
            with _LOCK:
                _QUEUE[kind].clear()
                job['stopped'] = f'{n} stream(s) playing: the rest waits for the next run'
                job['running'] = False
            return False
        if not logged:
            logging.info(f"[subs {kind}] {n} stream(s) playing: waiting so subtitle work doesn't take the line")
            logged = True
        with _LOCK:
            job['waiting'] = f'waiting: {n} stream(s) playing'
        if _CANCEL[kind].wait(PLAYBACK_WAIT_STEP):
            break
    with _LOCK:
        job.pop('waiting', None)
    return True


def _worker(kind, pause, wait_for_playback=True):
    from utilities import downsub
    from utilities.subtitle_status import label as _label
    downsub.config.reload()
    t0 = time.time()
    job = _JOB[kind]
    logging.info(f"[subs {kind}] {job.get('label')}: starting, {job.get('total')} item(s)")
    try:
        while True:
            if _CANCEL[kind].is_set():
                with _LOCK:
                    if _QUEUE[kind]:
                        # stop() empties the queue, so this was queued after
                        # the stop (it saw the lane still running): do it.
                        _CANCEL[kind].clear()
                    else:
                        job['stopped'] = 'stopped by user'
                        job['running'] = False
                        break
            if not _hold_for_playback(kind, job, wait_for_playback):
                break
            if _CANCEL[kind].is_set():
                continue
            with _LOCK:
                if not _QUEUE[kind]:
                    # Ends under the same lock _enqueue checks 'running'
                    # with, so work added now starts a new worker instead
                    # of waiting in the queue for one that is leaving.
                    job['running'] = False
                    break
                item = _QUEUE[kind].popleft()
            if kind == 'search' and not downsub.selected_providers():
                with _LOCK:
                    _QUEUE[kind].clear()
                    job['stopped'] = 'every provider is paused: ' + '; '.join(
                        f'{k}: {v}' for k, v in downsub.blocked_providers().items())
                    job['running'] = False
                break
            name = _label(item)
            with _LOCK:
                job['current'] = name
                job['current_id'] = item.get('id')
            ok, note, worked = False, '', True
            try:
                ok, note, worked = (_sync_one if kind == 'sync' else _search_one)(item)
            except Exception as e:
                logging.warning(f"[subs {kind}] {name}: {e}")
                note = str(e)
            with _LOCK:
                job['done'] = job.get('done', 0) + 1
                job['found'] += 1 if ok else 0
                n, total = job['done'], job['total']
                if worked:
                    job['recent'] = ([{'label': name, 'ok': ok, 'id': item.get('id'), 'note': note, 'kind': kind}]
                                     + (job.get('recent') or []))[:40]
            if worked:
                logging.info(f"[subs {kind}] {n}/{total} {name}: {note}")
                if pause:
                    time.sleep(pause)
    finally:
        with _LOCK:
            job['running'] = False
            job['current'] = None
            job['current_id'] = None
            job['finished_at'] = time.time()
            done, found, stopped = job.get('done'), job.get('found'), job.get('stopped')
        verb = 'fixed' if kind == 'sync' else 'saved'
        logging.info(f"[subs {kind}] done: {found} {verb} out of {done} in {time.time() - t0:.0f}s"
                     f"{' (' + stopped + ')' if stopped else ''}")


def start(items, label='manual', pause=1.0, kind='search'):
    """Queue work on a lane and make sure its worker runs.
    Returns ('started'|'queued', number added)."""
    kind = kind if kind in KINDS else 'search'
    first, n = _enqueue(items, kind, label)
    if first:
        threading.Thread(target=_worker, args=(kind, pause), daemon=True, name=f'subs-{kind}').start()
        return 'started', n
    return 'queued', n


def run_now(items, label='task', pause=1.0, kind='search'):
    """Scheduled task: run in the calling thread, or queue behind the lane's
    current run. Stops (rest next run) instead of waiting while a stream plays."""
    first, n = _enqueue(items, kind, label)
    if first:
        _worker(kind, pause, wait_for_playback=False)
        return True
    logging.info(f"[subs {kind}] {label}: queued {n} item(s) behind the run already going")
    return False
