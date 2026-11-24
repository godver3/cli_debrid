"""Fix Match: search, preview and apply a corrected match."""

from flask import jsonify, request
from ..models import admin_required
from database.core import get_db_connection
from utilities.settings import get_setting
import logging
from datetime import datetime
from ._common import library_bp
from .movie_metadata import refresh_movie_metadata
from .show_detail import refresh_show_metadata

# =============================================================================
# Fix Match — correct a library entry's imdb/tmdb/tvdb IDs in one action
# =============================================================================
#
# The "Fix Missing IMDb ID" debug function only rewrites media_items.imdb_id.
# When an entry is matched to the wrong title all three provider IDs are wrong,
# and the stale ones live in four places: media_items, the battery Item, the
# battery's TMDB/TVDB->IMDb mapping caches, and the media server's own match.
# Everything hanging off those IDs -- episode titles, air dates, genres, poster
# -- is wrong too.  These endpoints correct all of it from a single
# confirmation.

FIX_MATCH_SHOW_TYPES = ('episode', 'show')

FIX_MATCH_MOVIE_TYPES = ('movie',)

def _fix_match_types(media_type):
    """media_items.type values that make up one library entry of this kind."""
    return FIX_MATCH_SHOW_TYPES if media_type == 'show' else FIX_MATCH_MOVIE_TYPES

def _fix_match_row_filter(media_type, imdb_id, tmdb_id):
    """
    Build the WHERE clause selecting every media_items row of one library entry.

    Mirrors how show_detail_data/movie_detail_data group rows: an entry is all
    rows sharing either provider ID, so a half-corrected entry (right IMDb,
    wrong TMDB or vice versa) is still picked up whole.

    Returns (sql, params) or (None, None) when neither ID was supplied.
    """
    id_clauses, params = [], []
    if imdb_id:
        id_clauses.append('imdb_id = ?')
        params.append(str(imdb_id))
    if tmdb_id:
        id_clauses.append('tmdb_id = ?')
        params.append(str(tmdb_id))
    if not id_clauses:
        return None, None

    types = _fix_match_types(media_type)
    placeholders = ', '.join('?' for _ in types)
    sql = f"type IN ({placeholders}) AND ({' OR '.join(id_clauses)})"
    return sql, list(types) + params

def _fix_match_normalize_ids(metadata):
    """Pull imdb/tmdb/tvdb/slug out of a battery metadata dict's `ids` blob."""
    import json as _json

    ids = (metadata or {}).get('ids') or {}
    if isinstance(ids, str):
        try:
            ids = _json.loads(ids)
        except (ValueError, TypeError):
            ids = {}
    if not isinstance(ids, dict):
        ids = {}

    def _clean(value):
        text = str(value).strip() if value is not None else ''
        return text or None

    return {
        'imdb_id': _clean(ids.get('imdb')),
        'tmdb_id': _clean(ids.get('tmdb')),
        'tvdb_id': _clean(ids.get('tvdb')),
        'slug': _clean(ids.get('slug')),
    }

@library_bp.route('/fix_match/search', methods=['POST'])
@admin_required
def fix_match_search():
    """Search the metadata provider for the title this entry should be matched to."""
    data = request.get_json(silent=True) or {}
    query = (data.get('query') or '').strip()
    media_type = data.get('media_type') if data.get('media_type') in ('show', 'movie') else None

    if not query:
        return jsonify({'success': False, 'error': 'Enter a title to search for'}), 400

    year = data.get('year')
    try:
        year = int(year) if year not in (None, '') else None
    except (TypeError, ValueError):
        year = None

    try:
        from cli_battery.app.direct_api import DirectAPI
        results, source = DirectAPI.search_media(query=query, year=year, media_type=media_type)
        if results is None:
            return jsonify({'success': False,
                            'error': 'Metadata provider search failed (it may be rate limited) - '
                                     'you can still paste an IMDb or TMDB ID directly'}), 502

        cleaned = []
        for result in results[:20]:
            cleaned.append({
                'title': result.get('title'),
                'year': result.get('year'),
                'imdb_id': result.get('imdb_id'),
                'tmdb_id': str(result.get('tmdb_id')) if result.get('tmdb_id') else None,
                'type': result.get('type'),
            })
        return jsonify({'success': True, 'results': cleaned, 'source': source})
    except Exception as e:
        logging.error(f"[FixMatch] search '{query}' failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

@library_bp.route('/fix_match/preview', methods=['POST'])
@admin_required
def fix_match_preview():
    """
    Resolve a candidate to its full provider ID set and report the blast radius.

    Only the IMDb ID needs to be known - TMDB and TVDB both come out of the
    battery metadata fetch, which is what makes this a one-click fix for all
    three. A TMDB ID is accepted too and converted first.
    """
    import re as _re

    data = request.get_json(silent=True) or {}
    media_type = data.get('media_type') if data.get('media_type') in ('show', 'movie') else 'movie'
    imdb_id = (data.get('imdb_id') or '').strip()
    tmdb_id = str(data.get('tmdb_id') or '').strip()

    try:
        from cli_battery.app.direct_api import DirectAPI

        if not imdb_id and tmdb_id:
            imdb_id, _ = DirectAPI.tmdb_to_imdb(tmdb_id, media_type=media_type)
            if not imdb_id:
                return jsonify({'success': False,
                                'error': f'No IMDb ID found for TMDB {tmdb_id} ({media_type})'})

        if not imdb_id:
            return jsonify({'success': False, 'error': 'Provide an IMDb ID or a TMDB ID'}), 400
        if not _re.fullmatch(r'tt\d{7,10}', imdb_id):
            return jsonify({'success': False,
                            'error': f'"{imdb_id}" is not a valid IMDb ID (expected tt1234567)'}), 400

        if media_type == 'show':
            metadata, source = DirectAPI.get_show_metadata(imdb_id)
        else:
            metadata, source = DirectAPI.get_movie_metadata(imdb_id)

        if not metadata:
            return jsonify({'success': False,
                            'error': f'No {media_type} metadata found for {imdb_id}. '
                                     'Check the ID, and that it is the right media type.'})

        resolved = _fix_match_normalize_ids(metadata)
        resolved['imdb_id'] = resolved['imdb_id'] or imdb_id

        # Blast radius: which rows the apply step would rewrite.
        affected_rows, affected_titles = 0, []
        where_sql, where_params = _fix_match_row_filter(
            media_type, data.get('current_imdb_id'), data.get('current_tmdb_id'))
        if where_sql:
            conn = get_db_connection()
            try:
                rows = conn.execute(
                    f"SELECT COUNT(*) AS n, title, year FROM media_items "
                    f"WHERE {where_sql} GROUP BY title, year ORDER BY n DESC",
                    where_params).fetchall()
            finally:
                conn.close()
            affected_rows = sum(row['n'] for row in rows)
            affected_titles = [
                {'title': row['title'], 'year': row['year'], 'count': row['n']}
                for row in rows[:5]
            ]

        return jsonify({
            'success': True,
            'source': source,
            'title': metadata.get('title'),
            'year': metadata.get('year'),
            'type': metadata.get('type') or media_type,
            'affected_rows': affected_rows,
            'affected_titles': affected_titles,
            **resolved,
        })
    except Exception as e:
        logging.error(f"[FixMatch] preview for {imdb_id or tmdb_id} failed: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def _fix_match_refresh_entry_metadata(media_type, imdb_id, queue_missing=False):
    """
    Re-pull the per-item metadata for a just-corrected entry.

    Fixing the IDs leaves everything hanging off them - episode titles, air
    dates, genres, poster - still describing the *old* match, so the fix is
    only half done until they are refreshed.  This reuses the very endpoint the
    Refresh Metadata button calls (it takes only a media_id, so it is safe to
    invoke directly) rather than duplicating its episode-walking logic.

    Never raises.  The IDs are already committed by the time this runs, so a
    failed refresh must not turn a successful fix into an error - it is
    reported alongside the result instead.
    """
    try:
        if media_type == 'show':
            # Correcting a match must not, on its own, start downloading two
            # more seasons; queueing what is missing is opt-in.
            response = refresh_show_metadata(imdb_id, queue_missing=queue_missing)
        else:
            response = refresh_movie_metadata(imdb_id)
        # A view returns a Response, a bare dict (Flask jsonifies those itself),
        # or either of those paired with a status code on the failure paths.
        if isinstance(response, tuple):
            response = response[0]
        if isinstance(response, dict):
            payload = response
        elif hasattr(response, 'get_json'):
            payload = response.get_json(silent=True)
        else:
            payload = None
        if not isinstance(payload, dict):
            return {'success': False, 'error': 'Metadata refresh returned no result'}
        return payload
    except Exception as e:
        logging.error(f"[FixMatch] Metadata refresh for {imdb_id} failed: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}

def _fix_match_rematch_media_server(rating_key, title, year, tmdb_id, imdb_id, media_type):
    """
    Re-point the media server's own match at the corrected IDs.

    Runs on a worker thread: force_match_with_tmdb sleeps between Plex calls and
    can fall back to a trial-and-error search, so it is far too slow to hold the
    request open. The outcome goes to the log.
    """
    try:
        from utilities.plex_matching_functions import force_match_with_tmdb
        # Deliberately no season/episode: this is an entry-level fix, so the GUID
        # fast-path should resolve to the show (or movie) GUID, not an episode's.
        # ignore_previous_attempts, because the user asked for this by hand and the
        # once-per-rating-key guard would otherwise silently skip a repeat fix.
        ok = force_match_with_tmdb(
            title,
            str(year) if year else None,
            str(tmdb_id) if tmdb_id else '0',
            str(rating_key),
            imdb_id=imdb_id,
            media_type=media_type,
            ignore_previous_attempts=True,
        )
        logging.info(f"[FixMatch] Media server rematch for ratingKey={rating_key} "
                     f"-> {imdb_id}/tmdb {tmdb_id}: {'succeeded' if ok else 'failed'}")
    except Exception as e:
        logging.error(f"[FixMatch] Media server rematch for ratingKey={rating_key} errored: {e}",
                      exc_info=True)

@library_bp.route('/fix_match/apply', methods=['POST'])
@admin_required
def fix_match_apply():
    """
    Rewrite a library entry's provider IDs everywhere they are cached, then
    re-pull the metadata that hangs off them.

    Order matters: the metadata fetch happens first so a bad IMDb ID aborts
    before anything has been changed, and the refresh runs only once the rows
    carry the new IDs, because it looks the entry up by them.
    """
    import re as _re
    import threading

    data = request.get_json(silent=True) or {}
    media_type = data.get('media_type') if data.get('media_type') in ('show', 'movie') else None
    new_imdb_id = (data.get('new_imdb_id') or '').strip()
    old_imdb_id = (data.get('current_imdb_id') or '').strip() or None
    old_tmdb_id = str(data.get('current_tmdb_id') or '').strip() or None
    rematch_media_server = bool(data.get('rematch_media_server', True))
    queue_missing = bool(data.get('queue_missing', False))

    if not media_type:
        return jsonify({'success': False, 'error': "media_type must be 'show' or 'movie'"}), 400
    if not _re.fullmatch(r'tt\d{7,10}', new_imdb_id):
        return jsonify({'success': False,
                        'error': f'"{new_imdb_id}" is not a valid IMDb ID (expected tt1234567)'}), 400
    if not old_imdb_id and not old_tmdb_id:
        return jsonify({'success': False,
                        'error': "Need the entry's current IMDb or TMDB ID to know which rows to fix"}), 400

    try:
        from cli_battery.app.direct_api import DirectAPI
        from cli_battery.app.database import (managed_session, Item,
                                              TMDBToIMDBMapping, TVDBToIMDBMapping)

        # -- 1. Fetch the correct metadata first (nothing is mutated on failure) --
        metadata, source = DirectAPI.force_refresh_metadata(new_imdb_id, item_type=media_type)
        if not metadata:
            return jsonify({'success': False,
                            'error': f'Could not fetch {media_type} metadata for {new_imdb_id} - '
                                     'nothing was changed. Check the ID and the media type.'})

        resolved = _fix_match_normalize_ids(metadata)
        new_tmdb_id = resolved['tmdb_id']
        new_tvdb_id = resolved['tvdb_id']
        new_title = metadata.get('title')
        new_year = metadata.get('year')

        # -- 2. Drop the battery entry the wrong IMDb ID left behind --
        battery_item_deleted = False
        if old_imdb_id and old_imdb_id != new_imdb_id:
            with managed_session() as session:
                stale = session.query(Item).filter_by(imdb_id=old_imdb_id).first()
                if stale:
                    session.delete(stale)
                    battery_item_deleted = True

        # -- 3. Repoint the battery's ID mapping caches --
        # Both mapping tables are unique on their ID columns, so every row that
        # could collide is deleted (and flushed) before the correct one is added.
        with managed_session() as session:
            if old_tmdb_id:
                session.query(TMDBToIMDBMapping).filter_by(tmdb_id=str(old_tmdb_id)).delete()
            if old_imdb_id:
                session.query(TMDBToIMDBMapping).filter_by(imdb_id=old_imdb_id).delete()
                session.query(TVDBToIMDBMapping).filter_by(imdb_id=old_imdb_id).delete()
            if new_tmdb_id:
                session.query(TMDBToIMDBMapping).filter_by(tmdb_id=str(new_tmdb_id)).delete()
            if new_tvdb_id:
                session.query(TVDBToIMDBMapping).filter_by(tvdb_id=str(new_tvdb_id)).delete()
            session.query(TVDBToIMDBMapping).filter_by(imdb_id=new_imdb_id).delete()
            session.flush()

            if new_tmdb_id:
                session.add(TMDBToIMDBMapping(tmdb_id=str(new_tmdb_id),
                                              imdb_id=new_imdb_id,
                                              media_type=media_type))
            if new_tvdb_id:
                session.add(TVDBToIMDBMapping(tvdb_id=str(new_tvdb_id),
                                              imdb_id=new_imdb_id,
                                              media_type=media_type))

        # -- 4. Rewrite media_items --
        # Title and year go along with the IDs: a wrong match means the rows are
        # also carrying the wrong show's name, which is what the library grid
        # renders.  Per-episode fields (episode_title, release_date) still come
        # from the old match - Refresh Metadata is what re-pulls those.
        where_sql, where_params = _fix_match_row_filter(media_type, old_imdb_id, old_tmdb_id)
        conn = get_db_connection()
        try:
            rows = conn.execute(
                f"SELECT id, title, year, ms_item_id, state FROM media_items WHERE {where_sql}",
                where_params).fetchall()

            # Same predicate rather than an id list of the rows just selected:
            # no placeholder-count ceiling on a long-running show.
            set_parts = ['imdb_id = ?']
            set_params = [new_imdb_id]
            # Only overwrite tmdb_id/title/year when the provider actually gave us
            # one, so a sparse metadata record (no TMDB match for this IMDb ID)
            # cannot blank out a previously-working value on every matched row.
            if new_tmdb_id:
                set_parts.append('tmdb_id = ?')
                set_params.append(new_tmdb_id)
            if new_title:
                set_parts.append('title = ?')
                set_params.append(new_title)
            if new_year:
                set_parts.append('year = ?')
                set_params.append(new_year)
            set_parts.append('last_updated = ?')
            set_params.append(datetime.now())

            cursor = conn.execute(
                f"UPDATE media_items SET {', '.join(set_parts)} WHERE {where_sql}",
                set_params + where_params)
            rows_updated = cursor.rowcount
            conn.commit()
        finally:
            conn.close()

        logging.info(f"[FixMatch] {media_type} {old_imdb_id or old_tmdb_id} -> {new_imdb_id} "
                     f"(tmdb {old_tmdb_id} -> {new_tmdb_id}, tvdb -> {new_tvdb_id}); "
                     f"{rows_updated} media_items rows updated")

        # -- 5. Re-pull everything that hangs off the corrected IDs --
        # Runs before the rematch is dispatched so the battery (and its Plex
        # GUIDs) are already fresh when force_match_with_tmdb looks them up.
        metadata_refresh = _fix_match_refresh_entry_metadata(
            media_type, new_imdb_id, queue_missing=queue_missing)

        # -- 5b. Make the correction stick --
        # Imports of files cli_debrid did not place re-derive the match from the
        # release name every time, so without this the same fuzzy search that
        # mis-resolved this title would undo the fix on the next new file.
        override_recorded = False
        if new_title:
            from database.match_overrides import set_match_override
            override_recorded = set_match_override(
                new_title, new_year, media_type, new_imdb_id)

        # -- 6. Hand the media server the corrected match --
        rematch_started = False
        rematch_note = None
        if rematch_media_server:
            is_plex = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'
            plex_configured = bool(get_setting('Plex', 'url', default='')
                                   and get_setting('Plex', 'token', default=''))
            rating_key = next((str(row['ms_item_id']) for row in rows
                               if row['ms_item_id'] not in (None, '')), None)

            from utilities.silo_functions import is_silo_selected
            if is_silo_selected():
                rematch_note = 'Media server rematch skipped - Silo matches files itself'
            elif not is_plex:
                rematch_note = 'Media server rematch skipped - not in Direct Mount'
            elif not plex_configured:
                rematch_note = 'Media server rematch skipped - Plex is not configured'
            elif not rating_key:
                rematch_note = ('Media server rematch skipped - no Plex ratingKey stored for this '
                                'entry yet')
            elif not new_tmdb_id:
                rematch_note = 'Media server rematch skipped - no TMDB ID resolved to match against'
            else:
                threading.Thread(
                    target=_fix_match_rematch_media_server,
                    args=(rating_key, new_title, new_year, new_tmdb_id, new_imdb_id, media_type),
                    daemon=True,
                ).start()
                rematch_started = True
                rematch_note = 'Plex rematch running in the background - see the logs for the result'

        return jsonify({
            'success': True,
            'source': source,
            'title': new_title,
            'year': new_year,
            'imdb_id': new_imdb_id,
            'tmdb_id': new_tmdb_id,
            'tvdb_id': new_tvdb_id,
            'previous': {'imdb_id': old_imdb_id, 'tmdb_id': old_tmdb_id},
            'rows_updated': rows_updated,
            'battery_item_deleted': battery_item_deleted,
            'override_recorded': override_recorded,
            'metadata_refresh': {
                'success': bool(metadata_refresh.get('success')),
                'updated_episodes': metadata_refresh.get('updated_episodes'),
                'new_episodes_added': metadata_refresh.get('new_episodes_added'),
                'error': metadata_refresh.get('error'),
            },
            'rematch_started': rematch_started,
            'rematch_note': rematch_note,
        })
    except Exception as e:
        logging.error(f"[FixMatch] apply {old_imdb_id or old_tmdb_id} -> {new_imdb_id} failed: {e}",
                      exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500
