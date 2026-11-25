"""Which collected items have which subtitle languages.

Sources: sidecar files next to the library file (Symlinked/Local) and the
downloader's own history (both modes; the only list-wide source in Plex mode,
where subtitles live in Plex). Shared by the Subtitles page, its "Search all
missing" button and the Bulk subtitles task.
"""
import os

from utilities.subtitle_lang import covers, sidecar_lang

SUB_EXTS = ('.srt', '.ass', '.ssa', '.sub', '.vtt', '.smi')
ROW_CAP = 5000


def is_plex_mode():
    from utilities.settings import get_setting
    return get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'


def wanted_langs():
    """Configured languages as (label, key) pairs, e.g. ('fr', 'fra'),
    ('pt-BR', 'por-BR'); key from utilities.subtitle_lang."""
    from utilities.config.downsub_config import config
    from utilities.downsub import expand_languages
    from utilities.subtitle_lang import key
    config.reload()
    out, seen = [], set()
    for lang in expand_languages(config.SUBTITLE_LANGUAGES):
        k = key(lang)
        if k and k not in seen:
            seen.add(k)
            out.append((getattr(lang, 'ietf', None) or str(lang), k))
    return out


def sidecar_langs(path, dir_cache):
    """{language: [paths]} of sidecar subtitles next to a library file, e.g.
    'Show - S01E01.en.srt' -> 'en'. No language tag counts as 'und'."""
    if not path:
        return {}
    d, base = os.path.split(path)
    stem = os.path.splitext(base)[0]
    if d not in dir_cache:
        try:
            dir_cache[d] = os.listdir(d)
        except OSError:
            dir_cache[d] = []
    out = {}
    for f in dir_cache[d]:
        if not f.lower().endswith(SUB_EXTS) or not f.startswith(stem + '.'):
            continue
        middle = f[len(stem) + 1:].rsplit('.', 1)[0]
        lang, flags = sidecar_lang(middle)
        if 'forced' in flags or 'foreign' in flags:
            continue        # forced/foreign-only subtitles don't cover the language
        out.setdefault(lang or 'und', []).append(os.path.join(d, f))
    return out


def has_lang(found, wanted_key):
    """found: language keys/codes present; wanted_key from wanted_langs()."""
    return covers(wanted_key, found)


def label(item):
    t = item.get('title') or ''
    if item.get('type') == 'episode' and item.get('season_number') is not None:
        t += f" S{int(item['season_number']):02d}E{int(item.get('episode_number') or 0):02d}"
    elif item.get('year'):
        t += f" ({item['year']})"
    return t


def collected(q='', kind='all', full=False):
    """Collected movies/episodes, newest first, capped at ROW_CAP."""
    from database.core import get_db_connection
    where = ["state IN ('Collected','Upgrading')", "type IN ('movie','episode')"]
    args = []
    if kind in ('movie', 'episode'):
        where.append('type = ?')
        args.append(kind)
    if q:
        where.append('(title LIKE ? OR episode_title LIKE ?)')
        args += [f'%{q}%', f'%{q}%']
    cols = '*' if full else ('id, title, year, type, season_number, episode_number, '
                             'location_on_disk, collected_at, version, filled_by_file, ms_item_id, imdb_id')
    conn = get_db_connection()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM media_items WHERE " + ' AND '.join(where)
            + f" ORDER BY collected_at DESC LIMIT {ROW_CAP}", args).fetchall()]
    finally:
        conn.close()


def with_status(rows, plex=None, wanted=None):
    """rows + 'langs' [{lang, has}] and 'missing' (bool)."""
    from database import subtitle_history
    plex = is_plex_mode() if plex is None else plex
    wanted = wanted_langs() if wanted is None else wanted
    ids = [r['id'] for r in rows]
    # rows saved before the episode fix were keyed by ms_item_id: accept both
    history = subtitle_history.languages_by_item(
        ids, {r['id']: tuple(k for k in {subtitle_history.media_key_of(r), str(r.get('ms_item_id') or '')} if k)
              for r in rows})
    embedded = subtitle_history.embedded_items(ids)
    plex_tracks = {}
    from utilities.config.downsub_config import config as _cfg
    try:
        from utilities import plex_subtitle_info as psi
        if psi.available() and not getattr(_cfg, 'IGNORE_EMBEDDED', False):
            plex_tracks = psi.cached_tracks([psi.cache_key(r) for r in rows])
    except Exception:
        plex_tracks = {}
    dir_cache = {}
    out = []
    for r in rows:
        # sidecar files count in both modes: in Plex mode a writable library
        # folder (e.g. a symlink folder Bazarr wrote to) has them too
        found = sidecar_langs(r.get('location_on_disk'), dir_cache)
        for lang in history.get(r['id'], ()):
            found.setdefault(lang or '', [])
        emb = r['id'] in embedded and not getattr(_cfg, 'IGNORE_EMBEDDED', False)
        good = set()
        if plex_tracks:
            from utilities import plex_subtitle_info as psi
            good = psi.good_embedded_langs(plex_tracks.get(psi.cache_key(r)))
        langs = []
        for label, k in wanted:
            in_file = has_lang(found, k)
            in_track = emb or covers(k, good)
            langs.append({'lang': label, 'has': in_file or in_track, 'embedded': in_track and not in_file})
        out.append(dict(r, langs=langs, missing=any(not x['has'] for x in langs)))
    return out


def missing_items(q='', kind='all', skip_tried_hours=0, limit=None):
    """Full item rows missing at least one configured language. With
    skip_tried_hours, items searched that recently without a result are
    left out (the task uses this so it doesn't search the same items hourly)."""
    from database import subtitle_history
    rows = [r for r in with_status(collected(q, kind, full=True)) if r['missing']]
    if skip_tried_hours:
        recent = subtitle_history.recently_tried_without_result(skip_tried_hours)
        rows = [r for r in rows if r['id'] not in recent]
    return rows[:limit] if limit else rows
