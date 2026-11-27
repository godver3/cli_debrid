"""
Plex-side episode metadata for shows with a chosen episode order.

cli_debrid no longer writes and locks a chosen order's titles onto Plex
episodes (0.8.0-0.8.6 did, on order change). Without NFO files Plex shows
its own metadata; with NFO files (Tools -> NFO Files) those carry the order.
What's left: releasing the locks those versions set (on the show's next
order change, or when NFO files take a show over) and numbering NFO shows
by TMDB in Plex. Nothing here runs on a schedule.
"""

import json
import logging
import os
import threading
from typing import Dict, List, Optional, Tuple

_LOG = '[PlexOrderMetadata]'
FIELDS = ('title', 'summary', 'originallyAvailableAt')


def shows_with_chosen_order() -> List[Tuple[str, str]]:
    """(imdb_id, order) of every show with a non-default order."""
    from database.core import get_db_connection
    from database.show_overrides import ensure_show_overrides_table
    conn = get_db_connection()
    try:
        ensure_show_overrides_table(conn)
        rows = conn.execute("SELECT imdb_id, episode_order FROM show_overrides "
                            "WHERE episode_order IS NOT NULL AND episode_order != '' "
                            "AND episode_order != 'default'").fetchall()
        return [(r[0], r[1]) for r in rows if r[0]]
    finally:
        conn.close()


def _locked(ep) -> set:
    return {f.name for f in getattr(ep, 'fields', None) or [] if getattr(f, 'locked', False)}


def release_plex_show(show) -> int:
    """Unlock the title/summary/date locks 0.8.0-0.8.6 set and let Plex
    refresh them."""
    n = 0
    for ep in show.episodes():
        locked = _locked(ep) & set(FIELDS)
        if not locked:
            continue
        try:
            ep.edit(**{f'{f}.locked': 0 for f in locked})
            n += 1
        except Exception as e:
            logging.warning(f"{_LOG} {show.title}: unlock failed for {ep.title!r}: {e}")
    if n:
        try:
            show.refresh()
        except Exception as e:
            logging.debug(f"{_LOG} {show.title}: refresh after unlock: {e}")
    return n


def _plex_show(imdb_id: str, plex=None):
    from utilities.episode_order_change import _plex_server, _show_rows, find_plex_show
    plex = plex or _plex_server()
    if plex is None:
        return None, 'Plex is not configured'
    keys = [str(r['ms_item_id']) for r in _show_rows(imdb_id) if r.get('ms_item_id')]
    show = find_plex_show(plex, imdb_id, keys)
    return show, None if show is not None else 'show not in Plex yet'


def _nfo_mode(imdb_id: str) -> bool:
    """This show's order is carried by NFO files (Tools -> NFO Files) that
    Plex reads: files written while the library keeps its own agent change
    nothing in Plex."""
    try:
        from utilities.nfo_writer import plex_reads_nfos, wants_show
        return wants_show(imdb_id) and plex_reads_nfos()
    except Exception:
        return False


TMDB_AIRING = 'tmdbAiring'


# Shows Clyde switched to TMDB ordering, with the ordering they had before,
# so turning NFO files off (or a show leaving them) can put it back:
# {rating key: {'previous': value, 'imdb_id': ..., 'title': ...}}
_pins_lock = threading.Lock()


def _pins_path() -> str:
    from utilities.settings import get_config_dir
    return os.path.join(get_config_dir(), 'plex_order_pins.json')


def _load_pins() -> Dict[str, Dict]:
    try:
        with open(_pins_path(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pins(pins: Dict[str, Dict]) -> None:
    path = _pins_path()
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(pins, f, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning(f"{_LOG} could not save {path}: {e}")


def _show_ordering_pref(show):
    return next((p for p in show.preferences() if p.id == 'showOrdering'), None)


def pin_tmdb_ordering(show, imdb_id: Optional[str] = None) -> bool:
    """A show whose order the NFO files carry is numbered by TMDB (aired) in
    Plex: the numbering the NFOs are written against, and the only one Plex
    has for every show once it drops TheTVDB (2026-12-03). Before that a
    library or show ordering of TheTVDB would number it differently. True
    when the ordering was changed; the previous one is recorded so
    unpin_show can put it back."""
    try:
        pref = _show_ordering_pref(show)
        if pref is None or TMDB_AIRING not in (pref.enumValues or {}) or pref.value == TMDB_AIRING:
            return False
        key = str(getattr(show, 'ratingKey', '') or '')
        if key:
            with _pins_lock:
                pins = _load_pins()
                # A show pinned twice keeps the ordering it had before the first pin
                pins.setdefault(key, {'previous': pref.value or '', 'imdb_id': imdb_id,
                                      'title': getattr(show, 'title', '')})
                _save_pins(pins)
        show.editAdvanced(showOrdering=TMDB_AIRING)
        logging.info(f"{_LOG} {show.title}: Plex episode ordering set to {TMDB_AIRING} (NFO files carry the order)")
        return True
    except Exception as e:
        logging.warning(f"{_LOG} {getattr(show, 'title', '?')}: could not set TMDB ordering: {e}")
        return False


def release_for_nfo(imdb_id: str, plex=None) -> str:
    """NFO files carry the order's episode details: number the show by TMDB
    in Plex, drop the locks this module set (they would stop Plex from taking
    the NFO's values) and have Plex re-read the show."""
    show, problem = _plex_show(imdb_id, plex)
    if show is None:
        return ''
    pinned = pin_tmdb_ordering(show, imdb_id)
    n = release_plex_show(show)  # refreshes the show when it unlocked something
    if not n:
        try:
            show.refresh()
        except Exception as e:
            logging.debug(f"{_LOG} {show.title}: refresh: {e}")
    return (f"Plex re-reading {show.title}'s NFO files" + (f" ({n} episode locks released)" if n else '')
            + ('; numbered by TMDB in Plex' if pinned else ''))


def release_all_for_nfo(plex=None) -> int:
    """Every show with a chosen order whose NFO files Clyde writes: number it
    by TMDB and release the locks (turning NFO files on, or switching Plex to
    the NFO agent). Shows without NFO files (NFO mode off, or a per-show
    "never") are left as they are: TMDB numbering without NFOs would show
    them in TMDB's order, not the chosen one."""
    from utilities.nfo_writer import wants_show
    plex = plex or __import__('utilities.episode_order_change', fromlist=['_plex_server'])._plex_server()
    if plex is None:
        return 0
    done = 0
    for imdb_id, _order in shows_with_chosen_order():
        if not wants_show(imdb_id):
            continue
        try:
            if release_for_nfo(imdb_id, plex):
                done += 1
        except Exception as e:
            logging.warning(f"{_LOG} releasing {imdb_id} for NFO: {e}")
    return done


def pin_new_nfo_shows(imdb_ids) -> Dict[str, str]:
    """Plex just added episodes of these shows (symlink verification found
    them). A show whose order NFO files carry and that is new to Plex takes
    the library's ordering (TheTVDB before 2026-12-03): number it by TMDB
    like the NFOs expect. Shows without NFO files are left alone."""
    notes = {}
    for imdb_id in sorted({i for i in imdb_ids or () if i}):
        try:
            if not _nfo_mode(imdb_id):
                continue
            show, _problem = _plex_show(imdb_id)
            if show is not None and pin_tmdb_ordering(show, imdb_id):
                show.refresh()
                notes[imdb_id] = 'numbered by TMDB in Plex'
        except Exception as e:
            logging.warning(f"{_LOG} numbering new show {imdb_id} by TMDB: {e}")
    return notes


def sync_show(imdb_id: str, order: Optional[str] = None, plex=None) -> str:
    """After a show's order changed. With NFO files the NFOs carry the order:
    number the show by TMDB, release old locks and refresh. Without, only
    release the locks 0.8.0-0.8.6 set, so Plex's own metadata returns.
    Returns a note ('' when there was nothing to do)."""
    try:
        if _nfo_mode(imdb_id):
            return release_for_nfo(imdb_id, plex)
        show, _problem = _plex_show(imdb_id, plex)
        if show is None:
            return ''
        n = release_plex_show(show)
        # No longer carried by NFO files: the TMDB numbering Clyde set for them goes
        unpinned = unpin_show(show)
        notes = []
        if n:
            notes.append(f"Plex episode titles unlocked for {n} episodes")
        if unpinned:
            notes.append(f"Plex episode ordering put back to {unpinned}")
        return '; '.join(notes)
    except Exception as e:
        logging.error(f"{_LOG} {imdb_id}: {e}", exc_info=True)
        return f'Plex episode titles not updated: {e}'


def unpin_show(show) -> str:
    """Put back the ordering a show had before pin_tmdb_ordering, if Clyde
    pinned it and it is still on TMDB ordering (an ordering set since, by
    the user or an order change, is kept). Returns the label of the ordering
    put back, '' when nothing changed."""
    key = str(getattr(show, 'ratingKey', '') or '')
    if not key:
        return ''
    with _pins_lock:
        pins = _load_pins()
        rec = pins.pop(key, None)
        if rec is None:
            return ''
        _save_pins(pins)
    try:
        pref = _show_ordering_pref(show)
        previous = rec.get('previous') or ''
        enum = (pref.enumValues or {}) if pref is not None else {}
        if pref is None or pref.value != TMDB_AIRING or previous not in enum:
            return ''
        show.editAdvanced(showOrdering=previous)
        label = enum.get(previous) or previous or 'library default'
        logging.info(f"{_LOG} {show.title}: Plex episode ordering put back to {label} (no NFO files for it now)")
        return label
    except Exception as e:
        logging.warning(f"{_LOG} {getattr(show, 'title', '?')}: could not put the episode ordering back: {e}")
        return ''


def unpin_imdb(imdb_id: str, plex=None) -> str:
    """unpin_show for a show Clyde pinned, by its IMDb id (no Plex call when
    it never was)."""
    if not imdb_id or not any(r.get('imdb_id') == imdb_id for r in _load_pins().values()):
        return ''
    show, _problem = _plex_show(imdb_id, plex)
    return unpin_show(show) if show is not None else ''


def unpin_all(plex=None) -> int:
    """Every show Clyde switched to TMDB ordering goes back to the ordering it
    had (NFO files turned off, or Plex's previous agent restored). Returns how
    many were put back."""
    pins = _load_pins()
    if not pins:
        return 0
    plex = plex or __import__('utilities.episode_order_change', fromlist=['_plex_server'])._plex_server()
    if plex is None:
        return 0
    done = 0
    for key in list(pins):
        from plexapi.exceptions import NotFound
        try:
            show = plex.fetchItem(int(key))
        except Exception as e:
            if not isinstance(e, (NotFound, ValueError)):
                # Plex unreachable or timing out: keep the record for next time
                logging.warning(f"{_LOG} could not look up pinned show {key}: {e}")
                continue
            # Gone from Plex: nothing to put back
            with _pins_lock:
                current = _load_pins()
                current.pop(key, None)
                _save_pins(current)
            continue
        if unpin_show(show):
            done += 1
    return done
