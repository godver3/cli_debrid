"""
Keep a show's chosen episode order in Plex after Plex drops TheTVDB
(2026-12-03).

Plex numbers shows by TMDB from then on and can no longer follow a TVDB
DVD/absolute/alternate order. For a show whose order was picked by hand in
cli_debrid (Show options, or manual assign with a non-default order), the
symlinks already carry that order's season/episode numbers, so Plex sorts
the episodes that way. This writes the matching title, summary and air date
onto each Plex episode and locks those fields, so Plex's TMDB metadata
can't overwrite them. Shows on the default order are never touched; going
back to the default unlocks the fields and Plex's own metadata returns.

Runs only when an order is changed (no scheduled task): episodes added to
Plex later keep Plex's own metadata until the order is set again.
"""

import logging
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


def _wanted(seasons: Dict) -> Dict[Tuple[int, int], Dict]:
    """(season, episode) -> {'title', 'summary', 'date'} from an order's seasons."""
    out = {}
    for sn, season in (seasons or {}).items():
        for en, ep in ((season or {}).get('episodes') or {}).items():
            try:
                key = (int(sn), int(en))
            except (TypeError, ValueError):
                continue
            out[key] = {'title': (ep.get('title') or '').strip(),
                        'summary': (ep.get('overview') or '').strip(),
                        'date': (ep.get('first_aired') or '')[:10]}
    return out


def _locked(ep) -> set:
    return {f.name for f in getattr(ep, 'fields', None) or [] if getattr(f, 'locked', False)}


def _date(ep) -> str:
    d = getattr(ep, 'originallyAvailableAt', None)
    return d.strftime('%Y-%m-%d') if d else ''


def apply_to_plex_show(show, seasons: Dict) -> Dict[str, int]:
    """Write the order's metadata onto the Plex show's episodes. Episodes
    already right (and locked) are skipped."""
    want = _wanted(seasons)
    counts = {'updated': 0, 'unchanged': 0, 'not_in_order': 0, 'errors': 0}
    for ep in show.episodes():
        key = (getattr(ep, 'seasonNumber', None), getattr(ep, 'index', None))
        meta = want.get(key) if None not in key else None
        if meta is None or not meta['title']:
            counts['not_in_order'] += 1
            continue
        locked = _locked(ep)
        todo = []
        if (ep.title or '') != meta['title'] or 'title' not in locked:
            todo.append('title')
        if meta['summary'] and ((ep.summary or '') != meta['summary'] or 'summary' not in locked):
            todo.append('summary')
        if meta['date'] and (_date(ep) != meta['date'] or 'originallyAvailableAt' not in locked):
            todo.append('originallyAvailableAt')
        if not todo:
            counts['unchanged'] += 1
            continue
        try:
            ep.batchEdits()
            if 'title' in todo:
                ep.editTitle(meta['title'], locked=True)
            if 'summary' in todo:
                ep.editSummary(meta['summary'], locked=True)
            if 'originallyAvailableAt' in todo:
                ep.editOriginallyAvailable(meta['date'], locked=True)
            ep.saveEdits()
            counts['updated'] += 1
        except Exception as e:
            counts['errors'] += 1
            logging.warning(f"{_LOG} {show.title} S{key[0]:02d}E{key[1]:02d}: edit failed: {e}")
    return counts


def release_plex_show(show) -> int:
    """Unlock the fields this module locked and let Plex refresh them."""
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
    """This show's order is carried by NFO files (Tools -> NFO Files)."""
    try:
        from utilities.nfo_writer import wants_show
        return wants_show(imdb_id)
    except Exception:
        return False


def release_for_nfo(imdb_id: str, plex=None) -> str:
    """NFO files carry the order's episode details: drop the locks this module
    set (they would stop Plex from taking the NFO's values) and have Plex
    re-read the show."""
    show, problem = _plex_show(imdb_id, plex)
    if show is None:
        return ''
    n = release_plex_show(show)  # refreshes the show when it unlocked something
    if not n:
        try:
            show.refresh()
        except Exception as e:
            logging.debug(f"{_LOG} {show.title}: refresh: {e}")
    return f"Plex re-reading {show.title}'s NFO files" + (f" ({n} episode locks released)" if n else '')


def release_all_for_nfo(plex=None) -> int:
    """Every show with a chosen order: release the locks (turning NFO files on)."""
    plex = plex or __import__('utilities.episode_order_change', fromlist=['_plex_server'])._plex_server()
    if plex is None:
        return 0
    done = 0
    for imdb_id, _order in shows_with_chosen_order():
        try:
            if release_for_nfo(imdb_id, plex):
                done += 1
        except Exception as e:
            logging.warning(f"{_LOG} releasing {imdb_id} for NFO: {e}")
    return done


def lock_new_episodes(imdb_ids) -> Dict[str, str]:
    """Plex just added episodes of these shows (symlink verification found
    them). For a show on a chosen order whose order isn't carried by NFO
    files, set and lock the new episodes' titles too: without this only the
    episodes present at the order change were locked, and every later one
    showed Plex's (TMDB-numbered) details. Episodes already right are skipped."""
    from cli_battery.app.episode_order import show_episode_order
    notes = {}
    for imdb_id in sorted({i for i in imdb_ids or () if i}):
        try:
            order = show_episode_order(imdb_id)
            if not order or _nfo_mode(imdb_id):
                continue
            notes[imdb_id] = sync_show(imdb_id, order)
        except Exception as e:
            logging.warning(f"{_LOG} locking new episodes of {imdb_id}: {e}")
    return notes


def sync_show(imdb_id: str, order: Optional[str] = None, plex=None) -> str:
    """Apply (chosen order) or release (default order) one show. Returns a note.
    With NFO files on, the NFOs carry the order instead: release and refresh."""
    from cli_battery.app.episode_order import fetch_seasons_for_order, show_episode_order
    order = order if order is not None else show_episode_order(imdb_id)
    try:
        if _nfo_mode(imdb_id):
            return release_for_nfo(imdb_id, plex)
        show, problem = _plex_show(imdb_id, plex)
        if show is None:
            return f'Plex episode titles not updated: {problem}'
        if not order or order == 'default':
            n = release_plex_show(show)
            return f"Plex episode titles unlocked for {n} episodes" if n else ''
        seasons = fetch_seasons_for_order(imdb_id, order)
        if not seasons:
            return f'Plex episode titles not updated: could not fetch the {order} episode list'
        c = apply_to_plex_show(show, seasons)
        if c['updated'] or c['errors']:
            logging.info(f"{_LOG} {show.title} ({order}): {c}")
        return (f"Plex episode titles set from {order}: {c['updated']} updated, {c['unchanged']} already right"
                + (f", {c['errors']} failed" if c['errors'] else ''))
    except Exception as e:
        logging.error(f"{_LOG} {imdb_id}: {e}", exc_info=True)
        return f'Plex episode titles not updated: {e}'
