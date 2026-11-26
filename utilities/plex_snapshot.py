"""
Snapshot of Plex's own episode metadata, written out as NFO files.

On 2026-12-03 Plex stops using TheTVDB: from then on it can only fill TV
episodes in from TMDB. What Plex shows today (TheTVDB titles, descriptions
and air dates, plus anything edited by hand) is gone after the next refresh.
take() reads every episode of the TV libraries that hold the symlinked
folder, matches each one to its library file, and keeps it:

  * in db_content/plex_metadata_snapshot.db (every snapshot is kept, dated)
  * as NFO files next to the episodes (SNAPSHOT_MARKER), so Plex's NFO agent,
    Jellyfin, Emby or Kodi can read them back later

Taking one changes nothing in Plex and needs no NFO mode or agent switch.
Snapshot NFOs never replace an NFO file Clyde's writer made or one made by
someone else, while the writer (Tools -> NFO Files modes) may replace them;
when the writer drops a show's files, or NFO files are turned off, the
snapshot's come back. An upgraded episode keeps its snapshot NFO (only when
the new file has the same season/episode).
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from utilities.settings import get_setting, set_setting

_LOG = '[PlexSnapshot]'
PLEX_DROPS_TVDB = date(2026, 12, 3)
_SXXEXX = re.compile(r'(?<![a-z0-9])s(\d{1,4})[ ._-]?e(\d{1,4})', re.I)


# ─── store ───────────────────────────────────────────────────────────────────

def _db_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'plex_metadata_snapshot.db')


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, taken_at TEXT, server TEXT,
            files INTEGER, episodes INTEGER, shows INTEGER, unmatched INTEGER, ambiguous INTEGER);
        CREATE TABLE IF NOT EXISTS files (
            snapshot_id INTEGER, path TEXT, imdb_id TEXT, numbers TEXT, data TEXT,
            PRIMARY KEY (snapshot_id, path));
        CREATE INDEX IF NOT EXISTS files_show ON files (snapshot_id, imdb_id);
        CREATE TABLE IF NOT EXISTS items (
            snapshot_id INTEGER, media_item_id INTEGER, path TEXT, season INTEGER, episode INTEGER,
            PRIMARY KEY (snapshot_id, media_item_id));
        CREATE TABLE IF NOT EXISTS shows (
            snapshot_id INTEGER, imdb_id TEXT, data TEXT, PRIMARY KEY (snapshot_id, imdb_id));
    """)
    return conn


def in_use() -> Optional[int]:
    """The snapshot whose NFO files are in the library (NFO.snapshot_id)."""
    try:
        return int(get_setting('NFO', 'snapshot_id', '') or 0) or None
    except (TypeError, ValueError):
        return None


def snapshots() -> List[Dict]:
    if not os.path.exists(_db_path()):
        return []
    conn = _conn()
    try:
        cols = ('id', 'taken_at', 'server', 'files', 'episodes', 'shows', 'unmatched', 'ambiguous')
        return [dict(zip(cols, r)) for r in
                conn.execute(f"SELECT {', '.join(cols)} FROM snapshots ORDER BY id DESC").fetchall()]
    finally:
        conn.close()


# ─── Plex ────────────────────────────────────────────────────────────────────

def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _guids(el) -> Dict[str, str]:
    out = {}
    for g in el.findall('Guid'):
        kind, _, val = (g.get('id') or '').partition('://')
        if kind in ('tvdb', 'tmdb', 'imdb') and val:
            out[kind] = val
    return out


def _section_items(plex, key, libtype: int, page: int = 500) -> Iterable:
    from utilities.plex_nfo_agent import _q
    start = 0
    while True:
        root = _q(plex, f'/library/sections/{key}/all', 'get',
                  [('type', str(libtype)), ('includeGuids', '1'),
                   ('X-Plex-Container-Start', str(start)), ('X-Plex-Container-Size', str(page))])
        items = [el for el in root if el.tag in ('Video', 'Directory')]
        yield from items
        start += len(items)
        total = _int(root.get('totalSize')) or 0
        if len(items) < page or (total and start >= total):
            return


def _episode(el) -> Dict:
    return {
        'rating_key': el.get('ratingKey'),
        'season': _int(el.get('parentIndex')),
        'episode': _int(el.get('index')),
        'title': el.get('title'),
        'summary': el.get('summary'),
        'aired': el.get('originallyAvailableAt'),
        'content_rating': el.get('contentRating'),
        'duration': _int(el.get('duration')),
        'show_key': el.get('grandparentRatingKey'),
        'show_title': el.get('grandparentTitle'),
        'guids': _guids(el),
        'directors': [t.get('tag') for t in el.findall('Director') if t.get('tag')],
        'writers': [t.get('tag') for t in el.findall('Writer') if t.get('tag')],
    }


def _parts(el) -> List[str]:
    return [p.get('file') for m in el.findall('Media') for p in m.findall('Part') if p.get('file')]


def _split(path: str) -> List[str]:
    return [x for x in path.replace('\\', '/').split('/') if x]


def _suffix_len(a: List[str], b: List[str]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def match_path(plex_file: str, by_name: Dict[str, List[str]]) -> Tuple[Optional[str], bool]:
    """The library file Plex's `plex_file` is (Plex may see the folder under
    another path): same file name, then the longest run of equal folders
    from the end. (path, ambiguous)."""
    parts = _split(plex_file)
    if not parts:
        return None, False
    cands = by_name.get(parts[-1]) or []
    if len(cands) == 1:
        return cands[0], False
    scored = sorted(((_suffix_len(parts, _split(c)), c) for c in cands), reverse=True)
    if not scored:
        return None, False
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None, True
    return scored[0][1], False


# ─── job ─────────────────────────────────────────────────────────────────────

_job_lock = threading.Lock()
_job: Dict = {'running': False}


def job_status() -> Dict:
    return dict(_job)


def status() -> Dict:
    return {'snapshots': snapshots(), 'in_use': in_use(), 'job': job_status(),
            'after_switch': date.today() >= PLEX_DROPS_TVDB}


def _start(kind: str, target) -> bool:
    if not _job_lock.acquire(blocking=False):
        return False
    _job.clear()
    _job.update({'running': True, 'kind': kind, 'started': time.time(), 'step': '', 'error': None})

    def run():
        try:
            target()
        except Exception as e:
            logging.error(f"{_LOG} {kind} failed: {e}", exc_info=True)
            _job['error'] = str(e)
        finally:
            _job.update(running=False, finished=time.time(), step='')
            _job_lock.release()

    threading.Thread(target=run, name=f'plex-snapshot-{kind}', daemon=True).start()
    return True


def take(write: bool = True) -> bool:
    """Snapshot Plex in the background, then (write) put its NFO files in the
    library. False when a run is already going."""
    def run():
        sid = _take()
        if write:
            _job['written'] = _write_all(sid)
            set_setting('NFO', 'snapshot_id', str(sid))
    return _start('take', run)


def write_nfos(snapshot_id: int) -> bool:
    """Write (or rewrite) a snapshot's NFO files, in the background."""
    def run():
        _job['written'] = _write_all(snapshot_id)
        set_setting('NFO', 'snapshot_id', str(snapshot_id))
    return _start('write', run)


def remove_nfos() -> bool:
    """Delete every snapshot NFO, in the background (the snapshots stay)."""
    def run():
        _job['removed'] = _remove_all()
        set_setting('NFO', 'snapshot_id', '')
    return _start('remove', run)


# ─── taking ──────────────────────────────────────────────────────────────────

def _take() -> int:
    from utilities import nfo_writer, plex_nfo_agent
    if not nfo_writer.symlinked_mode():
        raise RuntimeError('needs Symlinked/Local file management (the NFO files go next to the symlinks)')
    plex = plex_nfo_agent._server()
    if plex is None:
        raise RuntimeError('Plex is not configured (File Management → Plex URL/token for symlinks)')
    sections = plex_nfo_agent.tv_sections(plex)
    if not sections:
        raise RuntimeError('no Plex TV library reads the symlinked folder '
                           f"({nfo_writer.library_root()})")

    rows = [r for r in nfo_writer._episode_rows() if r.get('location_on_disk')]
    by_name: Dict[str, List[str]] = {}
    rows_of: Dict[str, List[Dict]] = {}
    for r in rows:
        path = r['location_on_disk']
        if path not in rows_of:
            by_name.setdefault(os.path.basename(path.replace('\\', '/')), []).append(path)
        rows_of.setdefault(path, []).append(r)

    files: Dict[str, Dict[str, Dict]] = {}     # library path -> rating key -> episode
    shows: Dict[str, Dict] = {}                # Plex show key -> show
    seen = unmatched = ambiguous = 0
    for s in sections:
        _job['step'] = f"reading shows of '{s.title}'"
        for el in _section_items(plex, s.key, 2):
            shows[el.get('ratingKey')] = {'title': el.get('title'), 'year': _int(el.get('year')),
                                          'summary': el.get('summary'), 'guids': _guids(el)}
        _job['step'] = f"reading episodes of '{s.title}'"
        for el in _section_items(plex, s.key, 4):
            seen += 1
            ep = None
            for f in _parts(el):
                path, amb = match_path(f, by_name)
                ambiguous += amb
                if path:
                    ep = ep or _episode(el)
                    files.setdefault(path, {})[ep['rating_key']] = ep
            if ep is None:
                unmatched += 1
            if seen % 500 == 0:
                _job['step'] = f"reading episodes of '{s.title}' ({seen})"

    conn = _conn()
    try:
        cur = conn.execute("INSERT INTO snapshots (taken_at, server, files, episodes, shows, unmatched, ambiguous) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (time.strftime('%Y-%m-%d %H:%M:%S'), getattr(plex, 'friendlyName', '') or '',
                            len(files), seen - unmatched, 0, unmatched, ambiguous))
        sid = cur.lastrowid
        show_rows = {}
        for path, eps in files.items():
            file_rows = rows_of[path]
            imdb_id = file_rows[0].get('imdb_id')
            numbers = sorted({(r['season_number'], r['episode_number']) for r in file_rows
                              if r.get('season_number') is not None and r.get('episode_number') is not None})
            conn.execute("INSERT INTO files (snapshot_id, path, imdb_id, numbers, data) VALUES (?, ?, ?, ?, ?)",
                         (sid, path, imdb_id, json.dumps(numbers), json.dumps(list(eps.values()))))
            for r in file_rows:
                conn.execute("INSERT OR REPLACE INTO items (snapshot_id, media_item_id, path, season, episode) "
                             "VALUES (?, ?, ?, ?, ?)", (sid, r['id'], path, r['season_number'], r['episode_number']))
            show_key = next(iter(eps.values())).get('show_key')
            if imdb_id and imdb_id not in show_rows and show_key in shows:
                show_rows[imdb_id] = dict(shows[show_key], tmdb_id=file_rows[0].get('tmdb_id'))
        for imdb_id, data in show_rows.items():
            conn.execute("INSERT INTO shows (snapshot_id, imdb_id, data) VALUES (?, ?, ?)",
                         (sid, imdb_id, json.dumps(data)))
        conn.execute("UPDATE snapshots SET shows = ? WHERE id = ?", (len(show_rows), sid))
        conn.commit()
    finally:
        conn.close()
    _job['snapshot'] = {'id': sid, 'files': len(files), 'episodes': seen - unmatched,
                        'shows': len(show_rows), 'unmatched': unmatched, 'ambiguous': ambiguous}
    logging.info(f"{_LOG} snapshot {sid}: {len(files)} files, {seen - unmatched} Plex episodes, "
                 f"{len(show_rows)} shows ({unmatched} Plex episodes without a Clyde file, {ambiguous} ambiguous)")
    return sid


# ─── NFO files ───────────────────────────────────────────────────────────────

def _episodes_for_file(numbers: List, eps: List[Dict]) -> List[Tuple[Dict, Dict]]:
    """(episode for nfo_writer.build_episode_nfo, plan) per Plex episode of a
    file, numbered like the file (Clyde's numbers) when they pair up."""
    eps = sorted(eps, key=lambda e: (e.get('season') or 0, e.get('episode') or 0))
    nums = [tuple(n) for n in numbers] if len(numbers) == len(eps) else \
        [(e.get('season'), e.get('episode')) for e in eps]
    out = []
    for (s, n), e in zip(nums, eps):
        g = e.get('guids') or {}
        out.append(({'season': s, 'episode': n, 'title': e.get('title'), 'overview': e.get('summary'),
                     'aired': e.get('aired'),
                     'runtime': round(e['duration'] / 60000) if e.get('duration') else None,
                     'tvdb_id': g.get('tvdb'), 'tmdb_id': g.get('tmdb'), 'imdb_id': g.get('imdb')},
                    {'mode': 'full', 'directors': e.get('directors') or [], 'writers': e.get('writers') or []}))
    return out


def _episode_xml(numbers: List, eps: List[Dict], show_title: str) -> str:
    from utilities import nfo_writer
    pairs = _episodes_for_file(numbers, eps)
    return nfo_writer.build_episode_nfo([p[0] for p in pairs], show_title, [p[1] for p in pairs],
                                        marker=nfo_writer.SNAPSHOT_MARKER)


def _show_xml(imdb_id: str, show: Dict) -> str:
    from utilities import nfo_writer
    g = show.get('guids') or {}
    ids = {'tvdb': g.get('tvdb'), 'tmdb': g.get('tmdb') or show.get('tmdb_id'), 'imdb': g.get('imdb') or imdb_id}
    return nfo_writer.build_tvshow_nfo({'ids': ids}, marker=nfo_writer.SNAPSHOT_MARKER)


def _write_show_nfo(folder: str, imdb_id: str, show: Optional[Dict], counts: Dict) -> None:
    from utilities import nfo_writer
    if show is None:
        return
    # A folder holding more than one show gets no tvshow.nfo
    if nfo_writer._show_imdb_ids_in(folder) - {imdb_id}:
        counts['skipped_folders'] += 1
        return
    counts[nfo_writer._write(os.path.join(folder, nfo_writer.SHOW_NFO), _show_xml(imdb_id, show),
                             replace=(nfo_writer.SNAPSHOT,))] += 1


def _write_all(snapshot_id: int, imdb_id: Optional[str] = None) -> Dict[str, int]:
    """Write a snapshot's NFO files (one show's with imdb_id). Shows Clyde's
    NFO writer covers are left to it; files that aren't snapshot NFOs are
    never replaced."""
    from utilities import nfo_writer
    counts = {'written': 0, 'unchanged': 0, 'foreign': 0, 'error': 0, 'gone': 0,
              'writer_shows': 0, 'skipped_folders': 0}
    root = nfo_writer.library_root()
    if not root:
        return counts
    conn = _conn()
    try:
        sql, params = "SELECT path, imdb_id, numbers, data FROM files WHERE snapshot_id = ?", [snapshot_id]
        if imdb_id:
            sql += " AND imdb_id = ?"
            params.append(imdb_id)
        rows = conn.execute(sql, params).fetchall()
        show_sql, show_params = "SELECT imdb_id, data FROM shows WHERE snapshot_id = ?", [snapshot_id]
        if imdb_id:
            show_sql += " AND imdb_id = ?"
            show_params.append(imdb_id)
        shows = {i: json.loads(d) for i, d in conn.execute(show_sql, show_params).fetchall()}
    finally:
        conn.close()
    by_show: Dict[str, List] = {}
    for path, imdb, numbers, data in rows:
        by_show.setdefault(imdb or '', []).append((path, json.loads(numbers or '[]'), json.loads(data or '[]')))
    for n, (imdb, files) in enumerate(sorted(by_show.items())):
        _job['step'] = f'writing NFO files ({n + 1}/{len(by_show)} shows)'
        if imdb and nfo_writer.wants_show(imdb):
            counts['writer_shows'] += 1
            continue
        show = shows.get(imdb)
        title = (show or {}).get('title') or (files[0][2][0].get('show_title') if files[0][2] else '') or ''
        folders = set()
        for path, numbers, eps in files:
            if not eps or not os.path.lexists(path):
                counts['gone'] += 1
                continue
            counts[nfo_writer._write(nfo_writer.episode_nfo_path(path), _episode_xml(numbers, eps, title),
                                     replace=(nfo_writer.SNAPSHOT,))] += 1
            folder = nfo_writer.show_folder(path, root)
            if folder:
                folders.add(folder)
        for folder in folders:
            _write_show_nfo(folder, imdb, show, counts)
    logging.info(f"{_LOG} snapshot {snapshot_id} NFO files"
                 + (f' for {imdb_id}' if imdb_id else '') + f": {counts}")
    return counts


def _remove_all() -> int:
    from utilities import nfo_writer
    root = nfo_writer.library_root()
    n = 0
    if root and os.path.isdir(root):
        for d, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith('.nfo') and nfo_writer._remove_ours(os.path.join(d, f), (nfo_writer.SNAPSHOT,)):
                    n += 1
    logging.info(f"{_LOG} removed {n} snapshot NFO file(s)")
    return n


def restore_show(imdb_id: str) -> None:
    """The NFO writer dropped a show's files (it no longer covers the show):
    put the snapshot's back."""
    sid = in_use()
    if sid and os.path.exists(_db_path()):
        _write_all(sid, imdb_id)


def restore_all() -> Dict[str, int]:
    """NFO files were turned off: put the snapshot's NFOs back."""
    sid = in_use()
    if sid and os.path.exists(_db_path()):
        return _write_all(sid)
    return {}


def carry_to_symlink(media_item_id: int, new_path: str) -> bool:
    """A new symlink for an episode in the snapshot (an upgrade): give it the
    episode's snapshot NFO. Only when the item and the new file name still
    have the snapshot's season/episode; a renumbered file gets nothing."""
    from utilities import nfo_writer
    sid = in_use()
    if not sid or not new_path or not os.path.exists(_db_path()):
        return False
    conn = _conn()
    try:
        item = conn.execute("SELECT path, season, episode FROM items WHERE snapshot_id = ? AND media_item_id = ?",
                            (sid, int(media_item_id))).fetchone()
        if not item:
            return False
        old_path, season, episode = item
        m = _SXXEXX.search(os.path.basename(new_path))
        if not m or (int(m.group(1)), int(m.group(2))) != (season, episode):
            return False
        current = nfo_writer._episode_rows_by_id(int(media_item_id))
        if not current or (current[0].get('season_number'), current[0].get('episode_number')) != (season, episode):
            return False
        f = conn.execute("SELECT imdb_id, numbers, data FROM files WHERE snapshot_id = ? AND path = ?",
                         (sid, old_path)).fetchone()
        if not f:
            return False
        imdb_id, numbers, data = f[0], json.loads(f[1] or '[]'), json.loads(f[2] or '[]')
        if imdb_id and nfo_writer.wants_show(imdb_id):
            return False
        pairs = _episodes_for_file(numbers, data)
        eps = [e for (ep, _plan), e in zip(pairs, sorted(data, key=lambda e: (e.get('season') or 0, e.get('episode') or 0)))
               if (ep['season'], ep['episode']) == (season, episode)]
        if not eps:
            return False
        show_row = conn.execute("SELECT data FROM shows WHERE snapshot_id = ? AND imdb_id = ?", (sid, imdb_id)).fetchone()
        show = json.loads(show_row[0]) if show_row else None
        new_numbers = [[season, episode]]
        result = nfo_writer._write(nfo_writer.episode_nfo_path(new_path),
                                   _episode_xml(new_numbers, eps, (show or {}).get('title') or eps[0].get('show_title') or ''),
                                   replace=(nfo_writer.SNAPSHOT,))
        if os.path.normpath(old_path) != os.path.normpath(new_path):
            conn.execute("INSERT OR REPLACE INTO files (snapshot_id, path, imdb_id, numbers, data) VALUES (?, ?, ?, ?, ?)",
                         (sid, new_path, imdb_id, json.dumps(new_numbers), json.dumps(eps)))
            conn.execute("UPDATE items SET path = ? WHERE snapshot_id = ? AND media_item_id = ?",
                         (new_path, sid, int(media_item_id)))
            conn.commit()
    finally:
        conn.close()
    folder = nfo_writer.show_folder(new_path)
    if folder and not os.path.exists(os.path.join(folder, nfo_writer.SHOW_NFO)):
        _write_show_nfo(folder, imdb_id, show, {'written': 0, 'unchanged': 0, 'foreign': 0, 'error': 0,
                                                'skipped_folders': 0})
    if result == 'written':
        logging.info(f"{_LOG} {os.path.basename(new_path)}: kept the episode's snapshot NFO")
    return result in ('written', 'unchanged')
