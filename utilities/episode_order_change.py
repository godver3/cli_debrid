"""Switch a show to another episode order, moving its library rows with it.

preview_order_change() works out, without changing anything, where each of
the show's episode rows (every version) lands in the new order.
apply_order_change() then:

1. moves rows that map (season/episode/title), removes not-yet-grabbed rows
   whose episode doesn't exist in the new order, and leaves rows that have a
   file but can't be mapped untouched (listed in the result);
2. saves the order on the show and rebuilds cli_battery's seasons in it;
3. in symlink mode, renames the moved items' symlinks;
4. sets the show's episode ordering in Plex where Plex has the same order.

The map comes from fresh provider data for both orders (see
utilities/episode_order_remap.py); battery data isn't used for it because
older entries lack episode IDs.
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests

from database.core import get_db_connection
from utilities.missing_episodes import queue_missing_episodes
from utilities.settings import get_setting

# Rows in these states without a file are only a request for an episode:
# safe to drop when the episode is known not to exist in the new order.
REMOVABLE_STATES = ('Wanted', 'Unreleased', 'Sleeping', 'Blacklisted', 'Scraping')
# Plex's per-show ordering for orders it can express. The value names depend
# on the Plex agent (current: tvdbAiring/tvdbDvd/tvdbAbsolute/tmdbAiring,
# legacy: aired/dvd/absolute); the show's own preference list says which.
PLEX_SHOW_ORDERING = {
    'tvdb:official': ['tvdbAiring', 'aired'],
    'tvdb:dvd': ['tvdbDvd', 'dvd'],
    'tvdb:absolute': ['tvdbAbsolute', 'absolute'],
}
TMDB_DEFAULT_ORDERING = ['tmdbAiring']


def _show_rows(imdb_id: str) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, season_number, episode_number, version, state, episode_title, "
            "filled_by_file, location_on_disk, ms_item_id FROM media_items "
            "WHERE imdb_id = ? AND type = 'episode'", (imdb_id,))]
    finally:
        conn.close()


def _effective(order: Optional[str]) -> str:
    from cli_battery.app.episode_order import is_default_order, DEFAULT
    return DEFAULT if is_default_order(order) else order


def _plex_ordering_for(order: str) -> Optional[List[str]]:
    """Candidate Plex showOrdering values for an order, or None if Plex has none."""
    from cli_battery.app import tvdb_client
    if order == 'default':
        return PLEX_SHOW_ORDERING['tvdb:official'] if tvdb_client.is_available() else TMDB_DEFAULT_ORDERING
    return PLEX_SHOW_ORDERING.get(order)


def plex_ordering_value(show, candidates: List[str]) -> Optional[str]:
    """The first candidate this show's Plex agent accepts (None if none does)."""
    for pref in show.preferences():
        if pref.id == 'showOrdering':
            allowed = pref.enumValues or {}
            return next((c for c in candidates if c in allowed), None)
    return candidates[0] if candidates else None


def preview_order_change(imdb_id: str, new_order: Optional[str]) -> Dict[str, Any]:
    """Where every row would go. Nothing is changed."""
    from cli_battery.app.episode_order import fetch_seasons_for_order, show_episode_order
    from cli_battery.app import tvdb_client
    from utilities.episode_order_remap import plan_remap

    new_order = _effective(new_order)
    current_order = _effective(show_episode_order(imdb_id))
    if new_order == current_order:
        return {'success': False, 'error': 'The show already uses this episode order'}

    current = fetch_seasons_for_order(imdb_id, current_order)
    target = fetch_seasons_for_order(imdb_id, new_order)
    if not current or not target:
        return {'success': False, 'error': 'Could not fetch the episode list for '
                + ('the current order' if not current else 'the new order')}

    _add_stored_ids(imdb_id, current, current_order)
    # Trakt's numbering follows TMDB's default order, which TMDB groups refer to
    current_is_tmdb_default = current_order == 'default' and not tvdb_client.is_available()
    plan = plan_remap(current, target, current_is_tmdb_default=current_is_tmdb_default)
    if new_order.startswith('tmdb_group:') and tvdb_client.is_available() and plan['unmatched_target']:
        # TVDB numbering vs a TMDB group: TMDB knows each episode's TVDB ID, so
        # look those up for whatever air date/title couldn't place, and re-plan
        added = _add_group_tvdb_ids(imdb_id, target, plan['unmatched_target'])
        if added:
            plan = plan_remap(current, target, current_is_tmdb_default=current_is_tmdb_default)
    absent = set(plan['absent'])

    moves, removes, unmappable, unchanged = [], [], [], 0
    final_positions = defaultdict(list)  # (version, s, e) -> row ids
    for row in _show_rows(imdb_id):
        old = (row['season_number'], row['episode_number'])
        new = plan['map'].get(old)
        has_file = bool(row['filled_by_file'] or row['location_on_disk'])
        entry = {'id': row['id'], 'version': row['version'], 'state': row['state'],
                 'old': list(old), 'title': row['episode_title']}
        if new is None:
            if row['state'] in REMOVABLE_STATES and not has_file and old in absent:
                removes.append(entry)
                continue
            unmappable.append(entry)
            final_positions[(row['version'],) + old].append(row['id'])
            continue
        final_positions[(row['version'],) + new].append(row['id'])
        if new == old:
            unchanged += 1
            continue
        entry.update(new=list(new), new_title=plan['titles'].get(new) or row['episode_title'],
                     has_file=has_file)
        moves.append(entry)

    conflicts = [{'version': key[0], 'season': key[1], 'episode': key[2], 'row_ids': ids}
                 for key, ids in final_positions.items() if len(ids) > 1]
    missing = _missing_after_change(target, {(key[1], key[2]) for key in final_positions})
    plex_ordering = _plex_ordering_for(new_order)
    return {
        'success': True,
        'imdb_id': imdb_id,
        'current_order': current_order,
        'new_order': new_order,
        'moves': sorted(moves, key=lambda m: (m['old'], m['version'] or '')),
        'removes': removes,
        'unmappable': unmappable,
        'unchanged': unchanged,
        'conflicts': conflicts,
        'new_episodes': len(plan['unmatched_target']),
        'missing_episodes': len(missing),
        'missing': missing[:MISSING_LISTED],
        'missing_positions': [[m['season'], m['episode']] for m in missing],
        'joined_by': plan['joined_by'],
        'plex_ordering': plex_ordering,
        'plex_note': None if plex_ordering else
            'Plex has no matching episode ordering for this order; set the show\'s ordering in Plex '
            'by hand if its episode numbers don\'t line up.',
    }


MISSING_LISTED = 50


def _missing_after_change(target: Dict[Any, Any], occupied) -> List[Dict[str, Any]]:
    """Regular-season episodes of the new order no library entry will be on
    (Dragon Ball Kai aired -> DVD: the episodes only the DVD order has, plus
    any the library never had). Specials aren't counted."""
    missing = []
    for s_num, s_data in (target or {}).items():
        if not isinstance(s_num, int) or s_num <= 0 or not isinstance(s_data, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            if not isinstance(e_num, int) or (s_num, e_num) in occupied:
                continue
            ep = ep if isinstance(ep, dict) else {}
            missing.append({'season': s_num, 'episode': e_num, 'title': ep.get('title') or '',
                            'aired': (ep.get('first_aired') or '')[:10] or None})
    return sorted(missing, key=lambda m: (m['season'], m['episode']))


def _add_group_tvdb_ids(imdb_id: str, target: Dict[Any, Any], positions) -> int:
    try:
        from cli_battery.app.direct_api import DirectAPI
        from cli_battery.app.tmdb_episode_groups import add_tvdb_ids
        metadata, _ = DirectAPI.get_show_metadata(imdb_id)
        tmdb_id = ((metadata or {}).get('ids') or {}).get('tmdb')
        if not tmdb_id:
            return 0
        added = add_tvdb_ids(tmdb_id, target, positions)
        logging.info(f"[EpisodeOrder] {imdb_id}: added TVDB IDs to {added} of {len(positions)} unplaced group episodes")
        return added
    except Exception as e:
        logging.warning(f"[EpisodeOrder] TVDB ID lookup for {imdb_id}'s episode group failed: {e}")
        return 0


def _add_stored_ids(imdb_id: str, current: Dict[Any, Any], current_order: str) -> None:
    """Fill in episode IDs cli_battery already has for the current order.

    TVDB's episode lists carry no TMDB IDs, but the battery's stored episodes
    often do (Trakt supplies them), and those let a TMDB episode group join on
    IDs instead of air date and title. The stored seasons are in the current
    order, so they line up by season/episode.
    """
    try:
        from cli_battery.app.direct_api import DirectAPI
        stored, _ = DirectAPI.get_show_seasons(imdb_id)
    except Exception as e:
        logging.debug(f"[EpisodeOrder] No stored seasons for {imdb_id}: {e}")
        return
    for s_num, s_data in (stored or {}).items():
        current_season = current.get(s_num) if isinstance(current, dict) else None
        if not isinstance(s_data, dict) or not isinstance(current_season, dict):
            continue
        for e_num, ep in (s_data.get('episodes') or {}).items():
            target = (current_season.get('episodes') or {}).get(e_num)
            if not isinstance(target, dict) or not isinstance(ep, dict):
                continue
            for key in ('tmdb_id', 'tvdb_id'):
                if ep.get(key) and not target.get(key):
                    target[key] = ep[key]


def _apply_rows(preview: Dict[str, Any]) -> None:
    conn = get_db_connection()
    try:
        conn.execute('BEGIN')
        for move in preview['moves']:
            conn.execute(
                "UPDATE media_items SET season_number = ?, episode_number = ?, episode_title = ? WHERE id = ?",
                (move['new'][0], move['new'][1], move['new_title'], move['id']))
        for remove in preview['removes']:
            conn.execute("DELETE FROM media_items WHERE id = ?", (remove['id'],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _reset_battery_seasons(imdb_id: str) -> None:
    """Drop the show's stored seasons so the refresh rebuilds them in the new
    order (stored seasons are only ever upserted, never pruned)."""
    from cli_battery.app.database import Episode, Item, Season, managed_session
    with managed_session() as session:
        item = session.query(Item).filter_by(imdb_id=imdb_id).first()
        if not item:
            return
        season_ids = [s.id for s in session.query(Season.id).filter_by(item_id=item.id)]
        if season_ids:
            session.query(Episode).filter(Episode.season_id.in_(season_ids)).delete(synchronize_session=False)
            session.query(Season).filter(Season.id.in_(season_ids)).delete(synchronize_session=False)


def _plex_server():
    import plexapi.server
    if get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        url = get_setting('File Management', 'plex_url_for_symlink', default='')
        token = get_setting('File Management', 'plex_token_for_symlink', default='')
    else:
        url = get_setting('Plex', 'url', default='')
        token = get_setting('Plex', 'token', default='')
    if not url or not token:
        return None
    return plexapi.server.PlexServer(url.rstrip('/'), token, timeout=30)


def sync_plex_ordering(imdb_id: str, candidates: List[str], rating_keys: List[str]) -> str:
    """Set the show's episode ordering in Plex and refresh it. Returns a note."""
    try:
        plex = _plex_server()
        if plex is None:
            return 'Plex is not configured; ordering not changed there'
        show = None
        for key in rating_keys:
            try:
                item = plex.fetchItem(int(key))
                show = item.show() if item.type == 'episode' else item if item.type == 'show' else None
                if show is not None:
                    break
            except Exception:
                continue
        if show is None:
            for section in plex.library.sections():
                if section.type != 'show':
                    continue
                try:
                    show = section.getGuid(f"imdb://{imdb_id}")
                    break
                except Exception:
                    continue
        if show is None:
            return 'Show not found in Plex; set its episode ordering there by hand'
        ordering = plex_ordering_value(show, candidates)
        if not ordering:
            return f"Plex doesn't offer this ordering for '{show.title}' ({', '.join(candidates)}); set it there by hand"
        show.editAdvanced(showOrdering=ordering)
        logging.info(f"[EpisodeOrder] Plex '{show.title}' episode ordering set to {ordering}")
        # Plex can hold the refresh request open while it re-reads every episode
        # in the new order (Dragon Ball Kai: over 30s). The ordering is already
        # saved and the refresh carries on in Plex, so a slow reply isn't a failure.
        try:
            show.refresh()
        except requests.exceptions.Timeout:
            logging.info(f"[EpisodeOrder] Plex is still refreshing '{show.title}' after the ordering change")
            return (f"Plex episode ordering set to '{ordering}'; Plex is still refreshing the show "
                    "and will show the new order within a few minutes")
        return f"Plex episode ordering set to '{ordering}' and the show refreshed"
    except Exception as e:
        logging.error(f"[EpisodeOrder] Setting Plex ordering for {imdb_id} failed: {e}", exc_info=True)
        return f'Could not set the ordering in Plex: {e}'


def apply_order_change(imdb_id: str, new_order: Optional[str], sync_plex: bool = True,
                       add_missing: bool = False) -> Dict[str, Any]:
    from cli_battery.app.direct_api import DirectAPI
    from database.show_overrides import set_show_override

    preview = preview_order_change(imdb_id, new_order)
    if not preview.get('success'):
        return preview
    if preview['conflicts']:
        return dict(preview, success=False,
                    error='Some episodes would land on the same season/episode; nothing was changed')

    _apply_rows(preview)
    new_order = preview['new_order']
    set_show_override(imdb_id, episode_order=None if new_order == 'default' else new_order)
    _reset_battery_seasons(imdb_id)
    metadata, _ = DirectAPI.force_refresh_metadata(imdb_id, item_type='show')
    refreshed = bool(metadata)

    symlink_note = None
    moved_with_files = [m['id'] for m in preview['moves'] if m.get('has_file')]
    if moved_with_files and get_setting('File Management', 'file_collection_management') == 'Symlinked/Local':
        from utilities.local_library_scan import resync_symlinks_with_new_settings
        result = resync_symlinks_with_new_settings(item_ids=moved_with_files)
        symlink_note = (f"Symlinks: {result.get('symlinks_updated_count', 0)} renamed, "
                        f"{result.get('symlinks_created_count', 0)} created, "
                        f"{result.get('error_count', 0)} errors") if isinstance(result, dict) else None

    # After the metadata rebuild, so the episodes are read in the new order
    missing_added = None
    if add_missing and preview['missing_episodes']:
        try:
            missing_added = queue_missing_episodes(imdb_id, 'Episode-Order', preview['missing_positions'])
        except Exception as e:
            logging.error(f"[EpisodeOrder] Queueing missing episodes for {imdb_id} failed: {e}", exc_info=True)
            missing_added = None

    plex_note = preview['plex_note']
    if sync_plex and preview['plex_ordering']:
        rating_keys = [str(r['ms_item_id']) for r in _show_rows(imdb_id) if r.get('ms_item_id')]
        plex_note = sync_plex_ordering(imdb_id, preview['plex_ordering'], rating_keys)

    logging.info(f"[EpisodeOrder] {imdb_id}: {preview['current_order']} -> {new_order}; "
                 f"{len(preview['moves'])} moved, {len(preview['removes'])} removed, "
                 f"{len(preview['unmappable'])} left unmapped")
    return dict(preview, success=True, applied=True, metadata_refreshed=refreshed,
                symlink_note=symlink_note, plex_note=plex_note,
                missing_added=missing_added, missing_requested=bool(add_missing and preview['missing_episodes']))
