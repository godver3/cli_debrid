"""Shared helpers for tracker content sources (Floppy, Simkl).

They reduce a tracker's entries to the {'imdb_id', 'media_type'} items the
add_wanted_items pipeline expects, with the same blacklist/ghostlist gating the
Trakt and Scrob sources apply.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional


def tmdb_to_imdb(tmdb_id: Any, media_type: str) -> Optional[str]:
    """TMDB id -> IMDb id through the metadata battery. media_type: 'movie' or 'tv'."""
    if not tmdb_id:
        return None
    try:
        from cli_battery.app.direct_api import DirectAPI
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type=media_type)
        return imdb_id
    except Exception as e:
        logging.debug(f"Could not resolve tmdb_id {tmdb_id} ({media_type}) to imdb_id: {e}")
        return None


def tvdb_to_imdb(tvdb_id: Any, media_type: str) -> Optional[str]:
    if not tvdb_id:
        return None
    try:
        from cli_battery.app.tvdb_client import convert_tvdb_to_imdb
        return convert_tvdb_to_imdb(str(tvdb_id), media_type='movie' if media_type == 'movie' else 'show')
    except Exception as e:
        logging.debug(f"Could not resolve tvdb_id {tvdb_id} ({media_type}) to imdb_id: {e}")
        return None


def resolve_imdb(ids: Dict[str, Any], media_type: str) -> Optional[str]:
    """IMDb id from an ids dict: imdb, else tmdb, else tvdb."""
    imdb = str(ids.get('imdb') or '').strip()
    if imdb.startswith('tt'):
        return imdb
    return tmdb_to_imdb(ids.get('tmdb'), media_type) or tvdb_to_imdb(ids.get('tvdb'), media_type)


def gate_and_merge(entries: Iterable[Dict[str, Any]], source_name: str,
                   unblacklist: bool = False) -> List[Dict[str, Any]]:
    """entries: dicts with 'imdb_id', 'media_type' ('movie'|'tv') and optional 'season'.

    Drops ghostlisted items and blacklisted ones (unless unblacklist), merges
    duplicates, and turns season-only entries into 'requested_seasons' (a
    whole-show entry for the same show wins).
    """
    from database.core import get_db_connection

    out: List[Dict[str, Any]] = []
    by_imdb: Dict[str, Dict[str, Any]] = {}
    seasons: Dict[str, set] = {}
    whole: set = set()
    blocked = 0
    entries = list(entries)
    ids = sorted({e['imdb_id'] for e in entries})
    states: Dict[str, tuple] = {}
    if ids:
        try:
            conn = get_db_connection()
            try:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    rows = conn.execute(
                        f"SELECT imdb_id, title, state, ghostlisted FROM media_items "
                        f"WHERE imdb_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                    for row in rows:
                        prev = states.get(row[0])
                        # Any ghostlisted/blacklisted row of the title counts.
                        flag = (row[1], row[2] == 'Blacklisted', row[3] == 1)
                        if not prev or flag[1] or flag[2]:
                            states[row[0]] = flag
            finally:
                conn.close()
        except Exception as e:
            logging.error(f"[{source_name}] Could not check blacklist state: {e}")

    for e in entries:
        imdb_id, media_type, season = e['imdb_id'], e['media_type'], e.get('season')
        state = states.get(imdb_id)
        if state:
            title, blacklisted, ghostlisted = state
            if ghostlisted or (blacklisted and not unblacklist):
                if imdb_id not in by_imdb:
                    blocked += 1
                    logging.info(f"⛔ BLOCKED: Skipping {'ghostlisted' if ghostlisted else 'blacklisted'} "
                                 f"item from {source_name}: {title} (IMDB: {imdb_id})")
                    by_imdb[imdb_id] = None
                continue
        if imdb_id in by_imdb:
            if by_imdb[imdb_id] is not None and media_type == 'tv':
                if season is None:
                    whole.add(imdb_id)
                else:
                    seasons[imdb_id].add(season)
            continue
        item = {'imdb_id': imdb_id, 'media_type': media_type}
        by_imdb[imdb_id] = item
        out.append(item)
        if media_type == 'tv':
            seasons[imdb_id] = set()
            if season is None:
                whole.add(imdb_id)
            else:
                seasons[imdb_id].add(season)

    for imdb_id, wanted in seasons.items():
        if imdb_id not in whole and wanted:
            by_imdb[imdb_id]['requested_seasons'] = sorted(wanted)
    if blocked:
        logging.info(f"⛔ Blocked {blocked} blacklisted/ghostlisted items from {source_name}")
    return out
