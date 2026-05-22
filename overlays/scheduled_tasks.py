"""
Scheduled Tasks for Overlay System

Periodic tasks for overlay sync and cleanup.
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any

from .overlay_manager import OverlayManager
from .layout_manager import LayoutManager
from .activity_logger import log_activity
from .utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token
from utilities.settings import get_setting

logger = logging.getLogger(__name__)

# In-flight guard for apply_overlay_for_new_item — prevents duplicate runs when
# handle_state_change fires twice for the same item within a short window.
_overlay_in_flight: set = set()
_overlay_in_flight_lock = threading.Lock()

# Lock to prevent concurrent Plex library reads in _sync_ms_keys_auto
_sync_ms_keys_lock = threading.Lock()

# Concurrency cap: at most 3 overlay threads run image operations simultaneously.
# Without this, large batch downloads spawn 30+ threads, each loading/rendering
# poster images → memory spikes to 2+ GB → OOM container kill.
_overlay_semaphore = threading.Semaphore(3)


def _get_db_connection():
    """Get a database connection using the production DB access pattern."""
    from database.core import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn


def _sync_library_keys_for_new_items(plex_url: str, plex_token: str) -> Dict[str, int]:
    """
    Auto-populate ms_item_id for Collected items that don't have one yet.

    DB-first approach: fetch only the rows that are missing ms_item_id, build
    in-memory lookup dicts from a single Plex API call, resolve each missing row
    in O(1), then write only the matched rows. This avoids iterating the entire
    Plex library (4000+ items) for every row — previously caused 16+ minute
    sync times when only 14 rows needed updating.

    Also detects ms_item_id changes (e.g. after Plex split-apart) and resets
    overlay state to pending so overlays are regenerated with the new key.

    Returns a dict with counts: {'movies': N, 'episodes': N, 'errors': N}
    """
    conn = _get_db_connection()
    cursor = conn.cursor()

    # Fetch rows needing a key update — two categories:
    # 1. Missing: ms_item_id is NULL/empty → first-time assignment
    # 2. Candidates for split-detection: have ms_item_id AND location_on_disk
    #    → Plex may have assigned a new ratingKey after split-apart
    # Both are fetched upfront; the Plex lookup dicts resolve both in O(1).
    cursor.execute('''
        SELECT id, type, imdb_id, tmdb_id, title, year, location_on_disk, ms_item_id
        FROM media_items
        WHERE state IN ('Collected', 'Upgrading')
    ''')
    all_rows = cursor.fetchall()
    conn.close()

    missing_rows   = [dict(r) for r in all_rows if not (r['ms_item_id'] or '').strip()]
    split_rows     = [dict(r) for r in all_rows
                      if (r['ms_item_id'] or '').strip() and (r['location_on_disk'] or '').strip()]

    missing_count = len(missing_rows)
    split_candidate_count = len(split_rows)

    if not missing_count and not split_candidate_count:
        return {'movies': 0, 'episodes': 0, 'errors': 0}

    logger.info(
        f"Auto ms-key sync: {missing_count} missing ms_item_id, "
        f"{split_candidate_count} candidate(s) for split-apart detection"
    )

    counts = {'movies': 0, 'episodes': 0, 'errors': 0}

    # Group by type for separate Plex API calls
    def _by_type(rows):
        d = {'movie': [], 'episode': []}
        for r in rows:
            if r['type'] in d:
                d[r['type']].append(r)
        return d

    missing_by_type = _by_type(missing_rows)
    split_by_type   = _by_type(split_rows)

    try:
        from .plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)

        for db_type, count_key, plex_type in (
            ('movie',   'movies',   1),
            ('episode', 'episodes', 2),
        ):
            rows_missing = missing_by_type[db_type]
            rows_split   = split_by_type[db_type]

            if not rows_missing and not rows_split:
                continue

            try:
                # Single Plex API call — build in-memory lookup dicts
                plex_items = client.get_all_items_with_guids(plex_type=plex_type)

                file_map:  dict = {}   # location_on_disk → ratingKey
                imdb_map:  dict = {}   # imdb_id          → ratingKey
                tmdb_map:  dict = {}   # tmdb_id (str)    → ratingKey
                title_map: dict = {}   # (title_lower, year_str) → ratingKey

                for pi in plex_items:
                    rk = pi.get('ratingKey')
                    if not rk:
                        continue
                    for fp in (pi.get('file_paths') or []):
                        if fp:
                            file_map[fp] = rk
                    if pi.get('imdb_id'):
                        imdb_map[pi['imdb_id']] = rk
                    if pi.get('tmdb_id'):
                        tmdb_map[str(pi['tmdb_id'])] = rk
                    if pi.get('title') and pi.get('year'):
                        title_map[(pi['title'].lower(), str(pi['year']))] = rk

                # resolved: list of (db_id, new_rk, old_rk)
                resolved = []

                # Pass 1: rows missing ms_item_id entirely
                for row in rows_missing:
                    db_id   = row['id']
                    old_rk  = ''
                    loc     = row['location_on_disk'] or ''
                    imdb_id = row['imdb_id'] or ''
                    tmdb_id = str(row['tmdb_id'] or '')
                    title   = (row['title'] or '').lower()
                    year    = str(row['year'] or '')

                    new_rk = (
                        file_map.get(loc)
                        or (imdb_map.get(imdb_id) if imdb_id else None)
                        or (tmdb_map.get(tmdb_id) if tmdb_id else None)
                        or (title_map.get((title, year)) if title and year else None)
                    )
                    if new_rk:
                        resolved.append((db_id, new_rk, old_rk))

                # Pass 2: rows that have ms_item_id but Plex file-path now maps
                # to a different ratingKey — these are split-apart items.
                # Only file-path matching is reliable here; imdb_id would resolve
                # to whichever split item Plex returns first.
                for row in rows_split:
                    db_id   = row['id']
                    old_rk  = row['ms_item_id']
                    loc     = row['location_on_disk']

                    new_rk = file_map.get(loc)
                    if new_rk and new_rk != old_rk:
                        resolved.append((db_id, new_rk, old_rk))

                if not resolved:
                    continue

                # Write all resolved matches in one short-lived transaction
                conn = _get_db_connection()
                try:
                    cursor = conn.cursor()
                    changed_item_ids = []

                    for db_id, new_rk, old_rk in resolved:
                        cursor.execute(
                            'UPDATE media_items SET ms_item_id = ? WHERE id = ?',
                            (new_rk, db_id))
                        if cursor.rowcount:
                            counts[count_key] += 1
                            if old_rk and old_rk != new_rk:
                                changed_item_ids.append(db_id)

                    # Reset overlay state for split-apart items so overlays
                    # are regenerated with the new unique ratingKey.
                    if changed_item_ids:
                        placeholders = ','.join('?' * len(changed_item_ids))
                        cursor.execute(
                            f'UPDATE media_overlay_state '
                            f'SET status = \'pending\', '
                            f'reason = \'ms_item_id changed — overlay needs regeneration\', '
                            f'updated_at = CURRENT_TIMESTAMP '
                            f'WHERE media_item_id IN ({placeholders}) AND status = \'applied\'',
                            changed_item_ids)
                        if cursor.rowcount:
                            logger.info(
                                f"ms-key sync: reset {cursor.rowcount} overlay(s) to pending "
                                f"(ms_item_id changed after Plex split-apart)")

                        # Also reset siblings — items that still hold the old shared
                        # ms_item_id (the pre-split merged ratingKey). Their overlays
                        # were generated when the items were merged (version_count=2,
                        # possibly wrong resolution data) and need regeneration too.
                        old_rks = list({old_rk for _, _, old_rk in resolved if old_rk})
                        if old_rks:
                            excl_ph = ','.join('?' * len(changed_item_ids))
                            old_ph  = ','.join('?' * len(old_rks))
                            cursor.execute(
                                f'SELECT id FROM media_items '
                                f'WHERE ms_item_id IN ({old_ph}) '
                                f'AND id NOT IN ({excl_ph}) '
                                f'AND state IN (\'Collected\', \'Upgrading\')',
                                old_rks + changed_item_ids)
                            sibling_ids = [r[0] for r in cursor.fetchall()]
                            if sibling_ids:
                                sib_ph = ','.join('?' * len(sibling_ids))
                                cursor.execute(
                                    f'UPDATE media_overlay_state '
                                    f'SET status = \'pending\', '
                                    f'reason = \'sibling split-apart — overlay needs regeneration\', '
                                    f'updated_at = CURRENT_TIMESTAMP '
                                    f'WHERE media_item_id IN ({sib_ph}) AND status = \'applied\'',
                                    sibling_ids)
                                if cursor.rowcount:
                                    logger.info(
                                        f"ms-key sync: reset {cursor.rowcount} sibling overlay(s) "
                                        f"to pending (were merged with split-apart item)")

                    conn.commit()
                except Exception as _we:
                    logger.error(f"ms-key sync write failed ({db_type}): {_we}", exc_info=True)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    counts['errors'] += 1
                finally:
                    conn.close()

            except Exception as e:
                label = 'movies' if plex_type == 1 else 'shows'
                logger.error(f"Auto ms-key sync ({label}) failed: {e}", exc_info=True)
                counts['errors'] += 1

    except Exception as e:
        logger.error(f"Auto ms-key sync failed: {e}", exc_info=True)
        counts['errors'] += 1

    logger.info(
        f"Auto ms-key sync complete: {counts['movies']} movie(s), "
        f"{counts['episodes']} episode(s) updated, {counts['errors']} error(s)"
    )
    return counts


def _sync_library_keys_for_jellyfin() -> Dict[str, int]:
    """
    Jellyfin/Emby variant of _sync_library_keys_for_new_items.

    Auto-populates ms_item_id for Collected items that don't have one yet by
    matching against Jellyfin ProviderIds (IMDB / TMDB).

    Also overwrites any ms_item_id values that look like old Plex integer rating
    keys (numeric-only strings) — these are left over from the plex_rating_key →
    ms_item_id migration and will cause 400 errors against the Jellyfin API.
    Jellyfin IDs are always UUID strings (contain hyphens).

    Returns a dict with counts: {'movies': N, 'episodes': N, 'errors': N}
    """
    conn = _get_db_connection()
    cursor = conn.cursor()

    # Count items that need a Jellyfin ID: missing OR still holding a Plex integer key
    # Jellyfin IDs are 32-char hex (no hyphens); Plex keys are short integers.
    # LENGTH < 20 reliably identifies stale Plex integer keys.
    cursor.execute('''
        SELECT COUNT(*) FROM media_items
        WHERE state IN ('Collected', 'Upgrading')
          AND (ms_item_id IS NULL OR ms_item_id = ''
               OR LENGTH(ms_item_id) < 20)
    ''')
    missing_count = cursor.fetchone()[0]
    conn.close()

    if not missing_count:
        return {'movies': 0, 'episodes': 0, 'errors': 0}

    logger.info(f"Auto Jellyfin ms-key sync: {missing_count} Collected item(s) need Jellyfin ms_item_id")

    counts = {'movies': 0, 'episodes': 0, 'errors': 0}

    try:
        from .jellyfin_client import JellyfinClient
        client = JellyfinClient(get_jellyfin_url(), get_jellyfin_token())

        for media_type, db_type, count_key in (
            ('Movie',   'movie',   'movies'),
            ('Series',  'episode', 'episodes'),
        ):
            try:
                jf_items = client.get_all_items_with_guids(media_type=media_type)
                conn = _get_db_connection()
                cursor = conn.cursor()
                for ji in jf_items:
                    rk = ji.get('ratingKey')  # Jellyfin ItemId stored in ratingKey field
                    imdb_id = ji.get('imdb_id')
                    tmdb_id = ji.get('tmdb_id')
                    if not rk:
                        continue
                    # Match items with no ID or a legacy Plex integer ID
                    needs_id_cond = (
                        "(ms_item_id IS NULL OR ms_item_id = '' OR LENGTH(ms_item_id) < 20)"
                    )
                    updated = False
                    if imdb_id:
                        cursor.execute(f'''
                            UPDATE media_items
                            SET ms_item_id = ?
                            WHERE type = ?
                              AND imdb_id = ?
                              AND state IN ('Collected', 'Upgrading')
                              AND {needs_id_cond}
                        ''', (rk, db_type, imdb_id))
                        if cursor.rowcount:
                            counts[count_key] += cursor.rowcount
                            updated = True
                    if not updated and tmdb_id:
                        cursor.execute(f'''
                            UPDATE media_items
                            SET ms_item_id = ?
                            WHERE type = ?
                              AND tmdb_id = ?
                              AND state IN ('Collected', 'Upgrading')
                              AND {needs_id_cond}
                        ''', (rk, db_type, tmdb_id))
                        if cursor.rowcount:
                            counts[count_key] += cursor.rowcount
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Jellyfin ms-key sync ({media_type}) failed: {e}", exc_info=True)
                counts['errors'] += 1

    except Exception as e:
        logger.error(f"Jellyfin ms-key sync failed: {e}", exc_info=True)
        counts['errors'] += 1

    logger.info(
        f"Jellyfin ms-key sync complete: {counts['movies']} movie(s), "
        f"{counts['episodes']} episode(s) updated, {counts['errors']} error(s)"
    )
    return counts


def _sync_ms_keys_auto() -> Dict[str, int]:
    """Route to the correct ms-key sync function based on current mode.

    Uses a lock to prevent concurrent Plex library reads when multiple threads
    (e.g. parallel apply_overlay_for_new_item retries) call this simultaneously.
    """
    if not _sync_ms_keys_lock.acquire(blocking=False):
        logger.debug("_sync_ms_keys_auto: already running in another thread, skipping")
        return {'movies': 0, 'episodes': 0, 'errors': 0}
    try:
        if is_jellyfin_mode():
            return _sync_library_keys_for_jellyfin()
        plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        return _sync_library_keys_for_new_items(plex_url, plex_token)
    finally:
        _sync_ms_keys_lock.release()


def apply_overlay_for_new_item(item_id: int):
    """
    Apply overlay for a single newly-collected item.

    Designed to be called from handle_state_change() in a background daemon thread
    so it does not block the queue processing loop.

    If ms_item_id is not yet set (media server hasn't indexed the file yet), retries
    up to 3 times with increasing delays before giving up. The scheduled
    task_overlay_sync will catch any items missed here on the next run.
    """
    # Atomically claim this item_id; bail out if another thread already has it.
    with _overlay_in_flight_lock:
        if item_id in _overlay_in_flight:
            logger.debug(f"apply_overlay_for_new_item: item {item_id} already in-flight, skipping duplicate")
            return
        _overlay_in_flight.add(item_id)

    try:
        if not get_setting('Overlay Settings', 'overlays_enabled', False):
            return

        # Mode detection: get credentials for whichever media server is active
        if is_jellyfin_mode():
            jf_url   = get_jellyfin_url()
            jf_token = get_jellyfin_token()
            if not jf_token:
                return
            plex_url   = ''
            plex_token = jf_token  # passed to OverlayManager for backward compat signature
        else:
            plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
            plex_token = get_setting('Plex', 'token', default='')
            if not plex_token:
                return

        # Throttle: at most 3 threads do heavy work (DB lookups, ms-key sync, image
        # download/render/upload) concurrently.  When a large batch is collected,
        # additional threads block here and are served as slots free up.  Without
        # this cap, 30+ concurrent poster operations drove RSS from ~330 MB to
        # 2+ GB, triggering an OOM container kill.
        with _overlay_semaphore:
            _apply_overlay_work(item_id, plex_url, plex_token)

    except Exception as e:
        logger.error(f"apply_overlay_for_new_item: failed for item {item_id}: {e}", exc_info=True)
    finally:
        with _overlay_in_flight_lock:
            _overlay_in_flight.discard(item_id)


def _apply_overlay_work(item_id: int, plex_url: str, plex_token: str):
    """Inner worker called once the concurrency semaphore is held."""
    import time

    # Fetch base item info
    conn = _get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, ms_item_id, type, title, year, season_number, episode_number FROM media_items '
        "WHERE id = ? AND state IN ('Collected', 'Upgrading')",
        (item_id,)
    )
    item_row = cursor.fetchone()
    conn.close()

    if not item_row:
        logger.debug(f"apply_overlay_for_new_item: item {item_id} not found or not Collected/Upgrading, skipping")
        return

    item_type = item_row['type']
    ms_item_id = item_row['ms_item_id']

    def _needs_jellyfin_sync(mid):
        """True if ms_item_id is absent or a legacy Plex integer key."""
        if not mid:
            return True
        if is_jellyfin_mode() and '-' not in str(mid) and str(mid).isdigit():
            return True  # Old Plex rating key copied from migration
        return False

    # If ms_item_id not populated yet (or still a stale Plex integer ID in Jellyfin
    # mode), give media server time to index then retry
    if _needs_jellyfin_sync(ms_item_id):
        delays = [15, 30, 60]
        for attempt, wait in enumerate(delays, 1):
            logger.debug(
                f"apply_overlay_for_new_item: item {item_id} missing/stale ms_item_id, "
                f"waiting {wait}s before retry {attempt}/{len(delays)}"
            )
            time.sleep(wait)
            _sync_ms_keys_auto()

            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT ms_item_id FROM media_items WHERE id = ?', (item_id,))
            row2 = cursor.fetchone()
            conn.close()

            ms_item_id = row2['ms_item_id'] if row2 else None
            if not _needs_jellyfin_sync(ms_item_id):
                break

    if _needs_jellyfin_sync(ms_item_id):
        logger.info(
            f"apply_overlay_for_new_item: item {item_id} ms_item_id still not found/stale "
            f"after retries — scheduled sync will handle it"
        )
        return

    manager = OverlayManager(None, plex_url, plex_token)
    result = manager.generate_overlay_for_item(item_id, force=False)
    status = result.get('status', 'failed')

    if status == 'applied' and item_type == 'episode':
        _mark_all_episodes_applied(ms_item_id, result.get('content_hash'))
        # Immediately re-render all season posters so their version count badges
        # update without waiting for the next task_overlay_sync cycle.
        # force=True bypasses the quality-hash skip check on already-applied seasons.
        seasons = _get_seasons_for_immediate_render(manager, ms_item_id)
        for s in seasons:
            try:
                manager.generate_season_overlay(
                    show_plex_rating_key=ms_item_id,
                    season_plex_rating_key=s['ratingKey'],
                    season_number=s.get('index', 0),
                    force=True,
                )
            except Exception as _se:
                logger.warning(f"apply_overlay_for_new_item: season overlay failed for {s}: {_se}")

    logger.info(f"apply_overlay_for_new_item: item {item_id} → {status}")
    if status in ('applied', 'failed'):
        _title    = item_row['title'] or ''
        _year     = item_row['year']
        _type     = item_row['type']
        _season   = item_row['season_number']
        _episode  = item_row['episode_number']
        if _type == 'episode':
            _display = f"{_title} S{str(_season).zfill(2)}E{str(_episode).zfill(2)}"
        elif _year:
            _display = f"{_title} ({_year})"
        else:
            _display = _title
        log_activity('generate',
                     triggered_by='scheduled',
                     result='success' if status == 'applied' else 'failed',
                     title=f"Auto overlay on collect: {_display}",
                     stats={'media_item_id': item_id, 'type': _type})


def _reset_quality_changed_items() -> int:
    """
    Scan all 'applied' overlay items and reset any whose current best-quality DB
    data no longer matches the stored quality hash (last_metadata_hash).

    This catches quality upgrades that happened between scheduled sync runs — e.g.
    a 4K episode was added to a 1080p show, or a movie was replaced by a better
    encode.  Items reset to 'pending' are picked up by the main sync loop.

    Returns the number of items reset.
    """
    import hashlib, json as _json

    def _quality_hash(resolution, hdr, dolby_vision, audio_codec, audio_channels, video_codec):
        key = _json.dumps({
            'r':  (resolution     or '').lower().strip(),
            'h':  bool(hdr),
            'dv': bool(dolby_vision),
            'a':  (audio_codec    or '').lower().strip(),
            'ac': (audio_channels or '').lower().strip(),
            'vc': (video_codec    or '').lower().strip(),
        }, sort_keys=True)
        return hashlib.md5(key.encode()).hexdigest()

    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Fetch one representative row per media server item (show or movie) with applied overlay
        cursor.execute('''
            SELECT
                MIN(o.media_item_id) AS rep_id,
                m.ms_item_id,
                m.type,
                m.imdb_id,
                o.last_metadata_hash
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE o.status = 'applied'
              AND o.last_metadata_hash IS NOT NULL
              AND o.last_metadata_hash != ''
              AND m.ms_item_id IS NOT NULL
            GROUP BY
                CASE WHEN m.type = 'episode' THEN m.ms_item_id
                     ELSE CAST(m.id AS TEXT) END
        ''')
        applied_rows = cursor.fetchall()

        # Pre-fetch best quality per show (ms_item_id) and per movie (imdb_id)
        # using efficient grouped queries rather than N+1 individual queries.
        show_keys  = list({r['ms_item_id'] for r in applied_rows if r['type'] == 'episode'})
        # Movies: scope by ms_item_id so split Plex items (unique ratingKey per version)
        # each compare quality against their own DB rows, not all versions of the movie.
        movie_ms_ids = list({r['ms_item_id'] for r in applied_rows if r['type'] == 'movie' and r['ms_item_id']})

        _RES = ("CASE lower(ms_resolution) WHEN '2160p' THEN 6 WHEN '4k' THEN 6 "
                "WHEN '1440p' THEN 5 WHEN '1080p' THEN 4 WHEN '720p' THEN 3 "
                "WHEN '576p' THEN 2 WHEN '480p' THEN 1 ELSE 0 END")
        _HDR = ("CASE WHEN ms_dolby_vision=1 AND ms_hdr=1 THEN 4 "
                "WHEN ms_dolby_vision=1 THEN 3 WHEN ms_hdr=1 THEN 2 ELSE 1 END")
        _AUD = ("CASE lower(ms_audio_codec) "
                "WHEN 'truehd atmos' THEN 10 WHEN 'truehd+atmos' THEN 10 "
                "WHEN 'dts:x' THEN 9 WHEN 'truehd' THEN 8 "
                "WHEN 'dts-hd ma' THEN 7 WHEN 'dts-hd master audio' THEN 7 "
                "WHEN 'dts-hd hra' THEN 6 WHEN 'dts-hd' THEN 6 "
                "WHEN 'eac3 atmos' THEN 5 WHEN 'dd+ atmos' THEN 5 "
                "WHEN 'eac3' THEN 4 WHEN 'dd+' THEN 4 WHEN 'flac' THEN 4 "
                "WHEN 'dd atmos' THEN 3 WHEN 'ac3' THEN 3 "
                "WHEN 'dolby digital' THEN 3 WHEN 'dd' THEN 3 WHEN 'dts' THEN 3 "
                "WHEN 'aac' THEN 2 ELSE 1 END")

        show_best  = {}
        movie_best = {}

        if show_keys:
            placeholders = ','.join('?' * len(show_keys))
            cursor.execute(f'''
                SELECT ms_item_id,
                       ms_resolution, ms_hdr, ms_dolby_vision,
                       ms_audio_codec, ms_audio_channels, ms_video_codec
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ms_item_id
                        ORDER BY ({_RES}) DESC, ({_HDR}) DESC, ({_AUD}) DESC,
                                 CAST(COALESCE(ms_audio_channels,'0') AS REAL) DESC
                    ) AS rn
                    FROM media_items
                    WHERE type = 'episode'
                      AND ms_item_id IN ({placeholders})
                      AND state IN ('Collected','Upgrading')
                      AND ms_resolution IS NOT NULL
                ) WHERE rn = 1
            ''', show_keys)
            for r in cursor.fetchall():
                show_best[r['ms_item_id']] = r

        if movie_ms_ids:
            placeholders = ','.join('?' * len(movie_ms_ids))
            cursor.execute(f'''
                SELECT ms_item_id,
                       ms_resolution, ms_hdr, ms_dolby_vision,
                       ms_audio_codec, ms_audio_channels, ms_video_codec
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ms_item_id
                        ORDER BY ({_RES}) DESC, ({_HDR}) DESC, ({_AUD}) DESC,
                                 CAST(COALESCE(ms_audio_channels,'0') AS REAL) DESC
                    ) AS rn
                    FROM media_items
                    WHERE type = 'movie'
                      AND ms_item_id IN ({placeholders})
                      AND state IN ('Collected','Upgrading')
                      AND ms_resolution IS NOT NULL
                ) WHERE rn = 1
            ''', movie_ms_ids)
            for r in cursor.fetchall():
                movie_best[r['ms_item_id']] = r

        # Compare stored hash vs current quality hash; collect items to reset
        reset_ids = []
        for row in applied_rows:
            stored_hash = row['last_metadata_hash']
            if row['type'] == 'episode':
                best = show_best.get(row['ms_item_id'])
            else:
                best = movie_best.get(row['ms_item_id'])

            if not best:
                continue

            current_hash = _quality_hash(
                best['ms_resolution'], best['ms_hdr'], best['ms_dolby_vision'],
                best['ms_audio_codec'], best['ms_audio_channels'], best['ms_video_codec']
            )
            if current_hash != stored_hash:
                reset_ids.append(row['rep_id'])
                logger.debug(
                    f"Quality change detected for key={row['ms_item_id']} "
                    f"({stored_hash[:8]}→{current_hash[:8]}), resetting to pending"
                )

        if reset_ids:
            cursor.executemany(
                "UPDATE media_overlay_state SET status='pending', "
                "reason='Quality upgraded — overlay needs refresh', "
                "updated_at=CURRENT_TIMESTAMP WHERE media_item_id=?",
                [(i,) for i in reset_ids]
            )
            conn.commit()

        # ── Season quality reset ─────────────────────────────────────────
        # Also reset applied season overlays whose best-episode quality changed.
        cursor.execute('''
            SELECT show_ms_item_id, season_ms_item_id,
                   season_number, last_metadata_hash
            FROM season_overlay_state
            WHERE status = 'applied'
              AND last_metadata_hash IS NOT NULL
              AND last_metadata_hash != ''
        ''')
        season_rows = cursor.fetchall()

        # Batch season quality check — one query for all applied seasons, using
        # a window function to rank episodes per (show_key, season_number).
        # This replaces the previous per-season N+1 loop.
        season_reset_keys = []
        if season_rows:
            season_keys_needed = list({sr['show_ms_item_id'] for sr in season_rows})
            season_nums_needed = list({sr['season_number'] for sr in season_rows})
            sk_ph = ','.join('?' * len(season_keys_needed))
            sn_ph = ','.join('?' * len(season_nums_needed))
            _RES_S = ("CASE lower(ms_resolution) WHEN '2160p' THEN 6 WHEN '4k' THEN 6 "
                      "WHEN '1440p' THEN 5 WHEN '1080p' THEN 4 WHEN '720p' THEN 3 "
                      "WHEN '576p' THEN 2 WHEN '480p' THEN 1 ELSE 0 END")
            _HDR_S = ("CASE WHEN ms_dolby_vision=1 AND ms_hdr=1 THEN 4 "
                      "WHEN ms_dolby_vision=1 THEN 3 WHEN ms_hdr=1 THEN 2 ELSE 1 END")
            _AUD_S = ("CASE lower(ms_audio_codec) "
                      "WHEN 'truehd atmos' THEN 10 WHEN 'truehd+atmos' THEN 10 "
                      "WHEN 'dts:x' THEN 9 WHEN 'truehd' THEN 8 "
                      "WHEN 'dts-hd ma' THEN 7 WHEN 'dts-hd master audio' THEN 7 "
                      "WHEN 'dts-hd hra' THEN 6 WHEN 'dts-hd' THEN 6 "
                      "WHEN 'eac3 atmos' THEN 5 WHEN 'dd+ atmos' THEN 5 "
                      "WHEN 'eac3' THEN 4 WHEN 'dd+' THEN 4 WHEN 'flac' THEN 4 "
                      "WHEN 'dd atmos' THEN 3 WHEN 'ac3' THEN 3 "
                      "WHEN 'dolby digital' THEN 3 WHEN 'dd' THEN 3 WHEN 'dts' THEN 3 "
                      "WHEN 'aac' THEN 2 ELSE 1 END")
            cursor.execute(f'''
                SELECT ms_item_id, season_number,
                       ms_resolution, ms_hdr, ms_dolby_vision,
                       ms_audio_codec, ms_audio_channels, ms_video_codec
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ms_item_id, season_number
                        ORDER BY ({_RES_S}) DESC, ({_HDR_S}) DESC, ({_AUD_S}) DESC,
                                 CAST(COALESCE(ms_audio_channels,'0') AS REAL) DESC
                    ) AS rn
                    FROM media_items
                    WHERE type = 'episode'
                      AND ms_item_id IN ({sk_ph})
                      AND season_number IN ({sn_ph})
                      AND state IN ('Collected','Upgrading')
                      AND ms_resolution IS NOT NULL
                ) WHERE rn = 1
            ''', season_keys_needed + season_nums_needed)
            season_best = {}
            for r in cursor.fetchall():
                season_best[(r['ms_item_id'], r['season_number'])] = r

            for sr in season_rows:
                best = season_best.get((sr['show_ms_item_id'], sr['season_number']))
                if not best:
                    continue
                current_hash = _quality_hash(
                    best['ms_resolution'], best['ms_hdr'], best['ms_dolby_vision'],
                    best['ms_audio_codec'], best['ms_audio_channels'], best['ms_video_codec']
                )
                if current_hash != sr['last_metadata_hash']:
                    season_reset_keys.append(sr['season_ms_item_id'])

        if season_reset_keys:
            cursor.executemany(
                "UPDATE season_overlay_state SET status='pending', "
                "reason='Quality upgraded — season overlay needs refresh', "
                "updated_at=CURRENT_TIMESTAMP WHERE season_ms_item_id=?",
                [(k,) for k in season_reset_keys]
            )
            conn.commit()
            logger.debug(f"Quality-change: reset {len(season_reset_keys)} season overlay(s) to pending")

        conn.close()
        return len(reset_ids) + len(season_reset_keys)

    except Exception as e:
        logger.error(f"_reset_quality_changed_items failed: {e}", exc_info=True)
        return 0


def _reset_content_changed_items(batch_size: int = 200) -> int:
    """
    Reset applied overlay items whose content metadata has changed since the
    overlay was last generated.

    Content metadata covers: IMDb/TMDB/Trakt/RT ratings, show status, version count.
    These change less often than quality data, so:
      - Version count and show status are checked every sync (pure SQL, zero API calls).
      - Ratings are checked at most once per `overlay_content_check_interval_days` days
        (default 7), processing up to `batch_size` items per sync run to avoid hammering
        the MDBList API when the 24-hour in-process cache is cold.

    Items whose content hash no longer matches the stored last_content_hash are reset
    to 'pending' so the main sync loop re-renders them with fresh badge values.

    Returns the total number of items reset (movies/shows + seasons combined).
    """
    import hashlib
    import json as _json
    import time

    try:
        interval_days = int(get_setting('Overlay Settings', 'overlay_content_check_interval_days',
                                        default=7))
    except Exception:
        interval_days = 7

    # ── Helper: same hash logic as OverlayManager._compute_content_hash ──────
    def _content_hash(imdb_rating=None, tmdb_rating=None, trakt_rating=None,
                      rt_critics_score=None, rt_user_score=None,
                      status=None, version_count=None) -> str:
        key = _json.dumps({
            'ir':  round(float(imdb_rating), 1)   if imdb_rating    is not None else None,
            'tr':  round(float(tmdb_rating), 1)   if tmdb_rating    is not None else None,
            'kr':  round(float(trakt_rating), 1)  if trakt_rating   is not None else None,
            'rt':  int(rt_critics_score)           if rt_critics_score is not None else None,
            'rtu': int(rt_user_score)              if rt_user_score  is not None else None,
            'st':  (status or '').lower().strip(),
            'vc':  int(version_count)              if version_count  is not None else None,
        }, sort_keys=True)
        return hashlib.md5(key.encode()).hexdigest()

    total_reset = 0

    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # ── 1. Version-count and status check (every sync, pure SQL) ─────────
        # Re-compute version_count via grouped COUNT and compare against stored
        # content hash. Also pulls ms_network/status if available for shows.
        # We fetch all applied items that have a stored content hash.
        cursor.execute('''
            SELECT
                o.media_item_id,
                o.last_content_hash,
                m.type,
                m.imdb_id,
                m.ms_item_id
            FROM media_overlay_state o
            JOIN media_items m ON m.id = o.media_item_id
            WHERE o.status = 'applied'
              AND o.last_content_hash IS NOT NULL
              AND o.last_content_hash != ''
        ''')
        applied = cursor.fetchall()

        # Pre-compute version counts per show/movie (imdb_id, matching overlay_manager
        # logic). Must use imdb_id grouping and include both Collected+Upgrading states
        # to match the hash stored during apply — using only 'Collected' causes spurious
        # hash mismatches when items are in 'Upgrading' state.
        # Count total copies of duplicate episodes per show
        # (sum of all copies for season+episode combos that have >1 file)
        cursor.execute('''
            SELECT imdb_id, COALESCE(SUM(ep_count), 0) AS cnt
            FROM (
                SELECT imdb_id, COUNT(*) AS ep_count
                FROM media_items
                WHERE type = 'episode'
                  AND state IN ('Collected', 'Upgrading')
                  AND imdb_id IS NOT NULL
                GROUP BY imdb_id, season_number, episode_number
                HAVING COUNT(*) > 1
            )
            GROUP BY imdb_id
        ''')
        show_version_counts = {r['imdb_id']: r['cnt'] for r in cursor.fetchall()}

        # Movies: scope by ms_item_id so split Plex items (unique ratingKey per version)
        # each get their own count of 1 rather than being grouped with other versions
        # of the same movie. Merged items (same ms_item_id) still get the correct count.
        cursor.execute('''
            SELECT ms_item_id, COUNT(*) AS cnt
            FROM media_items
            WHERE type = 'movie'
              AND state IN ('Collected', 'Upgrading')
              AND ms_item_id IS NOT NULL AND ms_item_id != ''
            GROUP BY ms_item_id
        ''')
        movie_version_counts = {r['ms_item_id']: r['cnt'] for r in cursor.fetchall()}

        # Pre-fetch show statuses using the same source and normalization as
        # overlay_manager._fetch_show_status so the hashes always agree.
        # Using DirectAPI / cli_battery was wrong: it's a different DB with raw
        # values ('returning series', 'continuing') that don't match the normalized
        # values stored by the overlay ('Returning', 'Airing') → perpetual mismatch.
        def _normalise_status(raw_status: str, has_upcoming: bool = False) -> str:
            """Same logic as overlay_manager._fetch_show_status._normalise."""
            raw = raw_status.strip().lower()
            if raw in ('returning series', 'returning') and has_upcoming:
                return 'Airing'
            if raw in ('returning series', 'returning'):
                return 'Returning'
            if raw == 'ended':
                return 'Ended'
            if raw in ('canceled', 'cancelled'):
                return 'Canceled'
            return raw_status.strip().title()

        cursor.execute('''
            SELECT ts.imdb_id,
                   ts.status,
                   EXISTS(
                       SELECT 1 FROM media_items mi
                       WHERE mi.imdb_id = ts.imdb_id
                         AND mi.type = 'episode'
                         AND date(mi.release_date) BETWEEN date('now') AND date('now', '+14 days')
                       LIMIT 1
                   ) AS has_upcoming
            FROM tv_shows ts
            WHERE ts.status IS NOT NULL AND ts.status != ''
        ''')
        show_status_map = {
            r['imdb_id']: _normalise_status(r['status'], bool(r['has_upcoming']))
            for r in cursor.fetchall()
        }

        # Items to check for rating changes this run (gated by interval)
        cursor.execute('''
            SELECT value FROM overlay_sync_state WHERE key = 'last_content_check_at'
        ''')
        row = cursor.fetchone()
        last_check_ts = float(row['value']) if row else 0.0
        now_ts = time.time()
        do_rating_check = (now_ts - last_check_ts) >= (interval_days * 86400)

        # IDs eligible for rating check this batch (oldest last_content_hash first)
        rating_check_ids = set()
        if do_rating_check:
            cursor.execute('''
                SELECT o.media_item_id
                FROM media_overlay_state o
                JOIN media_items m ON m.id = o.media_item_id
                WHERE o.status = 'applied'
                  AND m.imdb_id IS NOT NULL AND m.imdb_id != ''
                ORDER BY o.updated_at ASC
                LIMIT ?
            ''', (batch_size,))
            rating_check_ids = {r['media_item_id'] for r in cursor.fetchall()}

        # Fetch cached ratings from OverlayManager's in-process cache when available;
        # fall back to MDBList API only for items in the rating-check batch.
        ratings_cache: dict = OverlayManager._ratings_cache  # shared in-process cache

        def _get_ratings(imdb_id: str, do_fetch: bool, item_type: str = '') -> dict:
            import json
            import requests

            # ── 1. In-memory cache ────────────────────────────────────────────
            cached = ratings_cache.get(imdb_id)
            if cached:
                ts, data = cached
                if time.time() - ts < 86400:
                    return data

            # ── 2. SQLite persistent cache (survives restarts, 7-day TTL) ────
            _RATINGS_TTL = 7 * 86400
            try:
                from database.core import get_db_connection
                from utilities.settings import get_setting as _gs
                _trakt_configured = bool(_gs('Trakt', 'client_id', ''))
                _in_backoff = time.time() < OverlayManager._trakt_backoff_until
                _db = get_db_connection()
                _row = _db.execute(
                    'SELECT ratings, fetched_at FROM overlay_ratings_cache WHERE imdb_id = ?',
                    (imdb_id,)
                ).fetchone()
                _db.close()
                if _row and (time.time() - _row[1]) < _RATINGS_TTL:
                    data = json.loads(_row[0])
                    # Bypass cache only if Trakt is configured, trakt_rating is missing,
                    # AND we're not currently in a backoff window. If in backoff, serve
                    # the stale cache to avoid spurious content-hash resets.
                    if _trakt_configured and not data.get('trakt_rating') and not _in_backoff:
                        logger.debug(
                            f"SQLite cache for {imdb_id} has no trakt_rating; will re-fetch")
                    else:
                        ratings_cache[imdb_id] = (_row[1], data)
                        return data
            except Exception as _ce:
                logger.debug(f"SQLite ratings cache read failed for {imdb_id}: {_ce}")

            if not do_fetch:
                return {}

            def _persist(rat: dict) -> None:
                try:
                    from database.core import get_db_connection as _gdc
                    _c = _gdc()
                    _c.execute(
                        'INSERT OR REPLACE INTO overlay_ratings_cache (imdb_id, ratings, fetched_at) '
                        'VALUES (?, ?, ?)',
                        (imdb_id, json.dumps(rat), time.time())
                    )
                    _c.commit()
                    _c.close()
                except Exception as _w:
                    logger.debug(f"SQLite ratings cache write failed for {imdb_id}: {_w}")

            # ── 3. MDBList (primary source) ───────────────────────────────────
            mdblist_ok = False
            result = {}
            try:
                from utilities.mdblist_api import get_mdblist_api_key, is_mdblist_configured
                if is_mdblist_configured():
                    time.sleep(0.2)  # proactive throttle
                    resp = requests.get(
                        'https://mdblist.com/api/',
                        params={'apikey': get_mdblist_api_key(), 'i': imdb_id},
                        timeout=5
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if not data.get('response', True):
                        logger.warning(f"MDBList API error for {imdb_id}: {data.get('error', 'Unknown error')}")
                    else:
                        for r in data.get('ratings', []):
                            src = (r.get('source') or '').lower()
                            val = r.get('value')
                            if val is None:
                                continue
                            v = float(val)
                            if src == 'imdb':
                                result['imdb_rating'] = round(v, 1)
                            elif src == 'tmdb':
                                result['tmdb_rating'] = round(v / 10 if v > 10 else v, 1)
                            elif src == 'trakt':
                                result['trakt_rating'] = round(v / 10 if v > 10 else v, 1)
                            elif src == 'tomatoes':
                                result['rt_critics_score'] = round(v)
                            elif src in ('popcorn', 'tomatoesaudience'):
                                result['rt_user_score'] = round(v)
                        mdblist_ok = True
            except Exception:
                pass

            if mdblist_ok:
                # Supplement missing IMDb rating from dataset
                if not result.get('imdb_rating'):
                    try:
                        from overlays.imdb_dataset import get_imdb_dataset_rating
                        ds = get_imdb_dataset_rating(imdb_id)
                        if ds is not None:
                            result['imdb_rating'] = ds
                    except Exception:
                        pass
                ratings_cache[imdb_id] = (time.time(), result)
                _persist(result)
                return result

            # ── 4. Trakt fallback ─────────────────────────────────────────────
            # Track whether Trakt was skipped due to rate-limit backoff.
            # If so, don't persist to SQLite — let the item be retried on the
            # next sync run rather than caching an incomplete result for 7 days.
            trakt_skipped_backoff = False
            if not result.get('trakt_rating'):
                try:
                    from utilities.settings import get_setting
                    client_id = get_setting('Trakt', 'client_id', '')
                    if client_id:
                        if time.time() < OverlayManager._trakt_backoff_until:
                            trakt_skipped_backoff = True
                            logger.debug(
                                f"Trakt backoff active for {imdb_id}, skipping "
                                f"{OverlayManager._trakt_backoff_until - time.time():.0f}s")
                        else:
                            elapsed = time.time() - OverlayManager._trakt_last_call
                            if elapsed < 0.3:
                                time.sleep(0.3 - elapsed)
                            OverlayManager._trakt_last_call = time.time()

                            endpoint = 'shows' if item_type == 'episode' else 'movies'
                            trakt_resp = requests.get(
                                f'https://api.trakt.tv/{endpoint}/{imdb_id}?extended=full',
                                headers={
                                    'Content-Type': 'application/json',
                                    'trakt-api-version': '2',
                                    'trakt-api-key': client_id,
                                },
                                timeout=5
                            )
                            if trakt_resp.status_code == 200:
                                trakt_data = trakt_resp.json()
                                trakt_rating = trakt_data.get('rating')
                                if trakt_rating is not None:
                                    result['trakt_rating'] = round(float(trakt_rating), 1)
                                    logger.debug(f"Trakt fallback rating for {imdb_id}: {result['trakt_rating']}")
                            elif trakt_resp.status_code == 429:
                                retry_after = int(trakt_resp.headers.get('Retry-After', 60))
                                OverlayManager._trakt_backoff_until = time.time() + retry_after
                                trakt_skipped_backoff = True
                                logger.warning(
                                    f"Trakt rate-limited (429) for {imdb_id}; "
                                    f"backing off {retry_after}s")
                except Exception as e:
                    logger.debug(f"Trakt fallback failed for {imdb_id}: {e}")

            # Supplement missing IMDb rating from dataset
            if not result.get('imdb_rating'):
                try:
                    from overlays.imdb_dataset import get_imdb_dataset_rating
                    ds = get_imdb_dataset_rating(imdb_id)
                    if ds is not None:
                        result['imdb_rating'] = ds
                except Exception:
                    pass

            if result:
                ratings_cache[imdb_id] = (time.time(), result)
                # Only persist when the result is complete — skip SQLite write
                # if Trakt was rate-limited so the item is retried next sync.
                if not trakt_skipped_backoff:
                    _persist(result)
            return result

        reset_ids = []
        for row in applied:
            mid = row['media_item_id']
            stored_hash = row['last_content_hash']
            item_type = row['type']
            imdb_id = row['imdb_id'] or ''
            plex_key = row['ms_item_id'] or ''

            # Version count — shows keyed by imdb_id, movies keyed by ms_item_id.
            # Shows: default 0 (no duplicate episodes).
            # Movies: scope to ms_item_id so split Plex items (different ms_item_id)
            # each get count=1 independently. Merged items (same ms_item_id, multiple
            # DB rows) still get the correct count > 1. Falls back to 1 when ms_item_id
            # is not set (item not yet synced).
            if item_type == 'episode':
                vc = show_version_counts.get(imdb_id, 0)
            else:
                vc = movie_version_counts.get(plex_key, 1) if plex_key else 1

            # Ratings: only fetch/use when this item is in the current batch OR
            # already in the in-process cache. If we have no ratings at all, skip
            # this item — computing an all-None hash and comparing it against a
            # stored hash that contains real ratings would cause a spurious reset
            # on every sync run when the cache is cold.
            fetch_ratings = mid in rating_check_ids
            ratings = _get_ratings(imdb_id, fetch_ratings, item_type) if imdb_id else {}

            if not ratings:
                continue

            # Status (show only) — use pre-fetched tv_shows map with same normalization
            # as overlay_manager._fetch_show_status. Reading from DirectAPI/cli_battery
            # was wrong: different DB, raw values ('returning series') vs stored normalized
            # values ('Returning') → perpetual hash mismatch resetting shows every sync.
            status_val = show_status_map.get(imdb_id) if item_type == 'episode' and imdb_id else None

            current_hash = _content_hash(
                imdb_rating=ratings.get('imdb_rating'),
                tmdb_rating=ratings.get('tmdb_rating'),
                trakt_rating=ratings.get('trakt_rating'),
                rt_critics_score=ratings.get('rt_critics_score'),
                rt_user_score=ratings.get('rt_user_score'),
                status=status_val,
                version_count=vc,
            )

            if current_hash != stored_hash:
                reset_ids.append(mid)
                logger.debug(
                    f"Content change for item {mid} imdb={imdb_id} "
                    f"({stored_hash[:8]}→{current_hash[:8]}), resetting to pending"
                )

        if reset_ids:
            cursor.executemany(
                "UPDATE media_overlay_state SET status='pending', "
                "reason='Content metadata changed — overlay needs refresh', "
                "updated_at=CURRENT_TIMESTAMP WHERE media_item_id=?",
                [(i,) for i in reset_ids]
            )
            conn.commit()
            total_reset += len(reset_ids)
            logger.info(f"Content-change check: reset {len(reset_ids)} item(s) to pending")

        # ── 2. Season content hash check ──────────────────────────────────────
        cursor.execute('''
            SELECT season_ms_item_id, last_content_hash, show_ms_item_id
            FROM season_overlay_state
            WHERE status = 'applied'
              AND last_content_hash IS NOT NULL
              AND last_content_hash != ''
        ''')
        applied_seasons = cursor.fetchall()

        # For seasons: build show_ms_item_id → imdb_id mapping by querying
        # media_items directly. The applied[] rows contain episode ms_item_ids
        # (each episode's own key), NOT the show-level key stored in
        # season_overlay_state.show_ms_item_id — so we must query the DB.
        applied_show_keys = {srow['show_ms_item_id'] for srow in applied_seasons
                             if srow['show_ms_item_id']}
        show_key_to_imdb = {}
        if applied_show_keys:
            placeholders = ','.join('?' * len(applied_show_keys))
            cursor.execute(f'''
                SELECT DISTINCT ms_item_id, imdb_id
                FROM media_items
                WHERE ms_item_id IN ({placeholders})
                  AND type = 'episode'
                  AND imdb_id IS NOT NULL AND imdb_id != ''
            ''', list(applied_show_keys))
            for r in cursor.fetchall():
                if r['ms_item_id'] and r['imdb_id']:
                    show_key_to_imdb[r['ms_item_id']] = r['imdb_id']

        season_reset_keys = []
        for srow in applied_seasons:
            season_key = srow['season_ms_item_id']
            stored_hash = srow['last_content_hash']
            show_key = srow['show_ms_item_id'] or ''
            imdb_id = show_key_to_imdb.get(show_key, '')

            # Only check seasons when we're doing a rating-check run AND we have an
            # imdb_id to fetch ratings for. Without fresh ratings we cannot compute a
            # meaningful hash to compare against the stored one — comparing an
            # all-None hash against a hash computed with real ratings would cause a
            # spurious reset every sync run.
            if not imdb_id or not do_rating_check:
                continue

            ratings = _get_ratings(imdb_id, True, 'episode')

            # Same fix as the show hash check — use pre-fetched tv_shows map.
            # DirectAPI/cli_battery returns raw status ('returning series') which
            # doesn't match the normalized value stored by the overlay ('Returning').
            status_val = show_status_map.get(imdb_id)

            current_hash = _content_hash(
                imdb_rating=ratings.get('imdb_rating'),
                tmdb_rating=ratings.get('tmdb_rating'),
                trakt_rating=ratings.get('trakt_rating'),
                rt_critics_score=ratings.get('rt_critics_score'),
                rt_user_score=ratings.get('rt_user_score'),
                status=status_val,
                version_count=None,  # seasons don't display version count badge
            )

            if current_hash != stored_hash:
                season_reset_keys.append(season_key)
                logger.debug(
                    f"Content change for season {season_key} "
                    f"({stored_hash[:8]}→{current_hash[:8]}), resetting to pending"
                )

        if season_reset_keys:
            cursor.executemany(
                "UPDATE season_overlay_state SET status='pending', "
                "reason='Content metadata changed — season overlay needs refresh', "
                "updated_at=CURRENT_TIMESTAMP WHERE season_ms_item_id=?",
                [(k,) for k in season_reset_keys]
            )
            conn.commit()
            total_reset += len(season_reset_keys)
            logger.info(f"Content-change check: reset {len(season_reset_keys)} season(s) to pending")

        # ── Update last_content_check_at timestamp if ratings were checked ────
        if do_rating_check and rating_check_ids:
            cursor.execute('''
                INSERT INTO overlay_sync_state (key, value, updated_at)
                VALUES ('last_content_check_at', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = CURRENT_TIMESTAMP
            ''', (str(now_ts),))
            conn.commit()

        conn.close()
        return total_reset

    except Exception as e:
        logger.error(f"_reset_content_changed_items failed: {e}", exc_info=True)
        return 0


def task_overlay_sync(triggered_by: str = 'scheduled'):
    """
    Periodic overlay sync task.

    Scans for Collected media items that need overlay generation and processes them:
    - Items with 'pending' or no status yet
    - Items that failed but retry_count < max_retries
    - Items that were analyzing and may now be ready

    For TV shows, all episodes share the same ms_item_id (the show key).
    The overlay is applied once per unique ms_item_id, then all matching
    episode rows are marked applied so they are not reprocessed.

    Automatically syncs ms_item_id for any new Collected items missing one
    before processing, so a manual "Sync Plex Library" is not required.

    This task should be run periodically (e.g., every 30 minutes).
    """
    if not get_setting('Overlay Settings', 'overlays_enabled', False):
        return {'success': False, 'message': 'Overlay system is disabled'}

    logger.info("Starting overlay sync task")
    _sync_start = time.time()

    # Kick off IMDb dataset prefetch in the background so it is warm before
    # the first _fetch_ratings call hits the fallback path.
    try:
        from overlays.imdb_dataset import prefetch_dataset
        prefetch_dataset()
    except Exception:
        pass

    try:
        if is_jellyfin_mode():
            jf_url   = get_jellyfin_url()
            jf_token = get_jellyfin_token()
            if not jf_token:
                logger.warning("Jellyfin token not configured, skipping overlay sync")
                return {'success': False, 'message': 'Jellyfin token not configured'}
            plex_url   = ''
            plex_token = jf_token  # passed to OverlayManager for backward compat signature
        else:
            plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
            plex_token = get_setting('Plex', 'token', default='')
            if not plex_token:
                logger.warning("Plex token not configured, skipping overlay sync")
                return {'success': False, 'message': 'Plex token not configured'}

        # Step 1: Auto-populate ms_item_id for any new Collected items
        _sync_ms_keys_auto()

        # Step 1.1: Sibling cleanup — mark orphaned pending episode rows as applied
        # where the episode was NEVER individually applied (overlay_applied_at IS NULL)
        # but its show already has applied siblings. These rows were created before the
        # sibling-marking fix and will never clear on their own because NULL-episode
        # shows fill the sync limit ahead of them in the processing queue.
        try:
            _sc_conn = _get_db_connection()
            _sc_conn.execute('''
                UPDATE media_overlay_state
                SET status = 'applied',
                    reason = 'Marked applied via sibling cleanup',
                    overlay_applied_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'pending'
                  AND overlay_applied_at IS NULL
                  AND media_item_id IN (
                    SELECT m.id FROM media_items m
                    WHERE m.type = 'episode'
                      AND m.ms_item_id IN (
                        SELECT DISTINCT m2.ms_item_id
                        FROM media_items m2
                        JOIN media_overlay_state o2 ON m2.id = o2.media_item_id
                        WHERE m2.type = 'episode'
                          AND o2.status = 'applied'
                      )
                  )
            ''')
            _sc_count = _sc_conn.total_changes
            _sc_conn.commit()
            _sc_conn.close()
            if _sc_count:
                logger.info(f"Sibling cleanup: marked {_sc_count} orphaned pending episode(s) applied")
        except Exception as _sc_err:
            logger.warning(f"Sibling cleanup failed: {_sc_err}")

        # Step 1.5: Reset 'applied' items whose quality improved since the overlay
        # was last generated (e.g. a 4K episode was added to a 1080p show).
        # Resets them to 'pending' so Step 2 picks them up naturally.
        quality_reset_count = _reset_quality_changed_items()
        if quality_reset_count:
            logger.info(f"Quality-change check: reset {quality_reset_count} item(s) to pending (quality upgraded)")

        # Step 1.8: Reset 'applied' items whose content metadata (ratings, status,
        # version count) has changed. Ratings are checked at most once per N days
        # (overlay_content_check_interval_days setting, default 7) in batches of 200
        # to avoid cold-cache API hammering. Version count and status are pure-SQL
        # and run every sync.
        content_reset_count = _reset_content_changed_items()
        if content_reset_count:
            logger.info(f"Content-change check: reset {content_reset_count} item(s) to pending (ratings/status/version changed)")

        # Step 1.9: Detect deselected overlay posters by comparing stored thumb URLs
        # against Plex's current thumb via a 2-call bulk fetch (one per library section).
        # If the timestamp suffix of the thumb URL changed, the selected poster changed
        # in Plex — reset that item to 'pending' so the overlay gets re-applied.
        try:
            from overlays.plex_client import PlexClient as _PlexClient
            _pc = _PlexClient(plex_url, plex_token)
            _sections_resp = _pc.session.get(
                f"{plex_url}/library/sections",
                params={'X-Plex-Token': plex_token},
                headers={'Accept': 'application/xml'}, timeout=10)
            if _sections_resp.status_code == 200:
                import xml.etree.ElementTree as _ET
                _sec_root = _ET.fromstring(_sections_resp.content)
                _section_ids = [s.get('key') for s in _sec_root if s.get('key')]
                # Bulk fetch thumb URLs for all sections
                _current_thumbs = {}  # rating_key -> thumb_path
                for _sid in _section_ids:
                    _current_thumbs.update(_pc.get_bulk_thumb_urls(_sid))
                if _current_thumbs:
                    # Find applied items with a stored plex_thumb_url
                    _th_conn = _get_db_connection()
                    _th_rows = _th_conn.execute('''
                        SELECT mos.media_item_id, mos.plex_thumb_url, mi.ms_item_id
                        FROM media_overlay_state mos
                        JOIN media_items mi ON mi.id = mos.media_item_id
                        WHERE mos.status = 'applied'
                          AND mos.plex_thumb_url IS NOT NULL
                          AND mi.ms_item_id IS NOT NULL
                          AND mos.overlay_applied_at < datetime('now', '-10 minutes')
                    ''').fetchall()
                    _thumb_reset_ids = []
                    _seen_ms = set()
                    for _row in _th_rows:
                        _ms_id = str(_row[2])
                        if _ms_id in _seen_ms:
                            continue
                        _stored = _row[1]
                        _current = _current_thumbs.get(_ms_id)
                        if _current and _stored and _current != _stored:
                            _thumb_reset_ids.append(_row[0])
                            _seen_ms.add(_ms_id)
                    if _thumb_reset_ids:
                        _th_conn.execute(
                            f"UPDATE media_overlay_state SET status='pending', reason='Poster deselected in Plex (thumb changed)', updated_at=CURRENT_TIMESTAMP WHERE media_item_id IN ({','.join('?' * len(_thumb_reset_ids))})",
                            _thumb_reset_ids)
                        _th_conn.commit()
                        logger.info(f"[ThumbCheck] Reset {len(_thumb_reset_ids)} item(s) to pending — poster changed in Plex")
                    _th_conn.close()
        except Exception as _te:
            logger.debug(f"[ThumbCheck] Thumb deselection check failed (non-fatal): {_te}")

        # Step 1.6: One-pass season backfill — register seasons for any shows that
        # are already 'applied' but have no season_overlay_state rows yet.
        # This fixes existing libraries where shows were applied before season overlays
        # existed, or before a season layout was configured.
        manager = OverlayManager(None, plex_url, plex_token)
        backfill_count = _backfill_season_registrations(manager)
        if backfill_count:
            logger.info(f"Season backfill: {backfill_count} season(s) queued for processing")

        # Step 1.7: Reset 'user_removed' season rows back to 'pending' when a season
        # layout is active.  'user_removed' is only meant for explicit per-item UI
        # removals; a batch "remove all overlays" incorrectly sets this status and
        # would otherwise permanently block re-application on the next sync.
        # Also fixes newly-added shows whose seasons have 'user_removed' rows from a
        # prior batch removal — INSERT OR IGNORE in _register_show_seasons_pending
        # skips those rows, so we must reset them here.
        try:
            _season_layouts = manager.layout_mgr.list_layouts(media_type='season', active_only=True)
            if _season_layouts:
                _conn = _get_db_connection()
                _cur = _conn.cursor()
                _cur.execute(
                    "UPDATE season_overlay_state SET status = 'pending', updated_at = CURRENT_TIMESTAMP "
                    "WHERE status = 'user_removed'"
                )
                _reset_count = _cur.rowcount
                _conn.commit()
                _conn.close()
                if _reset_count:
                    logger.info(
                        f"Step 1.7: Reset {_reset_count} 'user_removed' season(s) to 'pending' "
                        f"(batch-removal reset — season layout is active)"
                    )
        except Exception as _e17:
            logger.warning(f"Step 1.7 user_removed reset failed: {_e17}")

        # Step 2: Find Collected items that need overlay generation.
        #
        # TV deduplication: episodes all share the same ms_item_id (the show's
        # key). We use MIN(m.id) to pick one representative episode per show, then
        # after a successful overlay upload we mark ALL episodes of that show as
        # applied. Movies have unique keys so they are unaffected.
        conn = _get_db_connection()
        cursor = conn.cursor()

        # In Jellyfin mode, exclude legacy short Plex integer IDs (LENGTH < 20).
        # In Plex mode, Plex rating keys are short numeric strings so no length filter.
        _ms_id_filter = "AND LENGTH(m.ms_item_id) >= 20" if is_jellyfin_mode() else ""
        _sync_limit = int(get_setting('Overlay Settings', 'sync_items_per_run', 200))

        cursor.execute(f'''
            SELECT
                -- For episodes, prefer an ID that is NOT yet applied (NULL or
                -- actionable status) so generate_overlay_for_item won't skip it.
                -- Falls back to MIN(m.id) if all episodes in the group are applied.
                COALESCE(
                    MIN(CASE WHEN m.type = 'episode'
                                  AND (o.status IS NULL OR o.status != 'applied')
                             THEN m.id END),
                    MIN(m.id)
                ) AS id,
                m.ms_item_id,
                m.type,
                MAX(m.title) AS title,
                -- Aggregate status: what is the "best" (most actionable) status across
                -- all episodes in this show group?
                CASE
                    WHEN MAX(CASE WHEN o.status = 'applied'   THEN 1 ELSE 0 END) = 1 THEN 'applied'
                    WHEN MAX(CASE WHEN o.status = 'analyzing' THEN 1 ELSE 0 END) = 1 THEN 'analyzing'
                    WHEN MAX(CASE WHEN o.status = 'pending'   THEN 1 ELSE 0 END) = 1 THEN 'pending'
                    WHEN MAX(CASE WHEN o.status = 'removed'   THEN 1 ELSE 0 END) = 1 THEN 'removed'
                    WHEN MAX(CASE WHEN o.status = 'failed'    THEN 1 ELSE 0 END) = 1 THEN 'failed'
                    ELSE NULL
                END AS status,
                MAX(o.retry_count) AS retry_count,
                MIN(o.last_retry)  AS last_retry,
                -- Non-NULL if the show/movie was ever successfully applied before.
                -- Used to decide whether to force a fresh poster on re-render.
                MAX(o.overlay_applied_at) AS overlay_applied_at
            FROM media_items m
            LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE m.ms_item_id IS NOT NULL
              {_ms_id_filter}
              AND m.state IN ('Collected', 'Upgrading')
            GROUP BY
                CASE WHEN m.type = 'episode' THEN m.ms_item_id ELSE CAST(m.id AS TEXT) END
            HAVING
                (
                    -- Episodes: process whenever any episode in the show group has an
                    -- actionable status (NULL, pending, removed, stuck-analyzing, retryable-failed).
                    -- The MAX(applied)=0 gate is intentionally absent — quality/content resets
                    -- set individual episodes to 'pending' while others stay 'applied', so the
                    -- gate would permanently hide those shows. After processing, _mark_all_episodes_applied
                    -- marks the full group applied, preventing re-queuing next run.
                    m.type = 'episode'
                    AND (
                        MAX(CASE WHEN o.status IS NULL    THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'removed' THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'analyzing' AND o.updated_at < datetime('now', '-30 minutes') THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'failed' AND o.retry_count < 5 AND (o.last_retry IS NULL OR o.last_retry < datetime('now', '-1 hour')) THEN 1 ELSE 0 END) = 1
                    )
                )
                OR (
                    -- Movies: process when untracked (NULL), pending, removed, stuck-analyzing, or retryable-failed.
                    m.type != 'episode'
                    AND (
                        MAX(CASE WHEN o.status IS NULL     THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'removed' THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'analyzing' AND o.updated_at < datetime('now', '-30 minutes') THEN 1 ELSE 0 END) = 1
                        OR MAX(CASE WHEN o.status = 'failed' AND o.retry_count < 5 AND (o.last_retry IS NULL OR o.last_retry < datetime('now', '-1 hour')) THEN 1 ELSE 0 END) = 1
                    )
                )
            ORDER BY
                CASE
                    -- NULL (never processed) and pending (needs refresh) are equal priority
                    WHEN MAX(CASE WHEN o.status IS NULL OR o.status = 'pending' THEN 1 ELSE 0 END) = 1 THEN 1
                    WHEN MAX(CASE WHEN o.status = 'removed'   THEN 1 ELSE 0 END) = 1 THEN 2
                    WHEN MAX(CASE WHEN o.status = 'analyzing' THEN 1 ELSE 0 END) = 1 THEN 3
                    WHEN MAX(CASE WHEN o.status = 'failed'    THEN 1 ELSE 0 END) = 1 THEN 4
                    ELSE 5
                END,
                MIN(m.id)
            LIMIT {_sync_limit}
        ''')

        items_to_process = cursor.fetchall()
        conn.close()

        logger.info(f"Found {len(items_to_process)} item(s) to process (after TV deduplication)")

        # Step 3: Process items (manager already initialised in Step 1.6)
        # Note: do NOT early-return when items_to_process is empty — Step 5 (season
        # overlays) must always run so pending seasons are not skipped when all show/
        # movie items are already applied.
        total = applied = failed = skipped = analyzing = stale_reset = 0
        failure_details = []  # [(title, reason), ...]

        for row in items_to_process:
            item_id = row['id']
            ms_item_id = row['ms_item_id']
            item_type = row['type']
            item_title = row['title'] or ms_item_id
            total += 1

            # Use a fresh poster (from TMDB, not Plex) when re-rendering a
            # previously-applied item. Plex's current poster is the overlaid
            # version we uploaded, so downloading from Plex would produce
            # overlay-on-overlay. force_fresh_poster deletes the local backup
            # and forces a clean TMDB download as the new base.
            was_previously_applied = (
                row['overlay_applied_at'] is not None
                or row['status'] == 'applied'  # TV show: some episodes still applied
            )
            result = manager.generate_overlay_for_item(
                item_id, force=False, force_fresh_poster=was_previously_applied)
            status = result.get('status', 'failed')

            if status == 'applied':
                applied += 1
                # For TV shows: mark ALL episodes sharing this ms_item_id as
                # applied so they are not reprocessed on the next sync run.
                if item_type == 'episode':
                    _mark_all_episodes_applied(ms_item_id, result.get('content_hash'))
                    # Auto-register seasons so Step 5 picks them up for season overlays.
                    _register_show_seasons_pending(manager, ms_item_id)
            elif status == 'pending':
                # generate_overlay_for_item returns 'pending' when it detected a
                # stale ms_item_id (404) and reset it. Track so we can
                # re-sync and retry in the same run below.
                stale_reset += 1
            elif status == 'analyzing':
                analyzing += 1
            elif status == 'skipped':
                skipped += 1
            else:
                failed += 1
                reason = result.get('message') or result.get('error') or status
                failure_details.append(f"{item_title}: {reason}")

        # Step 4: If any stale keys were reset, re-run the media server library sync to
        # re-discover their new item IDs, then do a second processing pass.
        if stale_reset > 0:
            logger.info(
                f"{stale_reset} stale ms_item_id(s) reset — re-running media server library sync "
                f"to re-discover new keys"
            )
            _sync_ms_keys_auto()

            # Find items that were just re-keyed (status=pending, key now populated)
            conn = _get_db_connection()
            cursor = conn.cursor()
            _s4_ms_id_filter = "AND LENGTH(m.ms_item_id) >= 20" if is_jellyfin_mode() else ""
            cursor.execute(f'''
                SELECT
                    MIN(m.id) AS id,
                    m.ms_item_id,
                    m.type,
                    MAX(m.title) AS title
                FROM media_items m
                JOIN media_overlay_state o ON m.id = o.media_item_id
                WHERE m.ms_item_id IS NOT NULL
                  {_s4_ms_id_filter}
                  AND m.state IN ('Collected', 'Upgrading')
                  AND o.status = 'pending'
                GROUP BY
                    CASE WHEN m.type = 'episode' THEN m.ms_item_id ELSE CAST(m.id AS TEXT) END
                LIMIT 50
            ''')
            retry_items = cursor.fetchall()
            conn.close()

            logger.info(f"Retrying {len(retry_items)} item(s) with freshly-synced ms_item_ids")
            for row in retry_items:
                result = manager.generate_overlay_for_item(row['id'], force=False)
                status = result.get('status', 'failed')
                total += 1
                if status == 'applied':
                    applied += 1
                    if row['type'] == 'episode':
                        _mark_all_episodes_applied(row['ms_item_id'], result.get('content_hash'))
                        _register_show_seasons_pending(manager, row['ms_item_id'])
                elif status == 'analyzing':
                    analyzing += 1
                elif status == 'skipped':
                    skipped += 1
                else:
                    failed += 1
                    reason = result.get('message') or result.get('error') or status
                    failure_details.append(f"{row['title'] or row['id']} (retry): {reason}")

        logger.info(
            f"Overlay sync complete: {applied} applied, {analyzing} analyzing, "
            f"{failed} failed, {skipped} skipped (of {total} processed)"
        )

        # Step 5: Process pending season overlays from season_overlay_state.
        # Seasons already registered (via manual trigger or previous sync) that need
        # re-generation due to quality change, removal, or initial 'pending' status.
        conn = _get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT s.show_ms_item_id, s.season_ms_item_id, s.season_number, s.status,
                   COALESCE(mi.title, '') AS show_title, COALESCE(mi.year, '') AS show_year
            FROM season_overlay_state s
            LEFT JOIN (
                SELECT ms_item_id, title, year
                FROM media_items
                WHERE type = 'episode'
                GROUP BY ms_item_id
            ) mi ON mi.ms_item_id = s.show_ms_item_id
            WHERE (s.status IN ('pending', 'removed')
               OR (s.status = 'analyzing' AND s.updated_at < datetime('now', '-30 minutes'))
               OR (s.status = 'failed' AND s.retry_count < 5
                   AND (s.last_retry IS NULL OR s.last_retry < datetime('now', '-1 hour'))))
               AND s.status != 'user_removed'
            ORDER BY
                CASE WHEN s.status = 'pending' THEN 1
                     WHEN s.status = 'removed' THEN 2
                     WHEN s.status = 'analyzing' THEN 3
                     ELSE 4 END
            LIMIT {_sync_limit}
        ''')
        seasons_to_process = cursor.fetchall()
        conn.close()

        season_applied = season_failed = season_skipped = 0
        if seasons_to_process:
            logger.info(f"Season overlay sync: processing {len(seasons_to_process)} season(s)")
            for srow in seasons_to_process:
                sresult = manager.generate_season_overlay(
                    show_plex_rating_key=srow['show_ms_item_id'],
                    season_plex_rating_key=srow['season_ms_item_id'],
                    season_number=srow['season_number'],
                    force=False,
                )
                sstatus = sresult.get('status', 'error')
                if sstatus == 'applied':
                    season_applied += 1
                elif sstatus == 'skipped':
                    season_skipped += 1
                else:
                    season_failed += 1
                    sreason = sresult.get('message') or sresult.get('error') or sstatus
                    show_label = srow['show_title'] or srow['show_ms_item_id']
                    if srow['show_year']:
                        show_label = f"{show_label} ({srow['show_year']})"
                    failure_details.append(
                        f"{show_label} S{srow['season_number']} ({srow['season_ms_item_id']}): {sreason}"
                    )
            logger.info(
                f"Season overlay sync complete: {season_applied} applied, "
                f"{season_skipped} skipped, {season_failed} failed"
            )

        _sync_stats = {
            'total': total, 'applied': applied, 'failed': failed,
            'skipped': skipped, 'seasons_applied': season_applied,
            'seasons_failed': season_failed, 'seasons_skipped': season_skipped,
        }
        if failure_details:
            _sync_stats['failures'] = failure_details
        total_failed = failed + season_failed
        log_activity(
            'overlay_sync',
            triggered_by=triggered_by,
            result='success' if total_failed == 0 else 'partial',
            title=f"Overlay sync: {applied} applied, {failed} failed, {skipped} skipped"
                  + (f", {season_applied} seasons applied" if seasons_to_process else ""),
            stats=_sync_stats,
            duration_seconds=int(time.time() - _sync_start),
        )
        return {
            'success': True,
            'message': 'Overlay sync completed',
            'total': total,
            'applied': applied,
            'analyzing': analyzing,
            'failed': failed,
            'skipped': skipped,
            'seasons_applied': season_applied,
            'seasons_failed': season_failed,
            'seasons_skipped': season_skipped,
        }

    except Exception as e:
        logger.error(f"Overlay sync task failed: {e}", exc_info=True)
        log_activity('overlay_sync', triggered_by=triggered_by, result='failed',
                     title=f"Overlay sync failed: {e}",
                     duration_seconds=int(time.time() - _sync_start))
        return {'success': False, 'message': str(e)}


def _mark_all_episodes_applied(ms_item_id: str, content_hash: str = None):
    """
    Mark all episode rows sharing ms_item_id as 'applied' in media_overlay_state.

    Called after a successful show overlay upload to prevent the same show poster
    from being re-uploaded once per episode on subsequent sync runs.

    content_hash should be passed from the result of generate_overlay_for_item so
    that ALL episodes have last_content_hash populated.  Without this the content-
    change checker only sees the single representative episode (the one actually
    processed) and resets it to pending while the remaining episodes stay 'applied',
    causing the HAVING clause in task_overlay_sync to skip the show indefinitely.
    """
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Find all episode IDs with this show ms_item_id
        cursor.execute('''
            SELECT id FROM media_items
            WHERE ms_item_id = ?
              AND type = 'episode'
              AND state IN ('Collected', 'Upgrading')
        ''', (ms_item_id,))
        episode_ids = [r['id'] for r in cursor.fetchall()]

        now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

        # Batch upsert — single executemany instead of one execute per episode.
        # Include last_content_hash so the content-change checker has a stored hash
        # for every episode, not just the representative one.
        cursor.executemany('''
            INSERT INTO media_overlay_state (media_item_id, status, reason, overlay_applied_at, updated_at, created_at, last_content_hash)
            VALUES (?, 'applied', 'Applied via show-level overlay sync', ?, ?, ?, ?)
            ON CONFLICT(media_item_id) DO UPDATE SET
                status = 'applied',
                reason = 'Applied via show-level overlay sync',
                overlay_applied_at = excluded.overlay_applied_at,
                updated_at = excluded.updated_at,
                last_content_hash = COALESCE(excluded.last_content_hash, last_content_hash)
        ''', [(ep_id, now_str, now_str, now_str, content_hash) for ep_id in episode_ids])

        conn.commit()
        conn.close()
        logger.debug(f"Marked {len(episode_ids)} episode(s) as applied for show key {ms_item_id}")
    except Exception as e:
        logger.error(f"Failed to mark episodes applied for {ms_item_id}: {e}")


def _backfill_season_registrations(manager: OverlayManager) -> int:
    """
    One-pass backfill: for every show that is 'applied' in media_overlay_state but
    has NO rows at all in season_overlay_state, register all its Plex seasons as
    'pending' so Step 5 will process them on this or the next sync run.

    Uses a batch Plex API call (one per library section) instead of one per show,
    so even 1000+ show libraries are registered in a handful of API calls.
    Uses INSERT OR IGNORE so existing season rows (any status) are never touched.
    Safe to call every sync — the inner query returns nothing once all shows are covered.
    """
    # Only bother if a season layout is active
    try:
        layouts = manager.layout_mgr.list_layouts(media_type='season', active_only=True)
        if not layouts:
            return 0
    except Exception:
        return 0

    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Find show ms_item_ids where the show has at least one episode that is
        # 'applied' in media_overlay_state AND the show has zero season rows at all.
        # Uses EXISTS instead of NOT IN to handle NULLs correctly.
        # In Jellyfin mode: require LENGTH >= 20 to exclude legacy Plex integer keys
        # that haven't been synced to Jellyfin UUIDs yet.
        # In Plex mode: no length check — integer IDs are valid and are all < 20 chars.
        _id_len_filter = "AND LENGTH(m.ms_item_id) >= 20" if is_jellyfin_mode() else ""
        cursor.execute(f'''
            SELECT DISTINCT m.ms_item_id
            FROM media_items m
            WHERE m.type = 'episode'
              AND m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
              {_id_len_filter}
              AND m.state IN ('Collected', 'Upgrading')
              AND EXISTS (
                  SELECT 1 FROM media_overlay_state o2
                  WHERE o2.media_item_id = m.id
                    AND o2.status = 'applied'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM season_overlay_state s
                  WHERE s.show_ms_item_id = m.ms_item_id
              )
        ''')
        unregistered_keys = {r['ms_item_id'] for r in cursor.fetchall()}
        conn.close()

        if not unregistered_keys:
            return 0

        logger.info(
            f"Season backfill: {len(unregistered_keys)} show(s) have no season rows — "
            f"fetching seasons via library batch API"
        )

        # Build a map: show_ms_item_id → [season dicts]
        show_seasons_map: dict = {}
        if is_jellyfin_mode():
            # Jellyfin: fetch seasons per show individually
            # (no batch "all sections" API equivalent in Jellyfin)
            for show_key in unregistered_keys:
                try:
                    seasons_raw = manager.client.get_show_seasons(show_key)
                    for season in (seasons_raw or []):
                        show_seasons_map.setdefault(show_key, []).append(season)
                except Exception as _se:
                    logger.debug(f"Season backfill: Jellyfin seasons fetch failed for {show_key}: {_se}")
        else:
            # Plex: use the batch API — one call per TV library section
            sections = manager.plex.get_all_sections()
            tv_sections = [s for s in sections if s.get('type') == 'show']
            for section in tv_sections:
                all_seasons = manager.plex.get_all_seasons_for_section(section['key'])
                for season in all_seasons:
                    show_key = season.get('parentRatingKey', '')
                    if show_key in unregistered_keys:
                        show_seasons_map.setdefault(show_key, []).append(season)

        # Insert pending rows for all matched shows
        registered = 0
        conn = _get_db_connection()
        cursor = conn.cursor()
        for show_key, seasons in show_seasons_map.items():
            for s in seasons:
                cursor.execute('''
                    INSERT OR IGNORE INTO season_overlay_state
                        (show_ms_item_id, season_ms_item_id, season_number, status)
                    VALUES (?, ?, ?, 'pending')
                ''', (show_key, s['ratingKey'], s.get('index', 0)))
            registered += len(seasons)
        conn.commit()
        conn.close()

        # Any unregistered shows not found in Plex sections (deleted/unavailable)
        # are silently skipped — they'll be retried on future syncs if they reappear.
        logger.info(
            f"Season backfill complete: registered {registered} season(s) across "
            f"{len(show_seasons_map)} show(s)"
        )
        return registered

    except Exception as e:
        logger.error(f"Season backfill failed: {e}", exc_info=True)
        return 0


def _get_seasons_for_immediate_render(manager: OverlayManager,
                                       show_ms_item_id: str) -> list:
    """
    Return the list of season dicts for *show_ms_item_id* if and only if
    there is at least one active 'season' layout configured.

    Returns an empty list when:
      - No active 'season' layout exists (season overlays are not in use).
      - The Plex API call fails or returns nothing.

    Each item in the returned list is a dict with at least 'ratingKey' and
    'index' keys, as returned by manager.client.get_show_seasons().
    """
    try:
        layouts = manager.layout_mgr.list_layouts(media_type='season', active_only=True)
        if not layouts:
            logger.debug(
                f"_get_seasons_for_immediate_render: no active 'season' layout — "
                f"skipping immediate season render for {show_ms_item_id}"
            )
            return []
        seasons = manager.client.get_show_seasons(show_ms_item_id)
        return seasons or []
    except Exception as e:
        logger.warning(
            f"_get_seasons_for_immediate_render: failed to get seasons for "
            f"{show_ms_item_id}: {e}"
        )
        return []


def _register_show_seasons_pending(manager: OverlayManager, show_ms_item_id: str,
                                    force_reset: bool = False):
    """
    Register all seasons of a show as 'pending' in season_overlay_state so that
    Step 5 of task_overlay_sync will apply season overlays on the same or next run.

    By default uses INSERT OR IGNORE so already-applied seasons are not re-queued.
    Pass force_reset=True to also reset already-applied seasons — use this when
    content changes (e.g. a duplicate episode added) require re-rendering all season
    posters to reflect the updated version count badge.

    Does nothing if no active 'season' layout is configured — avoids queuing
    seasons that would just be skipped every sync run.
    """
    try:
        # Only register if there is an active 'season' layout.
        # Season overlays never use the TV layout — if there's no season layout,
        # the overlay would just be skipped, so don't bother registering.
        layouts = manager.layout_mgr.list_layouts(media_type='season', active_only=True)
        if not layouts:
            logger.debug(
                f"No active 'season' layout — skipping season registration for {show_ms_item_id}"
            )
            return

        seasons = manager.client.get_show_seasons(show_ms_item_id)
        if not seasons:
            return
        conn = _get_db_connection()
        cursor = conn.cursor()
        for s in seasons:
            if force_reset:
                # Reset all seasons (including already-applied) to pending so content
                # changes (e.g. duplicate file added) propagate to season posters.
                cursor.execute('''
                    INSERT INTO season_overlay_state
                        (show_ms_item_id, season_ms_item_id, season_number, status)
                    VALUES (?, ?, ?, 'pending')
                    ON CONFLICT(season_ms_item_id) DO UPDATE SET
                        status = CASE WHEN season_overlay_state.status = 'user_removed' THEN 'user_removed'
                                      ELSE 'pending' END,
                        updated_at = CURRENT_TIMESTAMP
                ''', (show_ms_item_id, s['ratingKey'], s.get('index', 0)))
            else:
                # INSERT new rows as pending; also reset existing 'user_removed' rows —
                # those come from a prior batch "remove all" and should not permanently
                # block re-application when a new item is added for the same show.
                cursor.execute('''
                    INSERT INTO season_overlay_state
                        (show_ms_item_id, season_ms_item_id, season_number, status)
                    VALUES (?, ?, ?, 'pending')
                    ON CONFLICT(season_ms_item_id) DO UPDATE SET
                        status = CASE WHEN season_overlay_state.status = 'user_removed' THEN 'pending'
                                      ELSE season_overlay_state.status END,
                        updated_at = CASE WHEN season_overlay_state.status = 'user_removed' THEN CURRENT_TIMESTAMP
                                          ELSE season_overlay_state.updated_at END
                ''', (show_ms_item_id, s['ratingKey'], s.get('index', 0)))
        conn.commit()
        conn.close()
        logger.debug(f"Registered {len(seasons)} season(s) as pending for show {show_ms_item_id}"
                     + (" (force reset)" if force_reset else ""))
    except Exception as e:
        logger.warning(f"Failed to register seasons for show {show_ms_item_id}: {e}")


def task_overlay_cleanup(triggered_by: str = 'scheduled'):
    """
    Periodic overlay cleanup task — DB housekeeping + Plex poster pruning.

    - Remove overlay state for items no longer in media_items table
    - Remove state records older than 90 days for skipped/failed items
    - Reset retry count for persistently failed items after 7 days
    - Plex only: delete old uploaded poster versions from Plex bundle storage
      (skipped automatically in Jellyfin/Emby mode)

    A single activity log entry is written covering all operations.
    """
    if not get_setting('Overlay Settings', 'overlays_enabled', False):
        return {'success': False, 'message': 'Overlay system is disabled'}

    logger.info("Starting overlay cleanup task")
    _cleanup_start = time.time()

    try:
        conn = _get_db_connection()
        cursor = conn.cursor()

        # Remove orphaned overlay state records
        cursor.execute('''
            DELETE FROM media_overlay_state
            WHERE media_item_id NOT IN (SELECT id FROM media_items)
        ''')
        orphaned_count = cursor.rowcount

        # Remove old failed/skipped records (older than 90 days)
        cursor.execute('''
            DELETE FROM media_overlay_state
            WHERE status IN ('failed', 'skipped')
              AND updated_at < datetime('now', '-90 days')
        ''')
        old_records_count = cursor.rowcount

        # Reset retry count for failed items older than 7 days so they get another chance
        cursor.execute('''
            UPDATE media_overlay_state
            SET retry_count = 0,
                status = 'pending'
            WHERE status = 'failed'
              AND retry_count >= 5
              AND (last_retry IS NULL OR last_retry < datetime('now', '-7 days'))
        ''')
        reset_count = cursor.rowcount

        # --- Season overlay state cleanup ---
        # Remove season records where the show is no longer in media_items
        cursor.execute('''
            DELETE FROM season_overlay_state
            WHERE show_ms_item_id NOT IN (
                SELECT DISTINCT ms_item_id FROM media_items
                WHERE ms_item_id IS NOT NULL
            )
        ''')
        season_orphaned_count = cursor.rowcount

        # Remove seasons confirmed as no longer in the media server
        cursor.execute("DELETE FROM season_overlay_state WHERE status = 'removed'")
        season_removed_count = cursor.rowcount

        # Remove old failed/skipped season records (older than 90 days)
        cursor.execute('''
            DELETE FROM season_overlay_state
            WHERE status IN ('failed', 'skipped')
              AND updated_at < datetime('now', '-90 days')
        ''')
        season_old_count = cursor.rowcount

        # Delete stale 404 season rows (Plex ratingKey no longer exists) instead of
        # resetting them — they will be re-registered with the new ratingKey on next sync.
        cursor.execute('''
            DELETE FROM season_overlay_state
            WHERE status = 'failed'
              AND retry_count >= 5
              AND reason LIKE '%404%'
        ''')

        # Reset retry count for other persistently failed season items after 7 days
        cursor.execute('''
            UPDATE season_overlay_state
            SET retry_count = 0,
                status = 'pending'
            WHERE status = 'failed'
              AND retry_count >= 5
              AND (reason IS NULL OR reason NOT LIKE '%404%')
              AND (last_retry IS NULL OR last_retry < datetime('now', '-7 days'))
        ''')
        season_reset_count = cursor.rowcount

        # Retry no_poster seasons after 30 days in case user added artwork
        cursor.execute('''
            UPDATE season_overlay_state
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'no_poster'
              AND updated_at < datetime('now', '-30 days')
        ''')
        season_no_poster_reset = cursor.rowcount

        conn.commit()
        conn.close()

        total_removed = orphaned_count + old_records_count + season_orphaned_count + season_old_count + season_removed_count
        total_reset   = reset_count + season_reset_count + season_no_poster_reset
        logger.info(
            f"DB cleanup: {orphaned_count} orphaned removed, {old_records_count} old removed, "
            f"{reset_count} failed reset; seasons: {season_orphaned_count} orphaned, "
            f"{season_removed_count} removed, {season_old_count} old, "
            f"{season_reset_count} reset, {season_no_poster_reset} no_poster retried"
        )

        # ── Plex poster file cleanup (skipped in Jellyfin/Emby mode) ──────────
        import glob as _glob
        total_deleted = 0
        total_errors  = 0
        total_bytes   = 0

        if not is_jellyfin_mode():
            plex_data_path = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
            if not plex_data_path:
                logger.info("Plex poster cleanup: 'plex_data_path' not configured — skipping.")
            elif not os.path.isdir(plex_data_path):
                logger.warning(f"Plex poster cleanup: '{plex_data_path}' not accessible — skipping.")
            else:
                plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
                plex_token = get_setting('Plex', 'token', default='')
                if not plex_token:
                    logger.warning("Plex poster cleanup: Plex token not configured — skipping.")
                else:
                    try:
                        # ── Fast disk-walk approach: zero per-item API calls ────────────
                        # Collect the SHA1 hash of every currently-active overlay poster
                        # from the DB (stored when the overlay was applied).  Then walk
                        # the Plex bundle filesystem once and delete any uploaded poster
                        # file whose hash is NOT in the keep set.
                        #
                        # Falls back to the old API-per-item loop for items whose hash
                        # was not yet stored (overlays applied before this change).

                        _hconn = _get_db_connection()
                        _hcur  = _hconn.cursor()

                        # Active upload hashes from media_overlay_state
                        _hcur.execute('''
                            SELECT last_plex_upload_hash
                            FROM media_overlay_state
                            WHERE last_plex_upload_hash IS NOT NULL
                              AND last_plex_upload_hash != ''
                              AND status IN ('applied', 'pending')
                        ''')
                        _keep_hashes = {r[0] for r in _hcur.fetchall()}

                        # Active upload hashes from season_overlay_state
                        _hcur.execute('''
                            SELECT last_plex_upload_hash
                            FROM season_overlay_state
                            WHERE last_plex_upload_hash IS NOT NULL
                              AND last_plex_upload_hash != ''
                              AND status IN ('applied', 'pending')
                        ''')
                        _keep_hashes.update(r[0] for r in _hcur.fetchall())
                        _hconn.close()

                        # ── Collect hashes of active collection + smart collection posters
                        # so the disk-walk doesn't delete them.
                        try:
                            import requests as _rq_coll
                            _coll_headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}

                            # Regular collections from plex_collection_sync DB
                            _coll_rks = []
                            try:
                                _ccdb = _get_db_connection()
                                _ccrows = _ccdb.execute(
                                    'SELECT movie_collection_ratingkey, show_collection_ratingkey FROM plex_collection_sync'
                                ).fetchall()
                                _ccdb.close()
                                for _row in _ccrows:
                                    for _rk in [_row[0], _row[1]]:
                                        if _rk:
                                            _coll_rks.append(str(_rk))
                            except Exception as _cdb_e:
                                logger.debug(f"Collection hash pre-pass: DB read error: {_cdb_e}")

                            # Smart collection ratingKeys from config
                            try:
                                from utilities.settings import get_all_settings as _gas
                                _psc = _gas().get('Plex Smart Collections', {})
                                _sc_colls = _psc.get('collections', {}) if isinstance(_psc, dict) else {}
                                if not isinstance(_sc_colls, dict):
                                    _sc_colls = {}
                                for _sc_rk in _sc_colls.keys():
                                    _coll_rks.append(str(_sc_rk))
                            except Exception as _sc_e:
                                logger.debug(f"Collection hash pre-pass: smart collection read error: {_sc_e}")

                            # Fetch selected poster hash for each collection ratingKey
                            _coll_added = 0
                            for _crk in _coll_rks:
                                try:
                                    _pr = _rq_coll.get(
                                        f"{plex_url}/library/metadata/{_crk}/posters",
                                        headers=_coll_headers, timeout=10
                                    )
                                    if _pr.status_code != 200:
                                        continue
                                    for _pp in _pr.json().get('MediaContainer', {}).get('Metadata', []):
                                        if not _pp.get('selected'):
                                            continue
                                        _ppk = _pp.get('ratingKey', '')
                                        if _ppk.startswith('upload://posters/'):
                                            _ph = _ppk[len('upload://posters/'):]
                                            if _ph:
                                                _keep_hashes.add(_ph)
                                                _coll_added += 1
                                except Exception as _cre:
                                    logger.debug(f"Collection hash pre-pass error for rk={_crk}: {_cre}")

                            if _coll_rks:
                                logger.info(
                                    f"Plex poster cleanup: collection pre-pass added {_coll_added} hash(es) "
                                    f"from {len(_coll_rks)} collection(s) to keep set"
                                )

                            # Trakt/Adaptive collection upload hashes stored in plex_collection_state.json
                            try:
                                import json as _pcjson
                                _pc_state_path = os.path.join(
                                    os.environ.get('USER_CONFIG', '/user/config'), 'plex_collection_state.json'
                                )
                                if os.path.exists(_pc_state_path):
                                    with open(_pc_state_path, 'r') as _pcf:
                                        _pc_state = _pcjson.load(_pcf)
                                    _pc_added = 0
                                    for _pc_entry in _pc_state.values():
                                        if isinstance(_pc_entry, dict):
                                            for _pch in _pc_entry.get('plex_upload_hashes', {}).values():
                                                if _pch:
                                                    _keep_hashes.add(_pch)
                                                    _pc_added += 1
                                    if _pc_added:
                                        logger.info(
                                            f"Plex poster cleanup: collection state pre-pass added {_pc_added} hash(es) to keep set"
                                        )
                            except Exception as _pc_e:
                                logger.debug(f"Collection hash pre-pass: collection state read error: {_pc_e}")

                            # Box set collection poster hashes stored directly in state file
                            try:
                                import json as _bsjson
                                _bs_state_path = os.path.join(
                                    os.environ.get('USER_CONFIG', '/user/config'), 'plex_boxsets_state.json'
                                )
                                if os.path.exists(_bs_state_path):
                                    with open(_bs_state_path, 'r') as _bsf:
                                        _bs_state = _bsjson.load(_bsf)
                                    _bs_hashes = _bs_state.get('collection_poster_hashes', {})
                                    _bs_added = 0
                                    for _bsh in _bs_hashes.values():
                                        if _bsh:
                                            _keep_hashes.add(_bsh)
                                            _bs_added += 1
                                    if _bs_added:
                                        logger.info(
                                            f"Plex poster cleanup: boxsets pre-pass added {_bs_added} hash(es) to keep set"
                                        )
                            except Exception as _bs_e:
                                logger.debug(f"Collection hash pre-pass: boxsets state read error: {_bs_e}")


                        except Exception as _cp_e:
                            logger.warning(f"Plex poster cleanup: collection hash pre-pass failed: {_cp_e}")

                        # ── Pre-pass: discover upload hashes for applied/pending items
                        # BEFORE the disk-walk so their active overlay files are not
                        # accidentally deleted. Runs Plex API calls, stores the selected
                        # poster hash, and adds it to _keep_hashes.
                        from concurrent.futures import ThreadPoolExecutor, as_completed
                        from .plex_client import PlexClient
                        client = PlexClient(plex_url, plex_token, timeout=10)

                        _ppconn = _get_db_connection()
                        _ppcur  = _ppconn.cursor()
                        _ppcur.execute('''
                            SELECT DISTINCT m.ms_item_id
                            FROM media_items m
                            JOIN media_overlay_state o ON m.id = o.media_item_id
                            WHERE o.status IN ('applied', 'pending')
                              AND o.overlay_applied_at IS NOT NULL
                              AND (o.last_plex_upload_hash IS NULL OR o.last_plex_upload_hash = '')
                              AND m.ms_item_id IS NOT NULL
                              AND m.ms_item_id != ''
                        ''')
                        _pp_item_keys = [r[0] for r in _ppcur.fetchall()]
                        _ppcur.execute('''
                            SELECT DISTINCT season_ms_item_id
                            FROM season_overlay_state
                            WHERE status IN ('applied', 'pending')
                              AND overlay_applied_at IS NOT NULL
                              AND (last_plex_upload_hash IS NULL OR last_plex_upload_hash = '')
                              AND season_ms_item_id IS NOT NULL
                              AND season_ms_item_id != ''
                        ''')
                        _pp_season_keys = [r[0] for r in _ppcur.fetchall()]
                        _ppconn.close()

                        def _prepass_get_hash(rk):
                            try:
                                for _p in (client.get_poster_list(rk) or []):
                                    if _p.get('selected'):
                                        _pk = _p.get('ratingKey', '')
                                        if _pk.startswith('upload://posters/'):
                                            _h = _pk[len('upload://posters/'):]
                                            if _h:
                                                return rk, _h
                            except Exception as _ppe:
                                logger.debug(f"Pre-pass error for {rk}: {_ppe}")
                            return rk, None

                        _pp_found = {}         # ms_item_id -> hash
                        _pp_season_found = {}  # season_ms_item_id -> hash
                        if _pp_item_keys or _pp_season_keys:
                            logger.info(
                                f"Plex poster cleanup: pre-pass for "
                                f"{len(_pp_item_keys)} item(s) + {len(_pp_season_keys)} season(s) "
                                f"without stored hash — fetching before disk-walk"
                            )
                            _pp_total = len(_pp_item_keys) + len(_pp_season_keys)
                            _pp_done  = 0
                            with ThreadPoolExecutor(max_workers=10) as _pp_pool:
                                _pp_item_futs = {
                                    _pp_pool.submit(_prepass_get_hash, rk): rk
                                    for rk in _pp_item_keys
                                }
                                _pp_season_futs = {
                                    _pp_pool.submit(_prepass_get_hash, rk): rk
                                    for rk in _pp_season_keys
                                }
                                for _pp_fut in as_completed(
                                        list(_pp_item_futs) + list(_pp_season_futs)):
                                    _rk, _h = _pp_fut.result()
                                    _pp_done += 1
                                    if _pp_done % 100 == 0:
                                        logger.info(
                                            f"Plex poster cleanup: pre-pass progress "
                                            f"{_pp_done}/{_pp_total} "
                                            f"({len(_pp_found) + len(_pp_season_found)} hashes found so far)"
                                        )
                                    if _h:
                                        if _pp_fut in _pp_item_futs:
                                            _pp_found[_rk] = _h
                                        else:
                                            _pp_season_found[_rk] = _h

                            if _pp_found or _pp_season_found:
                                try:
                                    _pp_write = _get_db_connection()
                                    for _ms_id, _uhash in _pp_found.items():
                                        _pp_write.execute(
                                            """UPDATE media_overlay_state
                                                  SET last_plex_upload_hash = ?,
                                                      updated_at = CURRENT_TIMESTAMP
                                                WHERE media_item_id IN (
                                                    SELECT id FROM media_items WHERE ms_item_id = ?
                                                )
                                                  AND (last_plex_upload_hash IS NULL
                                                   OR last_plex_upload_hash = '')""",
                                            (_uhash, _ms_id)
                                        )
                                    for _sms_id, _suhash in _pp_season_found.items():
                                        _pp_write.execute(
                                            """UPDATE season_overlay_state
                                                  SET last_plex_upload_hash = ?,
                                                      updated_at = CURRENT_TIMESTAMP
                                                WHERE season_ms_item_id = ?
                                                  AND (last_plex_upload_hash IS NULL
                                                   OR last_plex_upload_hash = '')""",
                                            (_suhash, _sms_id)
                                        )
                                    _pp_write.commit()
                                    _pp_write.close()
                                    _keep_hashes.update(_pp_found.values())
                                    _keep_hashes.update(_pp_season_found.values())
                                    logger.info(
                                        f"Plex poster cleanup: pre-pass stored hashes for "
                                        f"{len(_pp_found)} item(s) + {len(_pp_season_found)} season(s), "
                                        f"added to disk-walk keep set"
                                    )
                                except Exception as _pp_err:
                                    logger.warning(f"Pre-pass hash storage failed: {_pp_err}")

                        if _keep_hashes:
                            logger.info(
                                f"Plex poster cleanup: disk-walk mode, "
                                f"{len(_keep_hashes)} active upload hash(es) to preserve"
                            )

                            # Walk Plex Metadata directory once; delete from any
                            # Uploads/posters/ folder whose filename is not in keep set.
                            # Season posters are stored inside the show bundle at
                            # Uploads/posters/seasons/{N}/{hash} — not at the top-level
                            # Uploads/posters/ — so we use 'in' to match all subdirs.
                            _metadata_root = os.path.join(plex_data_path, 'Metadata')
                            _uploads_posters_sep = os.path.join('Uploads', 'posters') + os.sep
                            _uploads_posters_end = os.path.join('Uploads', 'posters')
                            for _dirpath, _dirnames, _filenames in os.walk(_metadata_root):
                                if not (_dirpath.endswith(_uploads_posters_end)
                                        or _uploads_posters_sep in _dirpath):
                                    continue
                                for _fname in _filenames:
                                    # Only touch files whose name looks like a SHA1/hash
                                    # (40 hex chars or longer) — skip thumbnails, etc.
                                    _bare = os.path.basename(_fname)
                                    if len(_bare) < 20:
                                        continue
                                    if _bare in _keep_hashes:
                                        continue
                                    _fp = os.path.join(_dirpath, _fname)
                                    try:
                                        try:
                                            total_bytes += os.path.getsize(_fp)
                                        except OSError:
                                            pass
                                        os.remove(_fp)
                                        total_deleted += 1
                                        logger.debug(f"Plex poster cleanup: deleted '{_fp}'")
                                    except Exception as _de:
                                        logger.error(f"Plex poster cleanup: failed '{_fp}': {_de}")
                                        total_errors += 1

                            logger.info(
                                f"Plex poster cleanup: disk-walk complete — "
                                f"{total_deleted} file(s) deleted, {total_errors} error(s)"
                            )
                        else:
                            logger.warning(
                                "Plex poster cleanup: no stored upload hashes found — "
                                "skipping disk-walk to avoid deleting active overlays. "
                                "Falling back to legacy API pass for all items."
                            )

                        # Log activity now (after fast DB + disk-walk phases) so the
                        # entry appears promptly regardless of how long the legacy API
                        # pass takes for items that still lack a stored upload hash.
                        _activity_title = f"Cleanup: {total_removed} record(s) removed, {total_reset} reset"
                        if total_deleted > 0:
                            _activity_mb = round(total_bytes / (1024 * 1024), 2)
                            _activity_title += f", {total_deleted} poster(s) deleted ({_activity_mb} MB)"
                        log_activity(
                            'cleanup',
                            triggered_by=triggered_by,
                            result='success' if total_errors == 0 else 'partial',
                            title=_activity_title,
                            stats={
                                'orphaned_removed':           orphaned_count,
                                'old_records_removed':        old_records_count,
                                'failed_items_reset':         reset_count,
                                'season_orphaned_removed':    season_orphaned_count,
                                'season_old_records_removed': season_old_count,
                                'season_failed_items_reset':  season_reset_count,
                                'posters_deleted':            total_deleted,
                                'mb_reclaimed':               round(total_bytes / (1024 * 1024), 2),
                                'poster_errors':              total_errors,
                            },
                            duration_seconds=int(time.time() - _cleanup_start),
                        )

                        # Legacy API-based cleanup.
                        # When disk-walk ran: only process items that lack a stored hash
                        # (they were applied before the hash-tracking column was added).
                        # When disk-walk was skipped (no hashes stored yet): process ALL
                        # items so that stale uploads are still removed safely via the API.
                        _lconn = _get_db_connection()
                        _lcur  = _lconn.cursor()
                        if _keep_hashes:
                            _lcur.execute('''
                                SELECT DISTINCT m.ms_item_id, m.title
                                FROM media_items m
                                JOIN media_overlay_state o ON m.id = o.media_item_id
                                WHERE (
                                    o.status IN ('applied', 'removed')
                                    OR (o.status = 'pending' AND o.overlay_applied_at IS NOT NULL)
                                )
                                  AND (o.last_plex_upload_hash IS NULL OR o.last_plex_upload_hash = '')
                                  AND m.ms_item_id IS NOT NULL
                                  AND m.ms_item_id != ''
                                ORDER BY m.ms_item_id
                            ''')
                        else:
                            # No hashes stored yet — use API for all items so we don't
                            # blindly delete files we cannot identify as safe to remove.
                            _lcur.execute('''
                                SELECT DISTINCT m.ms_item_id, m.title
                                FROM media_items m
                                JOIN media_overlay_state o ON m.id = o.media_item_id
                                WHERE (
                                    o.status IN ('applied', 'removed')
                                    OR (o.status = 'pending' AND o.overlay_applied_at IS NOT NULL)
                                )
                                  AND m.ms_item_id IS NOT NULL
                                  AND m.ms_item_id != ''
                                ORDER BY m.ms_item_id
                            ''')
                        items = _lcur.fetchall()
                        _lconn.close()

                        if items:
                            logger.info(
                                f"Plex poster cleanup: legacy API pass for "
                                f"{len(items)} item(s) without stored upload hash"
                            )

                        # Legacy API pass — only runs when items list is non-empty.
                        _stale_keys = []
                        _stale_lock = threading.Lock()

                        def _cleanup_one_item(item):
                            _rk    = item['ms_item_id']
                            _title = item['title']
                            _del, _err, _byt = 0, 0, 0
                            _found_hash = None
                            try:
                                try:
                                    _posters = client.get_poster_list(_rk)
                                except Exception as _e404:
                                    import requests as _req
                                    if isinstance(_e404, _req.exceptions.HTTPError) and \
                                            getattr(getattr(_e404, 'response', None), 'status_code', None) == 404:
                                        logger.warning(
                                            f"Plex poster cleanup: {_rk} ({_title}) 404 — queuing stale key clear"
                                        )
                                        with _stale_lock:
                                            _stale_keys.append(_rk)
                                        return _del, _err, _byt, None
                                    raise
                                for _p in (_posters or []):
                                    _p_key = _p.get('ratingKey', '')
                                    if _p.get('selected'):
                                        # Capture selected poster hash so future cleanup runs
                                        # can use the fast disk-walk path instead of API calls.
                                        if _p_key.startswith('upload://posters/'):
                                            _h = _p_key[len('upload://posters/'):]
                                            if _h:
                                                _found_hash = _h
                                        continue
                                    if not _p_key.startswith('upload://posters/'):
                                        continue
                                    _uhash = _p_key[len('upload://posters/'):]
                                    if not _uhash:
                                        continue
                                    for _fp in _glob.glob(os.path.join(
                                            plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                            'Uploads', 'posters', _uhash)):
                                        try:
                                            try:
                                                _byt += os.path.getsize(_fp)
                                            except OSError:
                                                pass
                                            os.remove(_fp)
                                            _del += 1
                                            logger.info(
                                                f"Plex poster cleanup: deleted '{_fp}' for '{_title}'")
                                        except Exception as _de:
                                            logger.error(
                                                f"Plex poster cleanup: failed '{_fp}': {_de}")
                                            _err += 1
                            except Exception as _ie:
                                logger.error(
                                    f"Plex poster cleanup error for '{_title}' ({_rk}): {_ie}")
                                _err += 1
                            return _del, _err, _byt, _found_hash

                        _hash_updates = {}  # ms_item_id -> selected upload hash
                        if items:
                            with ThreadPoolExecutor(max_workers=10) as _pool:
                                _futs = {_pool.submit(_cleanup_one_item, dict(it)): it for it in items}
                                for _fut in as_completed(_futs):
                                    _d, _e, _b, _h = _fut.result()
                                    total_deleted += _d
                                    total_errors  += _e
                                    total_bytes   += _b
                                    if _h:
                                        _hash_updates[_futs[_fut]['ms_item_id']] = _h

                        # Store discovered hashes so future cleanup runs use the fast
                        # disk-walk path instead of per-item Plex API calls (self-healing).
                        if _hash_updates:
                            try:
                                _hu_conn = _get_db_connection()
                                for _ms_id, _uhash in _hash_updates.items():
                                    _hu_conn.execute(
                                        """UPDATE media_overlay_state
                                              SET last_plex_upload_hash = ?,
                                                  updated_at = CURRENT_TIMESTAMP
                                            WHERE media_item_id IN (
                                                SELECT id FROM media_items WHERE ms_item_id = ?
                                            )
                                              AND (last_plex_upload_hash IS NULL
                                               OR last_plex_upload_hash = '')""",
                                        (_uhash, _ms_id)
                                    )
                                _hu_conn.commit()
                                _hu_conn.close()
                                logger.info(
                                    f"Plex poster cleanup: stored upload hash for "
                                    f"{len(_hash_updates)} item(s) — future runs will use disk-walk"
                                )
                            except Exception as _hue:
                                logger.warning(f"Failed to store upload hashes: {_hue}")

                        # Handle stale 404 keys sequentially (rare, needs DB writes)
                        for _sk in _stale_keys:
                            _c2 = _get_db_connection()
                            _cur2 = _c2.cursor()
                            _cur2.execute('SELECT id FROM media_items WHERE ms_item_id = ?', (_sk,))
                            _stale_ids = [r['id'] for r in _cur2.fetchall()]
                            _cur2.execute('UPDATE media_items SET ms_item_id = NULL WHERE ms_item_id = ?', (_sk,))
                            if _stale_ids:
                                _ph = ','.join('?' * len(_stale_ids))
                                _cur2.execute(
                                    f"UPDATE media_overlay_state SET status='pending', "
                                    f"reason='Stale ms_item_id cleared during poster cleanup', "
                                    f"updated_at=CURRENT_TIMESTAMP WHERE media_item_id IN ({_ph})",
                                    _stale_ids
                                )
                            _c2.commit()
                            _c2.close()

                        # --- Season poster cleanup: legacy API pass.
                        # When disk-walk ran: only seasons without a stored hash.
                        # When disk-walk was skipped: all seasons.
                        _c3 = _get_db_connection()
                        _cur3 = _c3.cursor()
                        if _keep_hashes:
                            _cur3.execute('''
                                SELECT DISTINCT season_ms_item_id, show_ms_item_id, season_number
                                FROM season_overlay_state
                                WHERE (
                                    status IN ('applied', 'removed', 'user_removed')
                                    OR (status = 'pending' AND overlay_applied_at IS NOT NULL)
                                )
                                  AND (last_plex_upload_hash IS NULL OR last_plex_upload_hash = '')
                            ''')
                        else:
                            _cur3.execute('''
                                SELECT DISTINCT season_ms_item_id, show_ms_item_id, season_number
                                FROM season_overlay_state
                                WHERE (
                                    status IN ('applied', 'removed', 'user_removed')
                                    OR (status = 'pending' AND overlay_applied_at IS NOT NULL)
                                )
                            ''')
                        season_items = _cur3.fetchall()
                        _c3.close()
                        if season_items:
                            logger.info(
                                f"Plex poster cleanup: legacy API pass for "
                                f"{len(season_items)} season(s) without stored upload hash"
                            )

                        def _cleanup_one_season(s_item):
                            _sk  = s_item['season_ms_item_id']
                            _sn  = s_item['season_number']
                            _lbl = f"Season {_sn} (key={_sk})"
                            _del, _err, _byt = 0, 0, 0
                            _found_hash = None
                            try:
                                _posters = client.get_poster_list(_sk)
                                for _p in (_posters or []):
                                    _p_key = _p.get('ratingKey', '')
                                    if _p.get('selected'):
                                        if _p_key.startswith('upload://posters/'):
                                            _h = _p_key[len('upload://posters/'):]
                                            if _h:
                                                _found_hash = _h
                                        continue
                                    if not _p_key.startswith('upload://posters/'):
                                        continue
                                    _usuffix = _p_key[len('upload://posters/'):]
                                    if not _usuffix:
                                        continue
                                    _matches = _glob.glob(os.path.join(
                                        plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                        'Uploads', 'posters', _usuffix))
                                    if not _matches:
                                        _flat = _usuffix.split('/')[-1]
                                        if _flat and _flat != _usuffix:
                                            _matches = _glob.glob(os.path.join(
                                                plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                                'Uploads', 'posters', _flat))
                                    for _fp in _matches:
                                        try:
                                            try:
                                                _byt += os.path.getsize(_fp)
                                            except OSError:
                                                pass
                                            os.remove(_fp)
                                            _del += 1
                                            logger.info(
                                                f"Plex poster cleanup: deleted '{_fp}' for {_lbl}")
                                        except Exception as _de:
                                            logger.error(
                                                f"Plex poster cleanup: failed '{_fp}': {_de}")
                                            _err += 1
                            except Exception as _se:
                                logger.error(f"Plex poster cleanup error for {_lbl}: {_se}")
                                _err += 1
                            return _del, _err, _byt, _found_hash

                        _season_hash_updates = {}  # season_ms_item_id -> selected upload hash
                        if season_items:
                            with ThreadPoolExecutor(max_workers=10) as _s_pool:
                                _s_futs = {
                                    _s_pool.submit(_cleanup_one_season, dict(si)): si
                                    for si in season_items
                                }
                                for _s_fut in as_completed(_s_futs):
                                    _d, _e, _b, _sh = _s_fut.result()
                                    total_deleted += _d
                                    total_errors  += _e
                                    total_bytes   += _b
                                    if _sh:
                                        _season_hash_updates[_s_futs[_s_fut]['season_ms_item_id']] = _sh

                        if _season_hash_updates:
                            try:
                                _shu_conn = _get_db_connection()
                                for _sms_id, _suhash in _season_hash_updates.items():
                                    _shu_conn.execute(
                                        """UPDATE season_overlay_state
                                              SET last_plex_upload_hash = ?,
                                                  updated_at = CURRENT_TIMESTAMP
                                            WHERE season_ms_item_id = ?
                                              AND (last_plex_upload_hash IS NULL
                                               OR last_plex_upload_hash = '')""",
                                        (_suhash, _sms_id)
                                    )
                                _shu_conn.commit()
                                _shu_conn.close()
                                logger.info(
                                    f"Plex poster cleanup: stored upload hash for "
                                    f"{len(_season_hash_updates)} season(s) — future runs will use disk-walk"
                                )
                            except Exception as _shue:
                                logger.warning(f"Failed to store season upload hashes: {_shue}")

                        total_mb_plex = round(total_bytes / (1024 * 1024), 2)
                        logger.info(
                            f"Plex poster cleanup: {total_deleted} deleted, "
                            f"{total_mb_plex} MB reclaimed, {total_errors} error(s)"
                        )

                        if total_deleted > 0:
                            try:
                                _cs = _get_db_connection()
                                _cs.execute('''
                                    INSERT INTO overlay_sync_state (key, value, updated_at)
                                    VALUES ('cleanup_total_posters', ?, CURRENT_TIMESTAMP)
                                    ON CONFLICT(key) DO UPDATE SET
                                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                                        updated_at = CURRENT_TIMESTAMP
                                ''', (str(total_deleted), total_deleted))
                                _cs.execute('''
                                    INSERT INTO overlay_sync_state (key, value, updated_at)
                                    VALUES ('cleanup_total_bytes', ?, CURRENT_TIMESTAMP)
                                    ON CONFLICT(key) DO UPDATE SET
                                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                                        updated_at = CURRENT_TIMESTAMP
                                ''', (str(total_bytes), total_bytes))
                                _cs.commit()
                                _cs.close()
                            except Exception as _cse:
                                logger.warning(f"Failed to update cleanup stats counter: {_cse}")

                    except Exception as _pce:
                        logger.warning(f"Plex poster cleanup failed: {_pce}")

        # ── Collection poster cleanup ──────────────────────────────────────────
        # Delete old non-selected uploaded posters from CLI Debrid managed collections.
        # Uses filesystem deletion (same as media poster cleanup) — Plex API DELETE
        # for uploaded posters is unreliable for collections.
        collection_posters_deleted = 0
        try:
            if not is_jellyfin_mode():
                _coll_plex_data = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
                if not _coll_plex_data or not os.path.isdir(_coll_plex_data):
                    logger.debug("Collection poster cleanup: plex_data_path not configured or inaccessible — skipping.")
                else:
                    from database.core import get_db_connection as _get_coll_db
                    from utilities.settings import get_setting as _gs
                    import requests as _requests
                    _plex_url = _gs('Plex', 'url', '').rstrip('/')
                    _plex_token = _gs('Plex', 'token', '')
                    if _plex_url and _plex_token:
                        _headers = {'X-Plex-Token': _plex_token, 'Accept': 'application/json'}

                        # Build a protected set from state files — these hashes must never
                        # be deleted even if Plex has reset its selected poster back to its
                        # own auto-generated one (which causes false-positive deletion of
                        # our still-valid uploaded poster).
                        _state_protected_hashes = set()
                        try:
                            import json as _cpjson
                            _cp_state_path = os.path.join(
                                os.environ.get('USER_CONFIG', '/user/config'), 'plex_collection_state.json'
                            )
                            if os.path.exists(_cp_state_path):
                                with open(_cp_state_path, 'r') as _cpf:
                                    _cp_state = _cpjson.load(_cpf)
                                for _cpe in _cp_state.values():
                                    if isinstance(_cpe, dict):
                                        for _cph in _cpe.get('plex_upload_hashes', {}).values():
                                            if _cph:
                                                _state_protected_hashes.add(_cph)
                        except Exception as _csp_e:
                            logger.debug(f"Collection poster cleanup: state file read error: {_csp_e}")

                        _cconn = _get_coll_db()
                        _rks = []
                        try:
                            rows = _cconn.execute(
                                'SELECT movie_collection_ratingkey, show_collection_ratingkey FROM plex_collection_sync'
                            ).fetchall()
                            for row in rows:
                                for rk in [row[0], row[1]]:
                                    if rk:
                                        _rks.append(str(rk))
                        finally:
                            _cconn.close()

                        # Build set of all selected upload hashes across ALL managed ratingKeys first,
                        # so we never delete a hash that is the active poster for a sibling collection
                        # (e.g. movie + show collections sharing the same Plex bundle due to same name).
                        _selected_hashes = set()
                        _rk_posters = {}
                        for _rk in _rks:
                            try:
                                r = _requests.get(
                                    f"{_plex_url}/library/metadata/{_rk}/posters",
                                    headers=_headers, timeout=10
                                )
                                if r.status_code != 200:
                                    continue
                                _rk_posters[_rk] = r.json().get('MediaContainer', {}).get('Metadata', [])
                                for p in _rk_posters[_rk]:
                                    if p.get('selected'):
                                        pk = p.get('ratingKey', '')
                                        if pk.startswith('upload://posters/'):
                                            _selected_hashes.add(pk[len('upload://posters/'):])
                            except Exception as _ce:
                                logger.debug(f"Collection poster cleanup error fetching rk={_rk}: {_ce}")

                        import glob as _cglob
                        for _rk, posters in _rk_posters.items():
                            try:
                                for p in posters:
                                    pk = p.get('ratingKey', '')
                                    if p.get('selected') or not pk.startswith('upload://posters/'):
                                        continue
                                    _chash = pk[len('upload://posters/'):]
                                    if not _chash:
                                        continue
                                    # Skip if this hash is the selected poster for any other ratingKey
                                    if _chash in _selected_hashes:
                                        continue
                                    # Skip if this hash is tracked in the state file — Plex may have
                                    # temporarily reset to its own poster but our upload is still valid
                                    if _chash in _state_protected_hashes:
                                        logger.debug(f"Collection poster cleanup: skipping state-protected hash {_chash} (rk={_rk})")
                                        continue
                                    # Delete via filesystem — same approach as media overlay cleanup
                                    _cpat = os.path.join(
                                        _coll_plex_data, 'Metadata', '*', '*', '*.bundle',
                                        'Uploads', 'posters', _chash
                                    )
                                    for _cf in _cglob.glob(_cpat):
                                        try:
                                            os.remove(_cf)
                                            collection_posters_deleted += 1
                                            logger.info(f"Collection poster cleanup: deleted '{_cf}' (rk={_rk})")
                                        except Exception as _cfe:
                                            logger.warning(f"Collection poster cleanup: failed to delete '{_cf}': {_cfe}")
                            except Exception as _ce:
                                logger.debug(f"Collection poster cleanup error for rk={_rk}: {_ce}")

                    if collection_posters_deleted > 0:
                        logger.info(f"Collection poster cleanup: deleted {collection_posters_deleted} old poster(s)")
        except Exception as _cpe:
            logger.warning(f"Collection poster cleanup failed: {_cpe}")

        # ── Smart collection poster cleanup ────────────────────────────────────
        # Same filesystem approach for smart collection posters.
        smart_posters_deleted = 0
        try:
            if not is_jellyfin_mode():
                _sc_plex_data = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
                if not _sc_plex_data or not os.path.isdir(_sc_plex_data):
                    logger.debug("Smart collection poster cleanup: plex_data_path not configured — skipping.")
                else:
                    from database.plex_smart_collections import _load_state as _sc_load, _migrate_state as _sc_migrate
                    import requests as _screquests
                    _sc_state = _sc_migrate(_sc_load())
                    _sc_collections = _sc_state.get('collections', {})
                    _sc_plex_url = get_setting('Plex', 'url', '').rstrip('/')
                    _sc_token = get_setting('Plex', 'token', '')
                    # Hashes stored when posters were last uploaded — never delete these
                    _sc_protected_hashes = set(_sc_state.get('plex_upload_hashes', {}).values())

                    if _sc_plex_url and _sc_token and _sc_collections:
                        _sc_headers = {'X-Plex-Token': _sc_token, 'Accept': 'application/json'}
                        import glob as _scglob
                        for _sc_rk in _sc_collections.keys():
                            try:
                                _scr = _screquests.get(
                                    f"{_sc_plex_url}/library/metadata/{_sc_rk}/posters",
                                    headers=_sc_headers, timeout=10
                                )
                                if _scr.status_code != 200:
                                    continue
                                for _scp in _scr.json().get('MediaContainer', {}).get('Metadata', []):
                                    _scpk = _scp.get('ratingKey', '')
                                    if _scp.get('selected') or not _scpk.startswith('upload://posters/'):
                                        continue
                                    _schash = _scpk[len('upload://posters/'):]
                                    if not _schash:
                                        continue
                                    if _schash in _sc_protected_hashes:
                                        logger.debug(f"Smart collection poster cleanup: skipping state-protected hash {_schash} (rk={_sc_rk})")
                                        continue
                                    _scpat = os.path.join(
                                        _sc_plex_data, 'Metadata', '*', '*', '*.bundle',
                                        'Uploads', 'posters', _schash
                                    )
                                    for _scf in _scglob.glob(_scpat):
                                        try:
                                            os.remove(_scf)
                                            smart_posters_deleted += 1
                                            logger.info(f"Smart collection poster cleanup: deleted '{_scf}' (rk={_sc_rk})")
                                        except Exception as _scfe:
                                            logger.warning(f"Smart collection poster cleanup: failed to delete '{_scf}': {_scfe}")
                            except Exception as _sce:
                                logger.debug(f"Smart collection poster cleanup error for rk={_sc_rk}: {_sce}")

                    if smart_posters_deleted > 0:
                        logger.info(f"Smart collection poster cleanup: deleted {smart_posters_deleted} old poster(s)")
        except Exception as _spe:
            logger.warning(f"Smart collection poster cleanup failed: {_spe}")

        return {
            'success': True,
            'message': 'Cleanup completed',
            'orphaned_removed':               orphaned_count,
            'old_records_removed':            old_records_count,
            'failed_items_reset':             reset_count,
            'season_orphaned_removed':        season_orphaned_count,
            'season_old_records_removed':     season_old_count,
            'season_failed_items_reset':      season_reset_count,
            'posters_deleted':                total_deleted,
            'mb_reclaimed':                   round(total_bytes / (1024 * 1024), 2),
            'collection_posters_deleted':     collection_posters_deleted,
            'smart_collection_posters_deleted': smart_posters_deleted,
        }

    except Exception as e:
        logger.error(f"Overlay cleanup task failed: {e}", exc_info=True)
        log_activity('cleanup', triggered_by=triggered_by, result='failed',
                     title=f"Overlay cleanup failed: {e}",
                     duration_seconds=int(time.time() - _cleanup_start))
        return {'success': False, 'message': str(e)}


def task_plex_poster_cleanup():
    """Deprecated — poster cleanup is now part of task_overlay_cleanup."""
    return task_overlay_cleanup()


def _task_plex_poster_cleanup_old():
    """Old body kept here temporarily; no longer called."""
    if is_jellyfin_mode():
        return {'success': True, 'message': 'Skipped (Jellyfin mode)', 'deleted': 0}

    logger.info("Starting Plex poster cleanup task")

    try:
        plex_data_path = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
        if not plex_data_path:
            logger.warning(
                "Plex poster cleanup: 'plex_data_path' not configured in Overlay Settings. "
                "Set it to your Plex Media Server data directory to enable poster cleanup."
            )
            return {'success': False, 'message': 'plex_data_path not configured'}

        if not os.path.isdir(plex_data_path):
            logger.error(f"Plex poster cleanup: plex_data_path '{plex_data_path}' does not exist or is not a directory")
            return {'success': False, 'message': f'plex_data_path not accessible: {plex_data_path}'}

        plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')

        if not plex_token:
            logger.warning("Plex token not configured, skipping Plex poster cleanup")
            return {'success': False, 'message': 'Plex token not configured'}

        from .plex_client import PlexClient
        client = PlexClient(plex_url, plex_token)

        conn = _get_db_connection()
        cursor = conn.cursor()

        # Only check unique ms_item_ids — no need to process per-episode.
        # Include:
        #   'applied'  — overlay is active, clean up old upload versions in Plex history
        #   'removed'  — overlay was removed but old upload:// versions may still exist
        #   'pending' with overlay_applied_at set — items reset to pending after
        #              "delete all backups" still have overlay-uploaded posters in Plex
        cursor.execute('''
            SELECT DISTINCT m.ms_item_id, m.title
            FROM media_items m
            JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE (
                o.status IN ('applied', 'removed')
                OR (o.status = 'pending' AND o.overlay_applied_at IS NOT NULL)
            )
              AND m.ms_item_id IS NOT NULL
              AND m.ms_item_id != ''
            ORDER BY m.ms_item_id
        ''')
        items = cursor.fetchall()
        conn.close()

        logger.info(f"Plex poster cleanup: checking {len(items)} unique ms_item_id(s) with applied or removed overlays")

        total_deleted = 0
        total_errors = 0
        total_bytes = 0

        for item in items:
            rating_key = item['ms_item_id']
            title = item['title']

            try:
                try:
                    posters = client.get_poster_list(rating_key)
                except Exception as _e404:
                    import requests as _req
                    if isinstance(_e404, _req.exceptions.HTTPError) and \
                            getattr(getattr(_e404, 'response', None), 'status_code', None) == 404:
                        logger.warning(
                            f"Plex poster cleanup: rating_key {rating_key} ({title}) returned 404 — "
                            f"clearing stale key from DB"
                        )
                        conn2 = _get_db_connection()
                        cur2 = conn2.cursor()
                        cur2.execute(
                            'SELECT id FROM media_items WHERE ms_item_id = ?',
                            (rating_key,)
                        )
                        stale_ids = [r['id'] for r in cur2.fetchall()]
                        cur2.execute(
                            'UPDATE media_items SET ms_item_id = NULL WHERE ms_item_id = ?',
                            (rating_key,)
                        )
                        if stale_ids:
                            placeholders = ','.join('?' * len(stale_ids))
                            cur2.execute(
                                f"UPDATE media_overlay_state SET status = 'pending', "
                                f"reason = 'Stale ms_item_id cleared during poster cleanup', "
                                f"updated_at = CURRENT_TIMESTAMP "
                                f"WHERE media_item_id IN ({placeholders})",
                                stale_ids
                            )
                        conn2.commit()
                        conn2.close()
                        continue
                    raise

                if not posters:
                    continue

                # Collect non-selected uploaded poster hashes.
                # Uploaded posters have ratingKey = 'upload://posters/{hash}' and no 'provider' field.
                to_delete = []
                for p in posters:
                    if p.get('selected'):
                        continue
                    r_key = p.get('ratingKey', '')
                    if r_key.startswith('upload://posters/'):
                        upload_hash = r_key[len('upload://posters/'):]
                        if upload_hash:
                            to_delete.append(upload_hash)

                # Delete by finding the file in the Plex bundle filesystem.
                for upload_hash in to_delete:
                    pattern = os.path.join(
                        plex_data_path, 'Metadata', '*', '*', '*.bundle',
                        'Uploads', 'posters', upload_hash
                    )
                    matches = _glob.glob(pattern)
                    if not matches:
                        logger.debug(f"Plex poster cleanup: no file found for hash {upload_hash} ({title})")
                        continue
                    for fpath in matches:
                        try:
                            try:
                                total_bytes += os.path.getsize(fpath)
                            except OSError:
                                pass
                            os.remove(fpath)
                            total_deleted += 1
                            logger.info(f"Plex poster cleanup: deleted '{fpath}' for '{title}'")
                        except Exception as e:
                            logger.error(f"Plex poster cleanup: failed to delete '{fpath}': {e}")
                            total_errors += 1

            except Exception as e:
                logger.error(f"Plex poster cleanup error for '{title}' ({rating_key}): {e}")
                total_errors += 1

        # --- Season poster cleanup ---
        # Season overlay state tracks season-level applied/removed overlays.
        # Season poster ratingKeys use 'upload://posters/seasons/N/{hash}' format.
        conn3 = _get_db_connection()
        cursor3 = conn3.cursor()
        cursor3.execute('''
            SELECT DISTINCT season_ms_item_id, show_ms_item_id, season_number
            FROM season_overlay_state
            WHERE status IN ('applied', 'removed', 'user_removed')
               OR (status = 'pending' AND overlay_applied_at IS NOT NULL)
        ''')
        season_items = cursor3.fetchall()
        conn3.close()

        logger.info(f"Plex poster cleanup: checking {len(season_items)} season(s) with overlay history")

        for s_item in season_items:
            season_key = s_item['season_ms_item_id']
            season_num = s_item['season_number']
            label = f"Season {season_num} (key={season_key})"

            try:
                posters = client.get_poster_list(season_key)
                if not posters:
                    continue

                # Find non-selected upload:// posters to clean up
                for p in posters:
                    if p.get('selected'):
                        continue
                    r_key = p.get('ratingKey', '')
                    if not r_key.startswith('upload://posters/'):
                        continue
                    # Extract path after 'upload://posters/' — for seasons this gives 'seasons/N/{hash}'
                    upload_suffix = r_key[len('upload://posters/'):]
                    if not upload_suffix:
                        continue

                    # Try filesystem deletion using the full suffix path
                    pattern = os.path.join(
                        plex_data_path, 'Metadata', '*', '*', '*.bundle',
                        'Uploads', 'posters', upload_suffix
                    )
                    matches = _glob.glob(pattern)

                    # If no match with full suffix, try just the trailing hash component
                    # (in case Plex stores season uploads flat like show uploads)
                    if not matches:
                        flat_hash = upload_suffix.split('/')[-1]
                        if flat_hash and flat_hash != upload_suffix:
                            flat_pattern = os.path.join(
                                plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                'Uploads', 'posters', flat_hash
                            )
                            matches = _glob.glob(flat_pattern)

                    if not matches:
                        logger.debug(
                            f"Plex poster cleanup: no file found for {r_key!r} ({label})")
                        continue
                    for fpath in matches:
                        try:
                            try:
                                total_bytes += os.path.getsize(fpath)
                            except OSError:
                                pass
                            os.remove(fpath)
                            total_deleted += 1
                            logger.info(
                                f"Plex poster cleanup: deleted '{fpath}' for {label}")
                        except Exception as e:
                            logger.error(
                                f"Plex poster cleanup: failed to delete '{fpath}': {e}")
                            total_errors += 1

            except Exception as e:
                logger.error(f"Plex poster cleanup error for {label}: {e}")
                total_errors += 1

        total_mb = round(total_bytes / (1024 * 1024), 2)
        logger.info(f"Plex poster cleanup complete: {total_deleted} old poster(s) deleted, {total_mb} MB reclaimed, {total_errors} error(s)")

        # Persist cumulative cleanup counters
        if total_deleted > 0:
            try:
                _conn_stats = _get_db_connection()
                _conn_stats.execute('''
                    INSERT INTO overlay_sync_state (key, value, updated_at)
                    VALUES ('cleanup_total_posters', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                        updated_at = CURRENT_TIMESTAMP
                ''', (str(total_deleted), total_deleted))
                _conn_stats.execute('''
                    INSERT INTO overlay_sync_state (key, value, updated_at)
                    VALUES ('cleanup_total_bytes', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                        updated_at = CURRENT_TIMESTAMP
                ''', (str(total_bytes), total_bytes))
                _conn_stats.commit()
                _conn_stats.close()
            except Exception as _e:
                logger.warning(f"Failed to update cleanup stats counter: {_e}")

        log_activity(
            'cleanup',
            triggered_by='scheduled',
            result='success' if total_errors == 0 else 'partial',
            title=f"Poster cleanup: {total_deleted} deleted, {total_mb} MB reclaimed"
                  + (f", {total_errors} errors" if total_errors else ""),
            stats={'deleted': total_deleted, 'errors': total_errors,
                   'mb_reclaimed': total_mb},
        )
        return {
            'success': True,
            'message': f'Plex poster cleanup completed: {total_deleted} old posters deleted',
            'deleted': total_deleted,
            'errors': total_errors,
        }

    except Exception as e:
        logger.error(f"Plex poster cleanup task failed: {e}", exc_info=True)
        return {'success': False, 'message': str(e)}


def task_overlay_full_sync(force: bool = False):
    """
    Full overlay sync task.

    Processes ALL Collected media items with media server item IDs, optionally forcing
    regeneration. TV shows are deduplicated by ms_item_id. This is a heavy
    operation and should be run manually or scheduled rarely.

    Args:
        force: Force regeneration even for items with existing overlays
    """
    if not get_setting('Overlay Settings', 'overlays_enabled', False):
        return {'success': False, 'message': 'Overlay system is disabled'}

    logger.info(f"Starting full overlay sync (force={force})")
    _full_sync_start = time.time()

    try:
        if is_jellyfin_mode():
            jf_token = get_jellyfin_token()
            if not jf_token:
                logger.warning("Jellyfin token not configured, skipping full sync")
                return {'success': False, 'message': 'Jellyfin token not configured'}
            plex_url   = ''
            plex_token = jf_token
        else:
            plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
            plex_token = get_setting('Plex', 'token', default='')
            if not plex_token:
                logger.warning("Plex token not configured, skipping full sync")
                return {'success': False, 'message': 'Plex token not configured'}

        manager = OverlayManager(None, plex_url, plex_token)

        conn = _get_db_connection()
        cursor = conn.cursor()

        # Deduplicate TV shows by ms_item_id
        cursor.execute('''
            SELECT
                MIN(id) AS id,
                ms_item_id,
                type
            FROM media_items
            WHERE ms_item_id IS NOT NULL
              AND LENGTH(ms_item_id) >= 20
              AND state IN ('Collected', 'Upgrading')
            GROUP BY
                CASE WHEN type = 'episode' THEN ms_item_id ELSE CAST(id AS TEXT) END
            ORDER BY MIN(id)
        ''')

        all_items = cursor.fetchall()
        conn.close()

        logger.info(f"Found {len(all_items)} unique item(s) to process (after TV deduplication)")

        if not all_items:
            return {'success': True, 'message': 'No items to process', 'processed': 0}

        total = applied = analyzing = failed = skipped = 0
        batch_size = 200

        for i in range(0, len(all_items), batch_size):
            batch = all_items[i:i + batch_size]
            logger.info(f"Processing batch {i // batch_size + 1}/{(len(all_items) + batch_size - 1) // batch_size}")

            for row in batch:
                item_id = row['id']
                ms_item_id = row['ms_item_id']
                item_type = row['type']
                total += 1

                result = manager.generate_overlay_for_item(item_id, force=force)
                status = result.get('status', 'failed')

                if status == 'applied':
                    applied += 1
                    if item_type == 'episode':
                        _mark_all_episodes_applied(ms_item_id, result.get('content_hash'))
                elif status == 'analyzing':
                    analyzing += 1
                elif status == 'skipped':
                    skipped += 1
                else:
                    failed += 1

        logger.info(
            f"Full sync complete: {applied} applied, {analyzing} analyzing, "
            f"{failed} failed, {skipped} skipped out of {total} total"
        )

        log_activity('full_sync',
                     triggered_by='scheduled',
                     title=f"Full sync: {applied} applied, {failed} failed, {skipped} skipped of {total}",
                     stats={'total': total, 'applied': applied, 'failed': failed, 'skipped': skipped},
                     duration_seconds=int(time.time() - _full_sync_start))
        return {
            'success': True,
            'message': 'Full overlay sync completed',
            'total': total,
            'applied': applied,
            'analyzing': analyzing,
            'failed': failed,
            'skipped': skipped,
        }

    except Exception as e:
        logger.error(f"Full overlay sync task failed: {e}", exc_info=True)
        log_activity('full_sync', triggered_by='scheduled', result='failed',
                     title=f"Full sync failed: {e}",
                     duration_seconds=int(time.time() - _full_sync_start))
        return {'success': False, 'message': str(e)}


# Task registry for easy access
OVERLAY_TASKS = {
    'overlay_sync': {
        'function': task_overlay_sync,
        'description': 'Sync overlays for pending/failed items',
        'default_interval': 1800,  # 30 minutes
        'enabled_by_default': True
    },
    'overlay_cleanup': {
        'function': task_overlay_cleanup,
        'description': 'Clean up old overlay state records',
        'default_interval': 86400,  # 24 hours
        'enabled_by_default': True
    },
    'overlay_full_sync': {
        'function': task_overlay_full_sync,
        'description': 'Full overlay sync for all items',
        'default_interval': 604800,  # 7 days
        'enabled_by_default': False  # Manual/opt-in only
    }
}
