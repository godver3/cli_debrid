"""
NFO files for the symlinked TV library (Symlinked/Local mode).

Plex numbers shows by TMDB once it drops TheTVDB (2026-12-03), while
Clyde names episode files by its own metadata (TVDB numbering, or the
order chosen for the show). Where the two disagree (anime, specials, split
seasons, DVD/absolute orders) Plex would show the wrong episode titles. An
NFO next to every episode file carries Clyde's title, plot and air date
for exactly the numbers in the file name, and Plex's NFO agent, Jellyfin,
Emby and Kodi all read it.

Files follow the Kodi format (as Sonarr writes it):

    <show folder>/tvshow.nfo        <tvshow> ... </tvshow>
    <episode file stem>.nfo         one <episodedetails> per episode in the file

Every file written here carries MARKER; NFOs made from a snapshot of Plex's
own metadata (utilities/plex_snapshot.py) carry SNAPSHOT_MARKER. Files with
neither (made by the user or another tool) are never overwritten or deleted.
Unchanged files are not rewritten, so the media server isn't sent a change
for nothing.

Off unless turned on from Tools → NFO Files (setting NFO.mode;
Symlinked/Local only).
"""

import logging
import os
import re
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from utilities.settings import get_setting

_LOG = '[NFO]'
MARKER = '<!-- written by Clyde -->'
# Files from before the rename count as this writer's too
LEGACY_MARKERS = ('<!-- written by cli_debrid -->',)
# Made from a snapshot of Plex's metadata: kept when NFO files are off or a
# show gets no NFOs of its own; the writer may replace them
SNAPSHOT_MARKER = '<!-- written by Clyde from Plex metadata -->'
WRITER, SNAPSHOT = 'writer', 'snapshot'
SHOW_NFO = 'tvshow.nfo'
VIDEO_EXTS = ('.mkv', '.mp4', '.avi', '.m4v', '.ts', '.m2ts', '.mov', '.wmv', '.webm', '.mpg', '.mpeg', '.iso')
_SEASON_DIR = re.compile(r'^(season[ ._-]*\d+|specials|s\d{1,4})$', re.I)
_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_BAD_XML_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


# ─── settings ────────────────────────────────────────────────────────────────

SECTION = 'NFO'
MODES = ('off', 'changed', 'all')


def symlinked_mode() -> bool:
    return get_setting('File Management', 'file_collection_management', '') == 'Symlinked/Local'


def mode() -> str:
    """'off', 'changed' (only shows whose episode order was changed) or
    'all'. NFO.enabled (the first version's switch) reads as 'all'."""
    m = str(get_setting(SECTION, 'mode', '') or '').strip().lower()
    if m in MODES:
        return m
    return 'all' if get_setting(SECTION, 'enabled', False) else 'off'


def enabled() -> bool:
    """NFO files are on (either mode) and the library is symlinked."""
    return mode() != 'off' and symlinked_mode()


def switch_plex_agent() -> bool:
    """Turning NFO files on also switches the Plex TV library to the NFO
    agent (NFO.plex_agent = 'switch', the default). 'leave': the files are
    written and Plex is left exactly as it is."""
    return str(get_setting(SECTION, 'plex_agent', 'switch') or 'switch').strip().lower() != 'leave'


def plex_reads_nfos() -> bool:
    """Clyde switched a Plex TV library to its NFO agent. Only then do NFO
    files change what Clyde does in Plex (TMDB numbering, releasing old
    title locks); written but unread, Plex is handled as without them."""
    try:
        from utilities.plex_nfo_agent import applied
        return applied()
    except Exception:
        return False


# Per-show override (Tools -> NFO Files -> Check a show):
#   'auto' (default) follows the mode, 'full' always writes every episode's
#   details, 'none' never writes NFO files for the show.
OVERRIDES = ('auto', 'full', 'none')
_ov_lock = threading.Lock()


def _overrides_path() -> str:
    return os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'nfo_show_overrides.json')


def _load_overrides() -> Dict[str, str]:
    try:
        import json
        with open(_overrides_path(), encoding='utf-8') as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if v in OVERRIDES} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def show_override(imdb_id: Optional[str]) -> str:
    return _load_overrides().get(imdb_id or '', 'auto')


def set_show_override(imdb_id: str, value: str) -> None:
    import json
    if value not in OVERRIDES:
        raise ValueError(f'override must be one of {OVERRIDES}')
    with _ov_lock:
        data = _load_overrides()
        if value == 'auto':
            data.pop(imdb_id, None)
        else:
            data[imdb_id] = value
        tmp = _overrides_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, _overrides_path())


def wants_show(imdb_id: Optional[str]) -> bool:
    """Does this show get NFO files? 'all': every show; 'changed': shows
    whose episode order isn't the default (the rest look exactly as Plex
    makes them, as without NFOs). A per-show override wins."""
    if not imdb_id or not enabled():
        return False
    override = show_override(imdb_id)
    if override != 'auto':
        return override == 'full'
    if mode() == 'all':
        return True
    try:
        from cli_battery.app.episode_order import show_episode_order
        return show_episode_order(imdb_id) is not None
    except Exception:
        return False


def library_root() -> str:
    return (get_setting('File Management', 'symlinked_files_path', '') or '').rstrip('/\\')


# ─── XML ─────────────────────────────────────────────────────────────────────

def _text(value) -> str:
    # \r\n -> \n: metadata plots can carry Windows line ends, and a file read
    # back in text mode never equals them, so it was rewritten on every run
    s = str(value).replace('\r\n', '\n').replace('\r', '\n')
    return escape(_BAD_XML_CHARS.sub('', s).strip())


def _tag(name: str, value, attrs: str = '') -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return f'  <{name}{attrs}>{_text(value)}</{name}>'


def _date(value) -> Optional[str]:
    s = str(value or '')[:10]
    return s if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s) else None


def _int(value) -> Optional[int]:
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _uniqueids(ids: Dict[str, Optional[str]], default: Optional[str]) -> List[str]:
    out = []
    for kind in ('tvdb', 'tmdb', 'imdb'):
        val = ids.get(kind)
        if val:
            flag = ' default="true"' if kind == default else ''
            out.append(f'  <uniqueid type="{kind}"{flag}>{_text(val)}</uniqueid>')
    return out


def build_tvshow_nfo(show: Dict, full: bool = False, marker: str = MARKER) -> str:
    """tvshow.nfo for a show: {'title', 'year', 'overview', 'runtime',
    'premiered', 'status', 'network', 'genres', 'ids': {imdb, tmdb, tvdb}}.

    By default only the ids: Plex Series (and Jellyfin's online providers)
    then supply the show's title, description, artwork, cast and ratings
    exactly as without NFOs. full=True writes the show details too."""
    ids = {k: (str(v) if v else None) for k, v in (show.get('ids') or {}).items()}
    # The default id is the one Clyde numbers episodes by: TVDB when
    # known, else TMDB (Kodi/Plex use it to identify the show).
    default = 'tvdb' if ids.get('tvdb') else 'tmdb' if ids.get('tmdb') else 'imdb'
    lines = []
    if full:
        lines += [
            _tag('title', show.get('title')),
            _tag('originaltitle', show.get('original_title') or show.get('title')),
            _tag('sorttitle', show.get('sort_title')),
            _tag('year', _int(show.get('year'))),
            _tag('plot', show.get('overview')),
            _tag('runtime', _int(show.get('runtime'))),
            _tag('premiered', _date(show.get('premiered'))),
            _tag('status', _status(show.get('status'))),
            _tag('studio', show.get('network')),
        ]
        lines += [_tag('genre', g) for g in (show.get('genres') or []) if g]
    lines += _uniqueids(ids, default)
    body = '\n'.join(x for x in lines if x)
    return f'{_XML_DECL}\n{marker}\n<tvshow>\n{body}\n</tvshow>\n'


def _status(status) -> Optional[str]:
    s = (status or '').strip().lower()
    if not s:
        return None
    if s in ('ended', 'canceled', 'cancelled'):
        return 'Ended'
    return 'Continuing'


def build_episode_nfo(episodes: List[Dict], show_title: str, plans: Optional[List[Dict]] = None,
                      marker: str = MARKER) -> str:
    """One <episodedetails> per episode in the file (multi-episode files get
    several, as Sonarr writes them). Each episode: {'season', 'episode',
    'title', 'overview', 'aired', 'runtime', 'tvdb_id', 'imdb_id', and
    optionally 'tmdb_id'}.

    plans (utilities/nfo_sources.plan_episodes), one per episode:
    'minimal' writes only the numbers and ids, so the media server's own
    agent supplies every detail; 'full' (the default) writes the details,
    plus thumb/directors/writers/actors/rating when the plan has them."""
    plans = plans or [{'mode': 'full'}] * len(episodes)
    blocks = []
    for ep, plan in sorted(zip(episodes, plans), key=lambda x: (x[0].get('season') or 0, x[0].get('episode') or 0)):
        ids = {'tvdb': ep.get('tvdb_id'), 'tmdb': ep.get('tmdb_id'), 'imdb': ep.get('imdb_id')}
        lines = [
            _tag('season', ep.get('season') if ep.get('season') is not None else None),
            _tag('episode', ep.get('episode') if ep.get('episode') is not None else None),
        ]
        if plan.get('mode') != 'minimal':
            lines = [_tag('title', ep.get('title')), _tag('showtitle', show_title)] + lines + [
                _tag('plot', ep.get('overview')),
                _tag('aired', _date(ep.get('aired'))),
                _tag('runtime', _int(ep.get('runtime'))),
                _tag('thumb', plan.get('thumb')),
            ]
            lines += [_tag('director', d) for d in plan.get('directors') or []]
            lines += [_tag('credits', w) for w in plan.get('writers') or []]
            # No <ratings>: Plex mixes them up with a custom agent (known issue,
            # Plex staff on the NFO agent thread); Plex Series supplies ratings
            for n, a in enumerate(plan.get('actors') or []):
                parts = [f'    <name>{_text(a["name"])}</name>']
                if a.get('role'):
                    parts.append(f'    <role>{_text(a["role"])}</role>')
                if a.get('thumb'):
                    parts.append(f'    <thumb>{_text(a["thumb"])}</thumb>')
                parts.append(f'    <order>{_text(a["order"] if a.get("order") is not None else n)}</order>')
                lines.append('  <actor>\n' + '\n'.join(parts) + '\n  </actor>')
        lines += _uniqueids({k: (str(v) if v else None) for k, v in ids.items()},
                            'tvdb' if ids['tvdb'] else 'tmdb' if ids['tmdb'] else 'imdb')
        body = '\n'.join(x for x in lines if x is not None)
        blocks.append(f'<episodedetails>\n{body}\n</episodedetails>')
    return f'{_XML_DECL}\n{marker}\n' + '\n'.join(blocks) + '\n'


# ─── files ───────────────────────────────────────────────────────────────────

def _kind_of(head: str) -> Optional[str]:
    """WRITER, SNAPSHOT or None (not ours) for the start of an NFO file."""
    if SNAPSHOT_MARKER in head:
        return SNAPSHOT
    if MARKER in head or any(m in head for m in LEGACY_MARKERS):
        return WRITER
    return None


def nfo_kind(path: str) -> Optional[str]:
    """WRITER, SNAPSHOT, None (someone else's) or '' (no file)."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            return _kind_of(f.read(400))
    except FileNotFoundError:
        return ''
    except OSError:
        return None


def _write(path: str, content: str, replace: Tuple[str, ...] = (WRITER, SNAPSHOT)) -> str:
    """'written', 'unchanged', 'foreign' (not one of `replace`, left alone) or
    'error'."""
    try:
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isfile(path):
                return 'foreign'
            with open(path, encoding='utf-8', errors='replace') as f:
                old = f.read()
            if _kind_of(old[:400]) not in replace:
                return 'foreign'
            if old == content:
                return 'unchanged'
        tmp = f'{path}.tmp-cli'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        os.replace(tmp, path)
        return 'written'
    except OSError as e:
        logging.warning(f"{_LOG} could not write {path}: {e}")
        return 'error'


def _remove_ours(path: str, kinds: Tuple[str, ...] = (WRITER,)) -> bool:
    if os.path.isfile(path) and not os.path.islink(path) and nfo_kind(path) in kinds:
        try:
            os.remove(path)
            return True
        except OSError as e:
            logging.warning(f"{_LOG} could not remove {path}: {e}")
    return False


def episode_nfo_path(video_path: str) -> str:
    return os.path.splitext(video_path)[0] + '.nfo'


def show_folder(video_path: str, root: Optional[str] = None) -> Optional[str]:
    """The show's folder for an episode file: the parent, or the grandparent
    when the parent is a season folder. None when that would be the library
    root or a folder above it (a flat layout has no per-show folder)."""
    root = os.path.normpath(root if root is not None else library_root()) if (root or library_root()) else ''
    folder = os.path.dirname(video_path)
    if _SEASON_DIR.match(os.path.basename(folder)):
        folder = os.path.dirname(folder)
    folder = os.path.normpath(folder)
    if not root or folder == root or not folder.startswith(root + os.sep):
        return None
    return folder


def _has_video(folder: str) -> bool:
    for _dir, _dirs, files in os.walk(folder):
        if any(f.lower().endswith(VIDEO_EXTS) for f in files):
            return True
    return False


def remove_for_video(video_path: str) -> List[str]:
    """A library file is being removed: drop its episode NFO, and the show's
    tvshow.nfo once no video is left in the show folder. Only our files."""
    removed = []
    # Also with NFO files off: snapshot NFOs (made from Plex's metadata) stay
    # then. Without any NFO of ours this is two file checks, so deletions
    # work exactly as without the feature.
    if not video_path or not symlinked_mode():
        return removed
    kinds = (WRITER, SNAPSHOT)
    # Called just before or after the file itself is removed
    nfo = episode_nfo_path(video_path)
    # A .mkv and a .mp4 with the same name share one NFO: it stays while
    # the other one is still here
    if not _same_stem_video_left(video_path) and _remove_ours(nfo, kinds):
        removed.append(nfo)
    folder = show_folder(video_path)
    show_nfo = os.path.join(folder, SHOW_NFO) if folder else None
    if show_nfo and nfo_kind(show_nfo) in kinds:
        others = [f for f in _videos_in(folder) if os.path.normpath(f) != os.path.normpath(video_path)]
        if not others and _remove_ours(show_nfo, kinds):
            removed.append(show_nfo)
    if removed:
        logging.info(f"{_LOG} removed {len(removed)} NFO file(s) of {os.path.basename(video_path)}")
    return removed


def _same_stem_video_left(video_path: str) -> bool:
    folder, name = os.path.split(video_path)
    stem = os.path.splitext(name)[0]
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(f != name and f.lower().endswith(VIDEO_EXTS) and os.path.splitext(f)[0] == stem
               and os.path.lexists(os.path.join(folder, f)) for f in names)


def _videos_in(folder: str) -> List[str]:
    out = []
    for d, _dirs, files in os.walk(folder):
        out += [os.path.join(d, f) for f in files if f.lower().endswith(VIDEO_EXTS)]
    return out


def prune_orphans(folder: str) -> int:
    """Episode NFOs of ours in `folder` (recursively) with no video of the
    same name next to them: the episode was deleted or renamed."""
    n = 0
    for d, _dirs, files in os.walk(folder):
        stems = {os.path.splitext(f)[0] for f in files if f.lower().endswith(VIDEO_EXTS)}
        for f in files:
            if f.lower().endswith('.nfo') and f != SHOW_NFO and os.path.splitext(f)[0] not in stems:
                if _remove_ours(os.path.join(d, f), (WRITER, SNAPSHOT)):
                    n += 1
    return n


# ─── data ────────────────────────────────────────────────────────────────────

_COLS = ('id', 'imdb_id', 'tmdb_id', 'title', 'year', 'season_number', 'episode_number',
         'episode_title', 'release_date', 'runtime', 'location_on_disk')


def _episode_rows(imdb_id: Optional[str] = None) -> List[Dict]:
    """Collected episodes that have a library file."""
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        sql = (f"SELECT {', '.join(_COLS)} FROM media_items WHERE type = 'episode' "
               "AND state IN ('Collected', 'Upgrading') AND location_on_disk IS NOT NULL "
               "AND location_on_disk != ''")
        params: Tuple = ()
        if imdb_id:
            sql += ' AND imdb_id = ?'
            params = (imdb_id,)
        return [dict(zip(_COLS, r)) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _show_imdb_ids_in(folder: str) -> set:
    """Distinct shows with library files under `folder` (guards a shared folder)."""
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT DISTINCT imdb_id FROM media_items WHERE type = 'episode' "
                            "AND state IN ('Collected', 'Upgrading') AND location_on_disk LIKE ? ESCAPE '\\'",
                            (_like_prefix(folder),)).fetchall()
        return {r[0] for r in rows if r[0]}
    finally:
        conn.close()


def _like_prefix(folder: str) -> str:
    esc = folder.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return esc + os.sep.replace('\\', '\\\\') + '%'


def _battery(imdb_id: str) -> Tuple[Dict, Dict]:
    """(show metadata, seasons) from cli_battery; seasons are stored in the
    show's chosen order, the same numbering the file names use."""
    from cli_battery.app.direct_api import DirectAPI
    meta, seasons = {}, {}
    try:
        meta = DirectAPI.get_show_metadata(imdb_id)[0] or {}
    except Exception as e:
        logging.debug(f"{_LOG} show metadata for {imdb_id} unavailable: {e}")
    try:
        seasons = DirectAPI.get_show_seasons(imdb_id)[0] or {}
    except Exception as e:
        logging.debug(f"{_LOG} seasons for {imdb_id} unavailable: {e}")
    return meta, seasons


def _season_lookup(seasons: Dict) -> Dict[Tuple[int, int], Dict]:
    out = {}
    for sn, season in (seasons or {}).items():
        for en, ep in ((season or {}).get('episodes') or {}).items():
            try:
                out[(int(sn), int(en))] = ep or {}
            except (TypeError, ValueError):
                continue
    return out


def _show_info(meta: Dict, row: Dict, seasons: Dict) -> Dict:
    ids = dict(meta.get('ids') or {})
    ids.setdefault('imdb', row.get('imdb_id'))
    if row.get('tmdb_id'):
        ids.setdefault('tmdb', row.get('tmdb_id'))
    first = min((ep.get('first_aired') or '' for (s, _e), ep in _season_lookup(seasons).items()
                 if s > 0 and ep.get('first_aired')), default=None)
    return {
        'title': meta.get('title') or row.get('title'),
        'year': meta.get('year') or row.get('year'),
        'overview': meta.get('overview'),
        'runtime': meta.get('runtime'),
        'premiered': first,
        'status': meta.get('status'),
        'network': meta.get('network'),
        'genres': meta.get('genres') or [],
        'ids': {'imdb': ids.get('imdb'), 'tmdb': ids.get('tmdb'), 'tvdb': ids.get('tvdb')},
    }


def _episode_info(row: Dict, lookup: Dict) -> Dict:
    s, e = row.get('season_number'), row.get('episode_number')
    ep = lookup.get((s, e)) if s is not None and e is not None else None
    ep = ep or {}
    return {
        'season': s, 'episode': e,
        'title': ep.get('title') or row.get('episode_title'),
        'overview': ep.get('overview'),
        'aired': ep.get('first_aired') or row.get('release_date'),
        'runtime': ep.get('runtime') or row.get('runtime'),
        'tvdb_id': ep.get('tvdb_id'),
        'imdb_id': ep.get('imdb_id'),
    }


def _plans(episodes: List[Dict], show: Dict) -> List[Dict]:
    if not episodes:
        return []
    try:
        from utilities.nfo_sources import plan_episodes
        plans = plan_episodes(episodes, (show.get('ids') or {}).get('tmdb'),
                              force_full=show.get('force_full', False))
        return [plans.get(i, {'mode': 'full'}) for i in range(len(episodes))]
    except Exception as e:
        logging.warning(f"{_LOG} episode details for {show.get('title')}: {e}")
        return [{'mode': 'full'}] * len(episodes)


# ─── writing ─────────────────────────────────────────────────────────────────

def _empty_counts() -> Dict[str, int]:
    return {'written': 0, 'unchanged': 0, 'foreign': 0, 'error': 0, 'removed': 0, 'skipped_folders': 0}


def write_show(imdb_id: str, rows: Optional[List[Dict]] = None, prune: bool = True,
               only_paths: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Write (or refresh) the NFOs of one show's library files.

    only_paths limits the episode NFOs written to those files (a new symlink);
    tvshow.nfo is still made when missing."""
    counts = _empty_counts()
    rows = rows if rows is not None else _episode_rows(imdb_id)
    if not wants_show(imdb_id):
        # 'changed' mode and the show is back on its default order (or the
        # mode changed): its NFO files go, so Plex shows its own details
        if only_paths is None:
            counts['removed'] += remove_show(imdb_id, rows)
            if counts['removed']:
                # NFOs made from a Plex snapshot come back in their place
                _restore_snapshot(imdb_id)
        return counts
    root = library_root()
    rows = [r for r in rows if r.get('location_on_disk') and os.path.lexists(r['location_on_disk'])]
    if not rows or not root:
        return counts
    meta, seasons = _battery(imdb_id)
    lookup = _season_lookup(seasons)
    show = _show_info(meta, rows[0], seasons)
    show['force_full'] = show_override(imdb_id) == 'full'

    title = show['title'] or rows[0].get('title') or ''

    by_file: Dict[str, List[Dict]] = {}
    for r in rows:
        by_file.setdefault(r['location_on_disk'], []).append(r)
    wanted = {os.path.normpath(p) for p in only_paths} if only_paths else None

    # Which episodes Plex (TMDB numbering) already gets right, and the
    # details of the ones it doesn't: computed once for the files written
    todo = [(p, fr) for p, fr in by_file.items() if wanted is None or os.path.normpath(p) in wanted]
    infos: Dict[str, List[Dict]] = {}
    flat: List[Dict] = []
    for path, file_rows in todo:
        seen, eps = set(), []
        for r in file_rows:
            key = (r.get('season_number'), r.get('episode_number'))
            if key not in seen:
                seen.add(key)
                eps.append(_episode_info(r, lookup))
        infos[path] = eps
        flat += eps
    plans_flat = _plans(flat, show)
    plan_of = {id(ep): plans_flat[i] for i, ep in enumerate(flat)}

    folders = set()
    for path, file_rows in by_file.items():
        folder = show_folder(path, root)
        if folder:
            folders.add(folder)
        if wanted is not None and os.path.normpath(path) not in wanted:
            continue
        eps = infos[path]
        counts[_write(episode_nfo_path(path), build_episode_nfo(eps, title, [plan_of[id(e)] for e in eps]))] += 1

    show_xml = build_tvshow_nfo(show)
    for folder in folders:
        target = os.path.join(folder, SHOW_NFO)
        if wanted is not None and os.path.exists(target):
            continue
        # A folder holding more than one show (an unusual template) gets no
        # tvshow.nfo: it would make the media server see one show.
        if _show_imdb_ids_in(folder) - {imdb_id}:
            counts['skipped_folders'] += 1
            continue
        counts[_write(target, show_xml)] += 1
        if prune and wanted is None:
            counts['removed'] += prune_orphans(folder)
    return counts


_pending_lock = threading.Lock()
_pending: Dict[int, str] = {}          # media item id -> new symlink path
_pending_event = threading.Event()
_worker: Optional[threading.Thread] = None
DEBOUNCE_SECONDS = 2.0


def write_for_symlink(media_item_id: Optional[int], symlink_path: str) -> None:
    """Hook after a symlink was made: queue that file's NFO. One worker
    writes queued files per show after a short pause, so re-linking a whole
    show (an order change, a resync) loads the show's metadata once."""
    global _worker
    if not media_item_id:
        return
    if not enabled():
        _carry_snapshot(media_item_id, symlink_path)
        return
    with _pending_lock:
        _pending[int(media_item_id)] = symlink_path
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name='nfo-write', daemon=True)
            _worker.start()
    _pending_event.set()


def _carry_snapshot(media_item_id: int, symlink_path: str) -> None:
    """An upgraded episode of a show the writer doesn't cover keeps the NFO
    made from Plex's metadata."""
    try:
        from utilities import plex_snapshot
        plex_snapshot.carry_to_symlink(media_item_id, symlink_path)
    except Exception as e:
        logging.debug(f"{_LOG} snapshot NFO for {symlink_path}: {e}")


def _drain() -> None:
    while True:
        if not _pending_event.wait(timeout=60):
            with _pending_lock:
                if not _pending:
                    return  # idle: the next hook starts a new worker
            continue
        # Wait until no new file has arrived for DEBOUNCE_SECONDS
        while True:
            _pending_event.clear()
            if not _pending_event.wait(timeout=DEBOUNCE_SECONDS):
                break
        with _pending_lock:
            batch = dict(_pending)
            _pending.clear()
        if batch:
            _write_batch(batch)


def _write_batch(batch: Dict[int, str]) -> None:
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            ids = list(batch)
            rows = conn.execute(f"SELECT id, imdb_id, type FROM media_items WHERE id IN ({','.join('?' * len(ids))})",
                                ids).fetchall()
        finally:
            conn.close()
        by_show: Dict[str, List[int]] = {}
        for item_id, imdb_id, kind in rows:
            if kind == 'episode' and imdb_id:
                by_show.setdefault(imdb_id, []).append(item_id)
        for imdb_id, item_ids in by_show.items():
            if not wants_show(imdb_id):
                # 'changed' mode on a default-order show, or the show's
                # override is 'none': no NFO of the writer's, so an upgrade
                # keeps the snapshot's (its old file's NFO is gone by now)
                for i in item_ids:
                    _carry_snapshot(i, batch[i])
                continue
            paths = {os.path.normpath(batch[i]) for i in item_ids}
            show_rows = _episode_rows(imdb_id)
            rows_here = [r for r in show_rows if os.path.normpath(r['location_on_disk']) in paths]
            # An item whose state/location isn't saved yet still gets its file's NFO
            known = {r['id'] for r in rows_here}
            for i in item_ids:
                if i not in known:
                    for r in _episode_rows_by_id(i):
                        r['location_on_disk'] = batch[i]
                        rows_here.append(r)
            c = write_show(imdb_id, rows=rows_here, prune=False, only_paths=paths)
            if c['written']:
                logging.info(f"{_LOG} wrote {c['written']} NFO file(s) for {imdb_id}")
    except Exception as e:
        logging.warning(f"{_LOG} queued NFO writes failed: {e}")


def _episode_rows_by_id(media_item_id: int) -> List[Dict]:
    from database.core import get_db_connection
    conn = get_db_connection()
    try:
        r = conn.execute(f"SELECT {', '.join(_COLS)} FROM media_items WHERE id = ?", (media_item_id,)).fetchone()
        return [dict(zip(_COLS, r))] if r else []
    finally:
        conn.close()


def _restore_snapshot(imdb_id: str) -> None:
    try:
        from utilities import plex_snapshot
        plex_snapshot.restore_show(imdb_id)
    except Exception as e:
        logging.debug(f"{_LOG} snapshot NFOs for {imdb_id} not restored: {e}")


def refresh_show(imdb_id: str) -> str:
    """After a show's episode order changed: rewrite its NFOs. Returns a note."""
    if not enabled():
        return ''
    try:
        c = write_show(imdb_id)
        if not wants_show(imdb_id):
            return f"NFO files removed ({c['removed']}): the show is back on its default order" if c['removed'] else ''
        return (f"NFO files: {c['written']} updated, {c['unchanged']} unchanged"
                + (f", {c['removed']} removed" if c['removed'] else '')) if (c['written'] or c['removed']) else ''
    except Exception as e:
        logging.error(f"{_LOG} rewriting NFOs for {imdb_id} failed: {e}", exc_info=True)
        return f'NFO files not updated: {e}'


# ─── whole library ───────────────────────────────────────────────────────────

_job_lock = threading.Lock()
_job: Dict = {'running': False}


def job_status() -> Dict:
    return dict(_job)


def _set_job(**kw):
    _job.update(kw)


def write_library(on_done=None) -> bool:
    """Write NFOs for every show in the library, in the background. False
    when a run is already going."""
    if not _job_lock.acquire(blocking=False):
        return False
    _job.clear()
    _job.update({'running': True, 'kind': 'write', 'started': time.time(), 'shows_done': 0,
                 'shows_total': 0, 'counts': _empty_counts(), 'current': '', 'error': None})

    def run():
        try:
            rows = _episode_rows()
            by_show: Dict[str, List[Dict]] = {}
            for r in rows:
                if r.get('imdb_id'):
                    by_show.setdefault(r['imdb_id'], []).append(r)
            _set_job(shows_total=len(by_show))
            for imdb_id, show_rows in sorted(by_show.items(), key=lambda kv: kv[1][0].get('title') or ''):
                _set_job(current=show_rows[0].get('title') or imdb_id)
                try:
                    c = write_show(imdb_id, rows=show_rows)
                    for k, v in c.items():
                        _job['counts'][k] = _job['counts'].get(k, 0) + v
                except Exception as e:
                    _job['counts']['error'] += 1
                    logging.warning(f"{_LOG} {imdb_id}: {e}")
                _job['shows_done'] += 1
            logging.info(f"{_LOG} library: {_job['shows_done']} shows, {_job['counts']}")
            if on_done:
                on_done()
        except Exception as e:
            logging.error(f"{_LOG} library run failed: {e}", exc_info=True)
            _set_job(error=str(e))
        finally:
            _set_job(running=False, finished=time.time(), current='')
            _job_lock.release()

    threading.Thread(target=run, name='nfo-library', daemon=True).start()
    return True


def remove_show(imdb_id: str, rows: Optional[List[Dict]] = None) -> int:
    """Delete our NFO files of one show (its episode NFOs and tvshow.nfo)."""
    rows = rows if rows is not None else _episode_rows(imdb_id)
    root = library_root()
    n, folders = 0, set()
    for r in rows:
        loc = r.get('location_on_disk')
        if not loc:
            continue
        if _remove_ours(episode_nfo_path(loc)):
            n += 1
        folder = show_folder(loc, root)
        if folder:
            folders.add(folder)
    for folder in folders:
        if _show_imdb_ids_in(folder) - {imdb_id}:
            continue
        if _remove_ours(os.path.join(folder, SHOW_NFO)):
            n += 1
        n += prune_orphans(folder)
    return n


def remove_library() -> Dict[str, int]:
    """Delete every NFO written here (turning the feature off). Snapshot
    NFOs stay (plex_snapshot.write_nfos puts back the ones replaced)."""
    root = library_root()
    n = 0
    if root and os.path.isdir(root):
        for d, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith('.nfo') and _remove_ours(os.path.join(d, f)):
                    n += 1
    logging.info(f"{_LOG} removed {n} NFO file(s) written by Clyde")
    return {'removed': n}
