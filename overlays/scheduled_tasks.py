"""
Scheduled Tasks for Overlay System

Periodic tasks for overlay sync and cleanup.
"""

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Dict

from .overlay_manager import OverlayManager
from .activity_logger import log_activity
from .utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token, overlays_active
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


def _sync_library_keys_for_new_items(plex_url: str, plex_token: str,
                                     missing_only: bool = False) -> Dict[str, int]:
    """
    Auto-populate ms_item_id for Collected items that don't have one yet.

    DB-first approach: fetch only the rows that are missing ms_item_id, build
    in-memory lookup dicts from a single Plex API call, resolve each missing row
    in O(1), then write only the matched rows. This avoids iterating the entire
    Plex library (4000+ items) for every row — previously caused 16+ minute
    sync times when only 14 rows needed updating.

    Also detects ms_item_id changes (e.g. after Plex split-apart) and resets
    overlay state to pending so overlays are regenerated with the new key.

    missing_only=True skips split-apart detection, so the call returns without
    touching Plex when no row is missing a key (used while a just-collected item
    waits to be indexed).

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
    split_rows     = [] if missing_only else [
        dict(r) for r in all_rows
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
                # A page fetch failing partway through leaves `plex_items` a PARTIAL
                # inventory with no other signal of that — see get_all_items_with_guids.
                # Blank-key assignment (Pass 1) can only ever fill an empty value, so
                # it's safe to run on a partial inventory (worst case: some rows just
                # don't resolve this pass). Existing-key invalidation (Pass 2,
                # split-apart detection) is NOT safe on partial data: a legitimate
                # item dropped from this fetch could make an unrelated row's file
                # path look like it moved. Require a complete inventory for that pass.
                inventory_complete = not getattr(client, 'last_fetch_incomplete', False)

                file_map:  dict = {}   # location_on_disk → ratingKey
                imdb_map:  dict = {}   # imdb_id          → ratingKey
                tmdb_map:  dict = {}   # tmdb_id (str)    → ratingKey
                title_map: dict = {}   # (title_lower, year_str) → ratingKey
                # Keys seen under more than one distinct ratingKey this fetch — e.g.
                # two Plex items sharing an IMDb ID (a real split-apart in progress).
                # Last-write-wins previously resolved these to whichever ratingKey the
                # API happened to return last; excluded here instead so an ambiguous
                # key can never silently steer a row onto the wrong item.
                ambiguous: dict = {'file': set(), 'imdb': set(), 'tmdb': set(), 'title': set()}

                def _set_unique(bucket: dict, key, rk: str, ambiguous_key: str) -> None:
                    existing = bucket.get(key)
                    if existing is not None and existing != rk:
                        ambiguous[ambiguous_key].add(key)
                        return
                    bucket[key] = rk

                for pi in plex_items:
                    rk = pi.get('ratingKey')
                    if not rk:
                        continue
                    for fp in (pi.get('file_paths') or []):
                        if fp:
                            _set_unique(file_map, fp, rk, 'file')
                    if pi.get('imdb_id'):
                        _set_unique(imdb_map, pi['imdb_id'], rk, 'imdb')
                    if pi.get('tmdb_id'):
                        _set_unique(tmdb_map, str(pi['tmdb_id']), rk, 'tmdb')
                    if pi.get('title') and pi.get('year'):
                        _set_unique(title_map, (pi['title'].lower(), str(pi['year'])), rk, 'title')

                for key in ambiguous['file']:
                    file_map.pop(key, None)
                for key in ambiguous['imdb']:
                    imdb_map.pop(key, None)
                for key in ambiguous['tmdb']:
                    tmdb_map.pop(key, None)
                for key in ambiguous['title']:
                    title_map.pop(key, None)
                if any(ambiguous.values()):
                    logger.warning(
                        f"ms-key sync ({db_type}): {sum(len(v) for v in ambiguous.values())} "
                        f"ambiguous identity key(s) (matched more than one Plex item) excluded "
                        f"from this pass rather than guessed"
                    )

                # resolved: list of (db_id, new_rk, old_rk)
                resolved = []

                # Pass 1: rows missing ms_item_id entirely — a blank-key assignment
                # can only add an association, never invalidate an existing one, so
                # this is safe to run even against a partial inventory.
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
                # to a different ratingKey — these are split-apart items. Only
                # file-path matching is reliable here; imdb_id would resolve to
                # whichever split item Plex returns first. Requires a complete,
                # non-ambiguous inventory: this pass REPLACES an existing
                # association, so acting on partial or duplicate-matched data
                # risks invalidating a correct key instead of only fixing a
                # genuinely stale one.
                if not inventory_complete:
                    logger.warning(
                        f"ms-key sync ({db_type}): Plex inventory fetch was incomplete — "
                        f"skipping split-apart detection this run ({len(rows_split)} "
                        f"candidate(s) deferred); blank-key assignment still applied"
                    )
                else:
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
                        # Predicate pins the write to the ms_item_id value this row had
                        # when it was read above — if something else changed it
                        # concurrently (another sync pass, a manual fix), this update
                        # is a no-op (rowcount 0) instead of blindly clobbering that
                        # newer value with data read before it happened.
                        if old_rk:
                            cursor.execute(
                                'UPDATE media_items SET ms_item_id = ? WHERE id = ? AND ms_item_id = ?',
                                (new_rk, db_id, old_rk))
                        else:
                            cursor.execute(
                                "UPDATE media_items SET ms_item_id = ? WHERE id = ? "
                                "AND (ms_item_id IS NULL OR ms_item_id = '')",
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
                try:
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
                finally:
                    # An error mid-loop used to leave this connection open with an
                    # uncommitted write transaction holding the database lock.
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


def _sync_ms_keys_auto(missing_only: bool = False) -> Dict[str, int]:
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
        return _sync_library_keys_for_new_items(plex_url, plex_token, missing_only=missing_only)
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
        if not overlays_active():
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

        # Wait for the media server to index the file before taking a work slot:
        # the wait is mostly sleeping, and holding one of the 3 slots through it
        # stalled every other new item behind it for up to ~2 minutes.
        if not _wait_for_ms_item_id(item_id):
            return

        # Throttle: at most 3 threads do heavy work (DB lookups, image
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


def _needs_ms_key(ms_item_id) -> bool:
    """True if ms_item_id is absent, or a legacy Plex integer key in Jellyfin mode."""
    if not ms_item_id:
        return True
    return is_jellyfin_mode() and str(ms_item_id).isdigit()


def _read_ms_item_id(item_id: int):
    conn = _get_db_connection()
    try:
        row = conn.execute('SELECT ms_item_id FROM media_items WHERE id = ?', (item_id,)).fetchone()
    finally:
        conn.close()
    return row['ms_item_id'] if row else None


def _wait_for_ms_item_id(item_id: int, delays=(15, 30, 60)) -> bool:
    """
    Wait (outside the work semaphore) until item_id has a media server key.

    Retries a missing-keys-only library sync after each delay. Returns False when
    the key is still missing afterwards; the scheduled task_overlay_sync picks the
    item up later.
    """
    if not _needs_ms_key(_read_ms_item_id(item_id)):
        return True
    for attempt, wait in enumerate(delays, 1):
        logger.debug(
            f"apply_overlay_for_new_item: item {item_id} missing/stale ms_item_id, "
            f"waiting {wait}s before retry {attempt}/{len(delays)}")
        time.sleep(wait)
        _sync_ms_keys_auto(missing_only=True)
        if not _needs_ms_key(_read_ms_item_id(item_id)):
            return True
    logger.info(
        f"apply_overlay_for_new_item: item {item_id} ms_item_id still not found/stale "
        f"after retries — scheduled sync will handle it")
    return False


def _apply_overlay_work(item_id: int, plex_url: str, plex_token: str):
    """Inner worker called once the concurrency semaphore is held."""
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
    if _needs_ms_key(ms_item_id):
        return

    manager = OverlayManager(None, plex_url, plex_token)
    result = manager.generate_overlay_for_item(item_id, force=False)
    status = result.get('status', 'failed')

    if status == 'applied':
        _mark_siblings_applied(ms_item_id, item_type, item_id, result.get('content_hash'))
    if status == 'applied' and item_type == 'episode':
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

        _ORDER = OverlayManager._QUALITY_ORDER  # same ranking + tie-break as the renderer

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
                        ORDER BY {_ORDER}
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
                        ORDER BY {_ORDER}
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
            cursor.execute(f'''
                SELECT ms_item_id, season_number,
                       ms_resolution, ms_hdr, ms_dolby_vision,
                       ms_audio_codec, ms_audio_channels, ms_video_codec
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY ms_item_id, season_number
                        ORDER BY {_ORDER}
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


# Ratings refreshed per sync run by the content-change check. The sync runs hourly;
# MDBList's free tier is 1,000 calls a day and renders draw on the same budget when
# their cache misses, so a library catching up refreshes at most ~960 titles a day.
RATING_REFRESHES_PER_RUN = 40

# imdb_id -> when this process last tried to refresh its ratings. A title whose
# ratings can't be fetched (or aren't persisted, e.g. during a Trakt backoff) keeps
# its old cache timestamp; without this it would stay oldest-first and take the
# whole refresh budget every run.
_rating_refresh_attempts: Dict[str, float] = {}


def _load_cached_ratings(cursor, imdb_ids) -> Dict[str, tuple]:
    """imdb_id -> (fetched_at, ratings) from the in-process and SQLite ratings caches."""
    import json as _json
    cached: Dict[str, tuple] = {}
    ids = list(imdb_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        try:
            cursor.execute(
                f"SELECT imdb_id, ratings, fetched_at FROM overlay_ratings_cache "
                f"WHERE imdb_id IN ({','.join('?' * len(chunk))})", chunk)
            for r in cursor.fetchall():
                try:
                    cached[r['imdb_id']] = (float(r['fetched_at'] or 0), _json.loads(r['ratings']) or {})
                except (TypeError, ValueError):
                    continue
        except sqlite3.Error as e:
            logger.debug(f"Ratings cache read failed: {e}")
            break
    for imdb_id, entry in list(OverlayManager._ratings_cache.items()):
        if imdb_id in cached and entry[0] <= cached[imdb_id][0]:
            continue
        if imdb_id in imdb_ids:
            cached[imdb_id] = entry
    return cached


def _reset_content_changed_items(manager=None, refresh_limit: int = RATING_REFRESHES_PER_RUN) -> int:
    """
    Reset applied overlays whose content badges (ratings, show status, version
    count) no longer match what was rendered, so the main sync re-renders them.

    The hash recomputed here must be built from the same values the renderer
    used, or every mismatching item re-renders on every sync:
      - status comes from overlay_manager.show_statuses (tv_shows, then cli_battery);
      - ratings come from the shared ratings cache, refreshed through
        OverlayManager._fetch_ratings (the renderer's own fetcher);
      - version count uses the renderer's grouping.

    Ratings are refreshed oldest-first, up to `refresh_limit` titles per run, once
    they are older than overlay_content_check_interval_days. Titles with no
    ratings at all are skipped rather than compared against an all-None hash.

    Returns the number of rows reset (movies/shows + seasons).
    """
    try:
        interval_days = float(get_setting('Overlay Settings', 'overlay_content_check_interval_days',
                                          default=7))
    except Exception:
        interval_days = 7.0
    interval_s = max(interval_days, 0.0) * 86400
    now = time.time()

    try:
        conn = _get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT o.media_item_id, o.last_content_hash, m.type, m.imdb_id, m.ms_item_id
                FROM media_overlay_state o
                JOIN media_items m ON m.id = o.media_item_id
                WHERE o.status = 'applied'
                  AND o.last_content_hash IS NOT NULL
                  AND o.last_content_hash != ''
            ''')
            applied = cursor.fetchall()

            # Version counts, grouped the way the renderer counts them: shows by
            # imdb_id (copies of episodes that have more than one file), movies by
            # ms_item_id (so split Plex items each count their own rows).
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
            cursor.execute('''
                SELECT ms_item_id, COUNT(*) AS cnt
                FROM media_items
                WHERE type = 'movie'
                  AND state IN ('Collected', 'Upgrading')
                  AND ms_item_id IS NOT NULL AND ms_item_id != ''
                GROUP BY ms_item_id
            ''')
            movie_version_counts = {r['ms_item_id']: r['cnt'] for r in cursor.fetchall()}

            cursor.execute('''
                SELECT season_ms_item_id, last_content_hash, show_ms_item_id
                FROM season_overlay_state
                WHERE status = 'applied'
                  AND last_content_hash IS NOT NULL
                  AND last_content_hash != ''
            ''')
            applied_seasons = cursor.fetchall()

            # season_overlay_state holds the show key; map it to the show's imdb_id.
            show_key_to_imdb: Dict[str, str] = {}
            show_keys = list({s['show_ms_item_id'] for s in applied_seasons if s['show_ms_item_id']})
            for start in range(0, len(show_keys), 500):
                chunk = show_keys[start:start + 500]
                cursor.execute(f'''
                    SELECT DISTINCT ms_item_id, imdb_id
                    FROM media_items
                    WHERE ms_item_id IN ({','.join('?' * len(chunk))})
                      AND type = 'episode'
                      AND imdb_id IS NOT NULL AND imdb_id != ''
                ''', chunk)
                for r in cursor.fetchall():
                    show_key_to_imdb[r['ms_item_id']] = r['imdb_id']

            # imdb_id -> item type ('episode' fetches the show's ratings from Trakt)
            imdb_types: Dict[str, str] = {}
            for r in applied:
                if r['imdb_id']:
                    imdb_types.setdefault(r['imdb_id'], r['type'])
            for imdb_id in show_key_to_imdb.values():
                imdb_types.setdefault(imdb_id, 'episode')

            cached = _load_cached_ratings(cursor, set(imdb_types))
        finally:
            conn.close()

        from .overlay_manager import show_statuses
        show_imdbs = [i for i, t in imdb_types.items() if t == 'episode']
        status_map = show_statuses(show_imdbs)

        # ── Refresh the oldest ratings (network; no DB connection held) ──────
        retry_gap = max(interval_s, 3600.0)
        due = [
            i for i in imdb_types
            if now - cached.get(i, (0.0, None))[0] >= interval_s
            and now - _rating_refresh_attempts.get(i, 0.0) >= retry_gap
        ]
        due.sort(key=lambda i: cached.get(i, (0.0, None))[0])
        due = due[:max(int(refresh_limit), 0)]
        if due:
            if manager is None:
                manager = _new_overlay_manager()
            refreshed = 0
            for imdb_id in due:
                _rating_refresh_attempts[imdb_id] = now
                try:
                    data = manager._fetch_ratings(imdb_id, imdb_types[imdb_id], refresh=True)
                except Exception as e:
                    logger.debug(f"Content-change check: ratings refresh failed for {imdb_id}: {e}")
                    continue
                if data:
                    cached[imdb_id] = (time.time(), data)
                    refreshed += 1
            logger.info(f"Content-change check: refreshed ratings for {refreshed}/{len(due)} title(s)")

        def _hash(imdb_id, status, version_count):
            ratings = (cached.get(imdb_id) or (0.0, None))[1]
            if not ratings:
                return None
            return OverlayManager._compute_content_hash(
                imdb_rating=ratings.get('imdb_rating'),
                tmdb_rating=ratings.get('tmdb_rating'),
                trakt_rating=ratings.get('trakt_rating'),
                rt_critics_score=ratings.get('rt_critics_score'),
                rt_user_score=ratings.get('rt_user_score'),
                status=status,
                version_count=version_count,
            )

        reset_ids = []
        for row in applied:
            imdb_id = row['imdb_id'] or ''
            if not imdb_id:
                continue
            if row['type'] == 'episode':
                vc = show_version_counts.get(imdb_id, 0)
                status = status_map.get(imdb_id)
            else:
                ms_id = row['ms_item_id'] or ''
                vc = movie_version_counts.get(ms_id, 1) if ms_id else 1
                status = None
            current = _hash(imdb_id, status, vc)
            if current and current != row['last_content_hash']:
                reset_ids.append(row['media_item_id'])

        season_reset_keys = []
        for srow in applied_seasons:
            imdb_id = show_key_to_imdb.get(srow['show_ms_item_id'] or '', '')
            if not imdb_id:
                continue
            # Seasons don't show a version count badge.
            current = _hash(imdb_id, status_map.get(imdb_id), None)
            if current and current != srow['last_content_hash']:
                season_reset_keys.append(srow['season_ms_item_id'])

        if reset_ids or season_reset_keys:
            conn = _get_db_connection()
            try:
                if reset_ids:
                    conn.executemany(
                        "UPDATE media_overlay_state SET status='pending', "
                        "reason='Content metadata changed — overlay needs refresh', "
                        "updated_at=CURRENT_TIMESTAMP WHERE media_item_id=? AND status='applied'",
                        [(i,) for i in reset_ids])
                if season_reset_keys:
                    conn.executemany(
                        "UPDATE season_overlay_state SET status='pending', "
                        "reason='Content metadata changed — season overlay needs refresh', "
                        "updated_at=CURRENT_TIMESTAMP WHERE season_ms_item_id=? AND status='applied'",
                        [(k,) for k in season_reset_keys])
                conn.commit()
            finally:
                conn.close()
            logger.info(
                f"Content-change check: reset {len(reset_ids)} item(s) and "
                f"{len(season_reset_keys)} season(s) to pending")
        return len(reset_ids) + len(season_reset_keys)

    except Exception as e:
        logger.error(f"_reset_content_changed_items failed: {e}", exc_info=True)
        return 0


def _new_overlay_manager() -> OverlayManager:
    """OverlayManager for the active media server (credentials from settings)."""
    if is_jellyfin_mode():
        return OverlayManager(None, '', get_jellyfin_token())
    plex_url = get_setting('Plex', 'url', default='http://localhost:32400').rstrip('/')
    return OverlayManager(None, plex_url, get_setting('Plex', 'token', default=''))


def _reset_deselected_posters(plex_url: str, plex_token: str) -> int:
    """
    Reset applied items whose poster was changed in Plex, so the overlay is re-applied.

    Compares the thumb URL stored when the overlay was uploaded (its timestamp
    suffix changes when the selected poster changes) with Plex's current one.
    Compared per Plex item, not per row: a show's episodes and a merged movie's
    versions share one poster, and a sibling row holding an older URL (rows marked
    applied before plex_thumb_url was copied to siblings, or by the manual generate
    path) is not a poster change. An item is reset only when none of its rows
    holds the current URL.

    Returns the number of rows reset.
    """
    from overlays.plex_client import PlexClient as _PlexClient
    import xml.etree.ElementTree as _ET
    pc = _PlexClient(plex_url, plex_token)
    resp = pc.session.get(
        f"{plex_url}/library/sections",
        params={'X-Plex-Token': plex_token},
        headers={'Accept': 'application/xml'}, timeout=10)
    if resp.status_code != 200:
        return 0
    section_ids = [s.get('key') for s in _ET.fromstring(resp.content) if s.get('key')]
    current_thumbs = {}  # rating_key -> thumb path
    for sid in section_ids:
        current_thumbs.update(pc.get_bulk_thumb_urls(sid))
    if not current_thumbs:
        return 0

    conn = _get_db_connection()
    try:
        rows = conn.execute('''
            SELECT mos.media_item_id, mos.plex_thumb_url, mi.ms_item_id
            FROM media_overlay_state mos
            JOIN media_items mi ON mi.id = mos.media_item_id
            WHERE mos.status = 'applied'
              AND mos.plex_thumb_url IS NOT NULL
              AND mi.ms_item_id IS NOT NULL
              AND mos.overlay_applied_at < datetime('now', '-10 minutes')
        ''').fetchall()
        by_key: Dict[str, list] = {}
        for media_item_id, thumb, ms_id in rows:
            by_key.setdefault(str(ms_id), []).append((media_item_id, thumb))
        reset_ids = []
        for ms_id, group in by_key.items():
            current = current_thumbs.get(ms_id)
            if not current or any(thumb == current for _, thumb in group):
                continue
            # One row is enough: the main sync renders the group from it and
            # _mark_siblings_applied brings the rest along.
            reset_ids.append(min(mid for mid, _ in group))
        for start in range(0, len(reset_ids), 500):
            chunk = reset_ids[start:start + 500]
            conn.execute(
                f"UPDATE media_overlay_state SET status='pending', "
                f"reason='Poster deselected in Plex (thumb changed)', updated_at=CURRENT_TIMESTAMP "
                f"WHERE media_item_id IN ({','.join('?' * len(chunk))})", chunk)
        if reset_ids:
            conn.commit()
            logger.info(f"[ThumbCheck] Reset {len(reset_ids)} item(s) to pending — poster changed in Plex")
        return len(reset_ids)
    finally:
        conn.close()


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
    if not overlays_active():
        return {'success': False, 'message': 'Overlay system is disabled (or Silo, which draws its own badges)'}

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

        manager = OverlayManager(None, plex_url, plex_token)

        # Step 1.5: Reset 'applied' items whose quality improved since the overlay
        # was last generated (e.g. a 4K episode was added to a 1080p show).
        # Resets them to 'pending' so Step 2 picks them up naturally.
        quality_reset_count = _reset_quality_changed_items()
        if quality_reset_count:
            logger.info(f"Quality-change check: reset {quality_reset_count} item(s) to pending (quality upgraded)")

        # Step 1.8: Reset 'applied' items whose content badges (ratings, status,
        # version count) changed. Ratings older than overlay_content_check_interval_days
        # are refreshed a few dozen titles per run, oldest first.
        content_reset_count = _reset_content_changed_items(manager)
        if content_reset_count:
            logger.info(f"Content-change check: reset {content_reset_count} item(s) to pending (ratings/status/version changed)")

        # Step 1.9: Detect deselected overlay posters (Plex only) by comparing stored
        # thumb URLs against Plex's current thumb via one bulk fetch per library section.
        if not is_jellyfin_mode():
            try:
                _reset_deselected_posters(plex_url, plex_token)
            except Exception as _te:
                logger.debug(f"[ThumbCheck] Thumb deselection check failed (non-fatal): {_te}")

        # Step 1.6: One-pass season backfill — register seasons for any shows that
        # are already 'applied' but have no season_overlay_state rows yet.
        # This fixes existing libraries where shows were applied before season overlays
        # existed, or before a season layout was configured.
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
                    -- gate would permanently hide those shows. After processing, _mark_siblings_applied
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

        # Step 3: Process items (manager created before Step 1.5)
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
                # Mark every row sharing this ms_item_id (a show's episodes, a merged
                # movie's versions) applied so they are not rendered again.
                _mark_siblings_applied(ms_item_id, item_type, item_id, result.get('content_hash'))
                if item_type == 'episode':
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
                    _mark_siblings_applied(row['ms_item_id'], row['type'], row['id'],
                                           result.get('content_hash'))
                    if row['type'] == 'episode':
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


def _mark_siblings_applied(ms_item_id: str, item_type: str = 'episode',
                           source_item_id: int = None, content_hash: str = None):
    """
    Mark every row sharing ms_item_id as 'applied' after one of them was rendered.

    Episodes of a show share the show's poster; merged multi-version movies (several
    rows, one Plex item) share the movie's. Without this, each sibling is rendered
    and uploaded separately.

    The rendered row's poster fields (quality/layout/content hashes, upload hash,
    Plex thumb URL) are copied to the siblings when source_item_id is given. A
    sibling left with an older plex_thumb_url looks like "poster changed in Plex"
    to the next sync's thumb check, and one left without a content hash is
    invisible to the content-change check.
    """
    if item_type not in ('episode', 'movie'):
        return
    try:
        conn = _get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM media_items WHERE ms_item_id = ? AND type = ? "
                "AND state IN ('Collected', 'Upgrading')",
                (ms_item_id, item_type))
            sibling_ids = [r['id'] for r in cursor.fetchall() if r['id'] != source_item_id]
            if not sibling_ids:
                return

            src = {}
            if source_item_id is not None:
                row = cursor.execute(
                    "SELECT last_metadata_hash, last_layout_hash, last_content_hash, "
                    "last_plex_upload_hash, plex_thumb_url, textless_poster_used "
                    "FROM media_overlay_state WHERE media_item_id = ? AND status = 'applied'",
                    (source_item_id,)).fetchone()
                src = dict(row) if row else {}
            if content_hash:
                src['last_content_hash'] = content_hash

            now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            reason = ('Applied via show-level overlay sync' if item_type == 'episode'
                      else 'Applied via sibling version overlay')
            cursor.executemany('''
                INSERT INTO media_overlay_state
                    (media_item_id, status, reason, overlay_applied_at, updated_at, created_at,
                     last_metadata_hash, last_layout_hash, last_content_hash,
                     last_plex_upload_hash, plex_thumb_url, textless_poster_used)
                VALUES (?, 'applied', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_item_id) DO UPDATE SET
                    status = 'applied',
                    reason = excluded.reason,
                    overlay_applied_at = excluded.overlay_applied_at,
                    updated_at = excluded.updated_at,
                    last_metadata_hash = COALESCE(excluded.last_metadata_hash, last_metadata_hash),
                    last_layout_hash = COALESCE(excluded.last_layout_hash, last_layout_hash),
                    last_content_hash = COALESCE(excluded.last_content_hash, last_content_hash),
                    last_plex_upload_hash = COALESCE(excluded.last_plex_upload_hash, last_plex_upload_hash),
                    plex_thumb_url = COALESCE(excluded.plex_thumb_url, plex_thumb_url),
                    textless_poster_used = COALESCE(excluded.textless_poster_used, textless_poster_used)
            ''', [(sid, reason, now_str, now_str, now_str,
                   src.get('last_metadata_hash'), src.get('last_layout_hash'),
                   src.get('last_content_hash'), src.get('last_plex_upload_hash'),
                   src.get('plex_thumb_url'), src.get('textless_poster_used'))
                  for sid in sibling_ids])
            conn.commit()
            logger.debug(f"Marked {len(sibling_ids)} sibling {item_type} row(s) applied for key {ms_item_id}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Failed to mark siblings applied for {ms_item_id}: {e}")


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


_UPLOAD_PREFIX = 'upload://posters/'


_PLEX_DATA_SUBDIRS = (
    os.path.join('Application Support', 'Plex Media Server'),
    os.path.join('Library', 'Application Support', 'Plex Media Server'),
)


def check_plex_data_path(plex_data_path: str) -> Dict:
    """
    Whether a configured Plex data path contains Plex's Metadata folder.

    Returns {'ok': bool, 'suggestion': str|None}. The suggestion is the standard
    '.../Application Support/Plex Media Server' folder under the given path when
    Metadata is there instead (a path set one or two levels too high). Used by the
    Overlay Settings check and by the cleanup task, which never guesses.
    """
    path = (plex_data_path or '').strip()
    if not path:
        return {'ok': False, 'suggestion': None}
    if os.path.isdir(os.path.join(path, 'Metadata')):
        return {'ok': True, 'suggestion': None}
    for sub in _PLEX_DATA_SUBDIRS:
        candidate = os.path.join(path, sub)
        if os.path.isdir(os.path.join(candidate, 'Metadata')):
            return {'ok': False, 'suggestion': candidate}
    return {'ok': False, 'suggestion': None}


def _resolve_plex_metadata_root(plex_data_path: str):
    """
    The Plex 'Metadata' folder under the configured Plex data path, or None.

    Never guesses: if the folder is further down (the path points at .../Library
    instead of .../Plex Media Server), logs the path to set and returns None, so
    a cleanup that deletes files only runs against the folder the user named.
    """
    check = check_plex_data_path(plex_data_path)
    if check['ok']:
        return os.path.join(plex_data_path, 'Metadata')
    if check['suggestion']:
        logger.warning(
            f"Plex poster cleanup: no Metadata folder in '{plex_data_path}' — skipping. "
            f"Set the Plex data path to '{check['suggestion']}' to enable it.")
    else:
        logger.warning(f"Plex poster cleanup: no Metadata folder in '{plex_data_path}' — skipping.")
    return None


def _collection_upload_hashes(plex_url: str, plex_token: str) -> set:
    """Upload hashes of the posters cli_debrid manages on Plex collections."""
    import json as _json
    import requests as _requests
    hashes = set()
    rks = []
    try:
        conn = _get_db_connection()
        try:
            for row in conn.execute(
                    'SELECT movie_collection_ratingkey, show_collection_ratingkey '
                    'FROM plex_collection_sync').fetchall():
                rks.extend(str(rk) for rk in (row[0], row[1]) if rk)
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"Poster cleanup: collection list read failed: {e}")
    try:
        from utilities.settings import get_all_settings
        smart = get_all_settings().get('Plex Smart Collections', {})
        colls = smart.get('collections', {}) if isinstance(smart, dict) else {}
        if isinstance(colls, dict):
            rks.extend(str(rk) for rk in colls.keys())
    except Exception as e:
        logger.debug(f"Poster cleanup: smart collection list read failed: {e}")

    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
    for rk in rks:
        try:
            r = _requests.get(f"{plex_url}/library/metadata/{rk}/posters", headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            for p in r.json().get('MediaContainer', {}).get('Metadata', []):
                key = p.get('ratingKey', '')
                if p.get('selected') and key.startswith(_UPLOAD_PREFIX):
                    hashes.add(key[len(_UPLOAD_PREFIX):])
        except Exception as e:
            logger.debug(f"Poster cleanup: collection poster lookup failed for {rk}: {e}")

    config_dir = os.environ.get('USER_CONFIG', '/user/config')
    try:
        path = os.path.join(config_dir, 'plex_collection_state.json')
        if os.path.exists(path):
            with open(path) as f:
                for entry in _json.load(f).values():
                    if isinstance(entry, dict):
                        hashes.update(h for h in entry.get('plex_upload_hashes', {}).values() if h)
    except Exception as e:
        logger.debug(f"Poster cleanup: collection state read failed: {e}")
    try:
        path = os.path.join(config_dir, 'plex_boxsets_state.json')
        if os.path.exists(path):
            with open(path) as f:
                hashes.update(h for h in _json.load(f).get('collection_poster_hashes', {}).values() if h)
    except Exception as e:
        logger.debug(f"Poster cleanup: boxsets state read failed: {e}")
    return hashes


def _recorded_upload_hashes() -> set:
    """Every upload hash recorded on an overlay row, whatever its status."""
    conn = _get_db_connection()
    try:
        hashes = {r[0] for r in conn.execute(
            "SELECT last_plex_upload_hash FROM media_overlay_state "
            "WHERE last_plex_upload_hash IS NOT NULL AND last_plex_upload_hash != ''")}
        hashes.update(r[0] for r in conn.execute(
            "SELECT last_plex_upload_hash FROM season_overlay_state "
            "WHERE last_plex_upload_hash IS NOT NULL AND last_plex_upload_hash != ''"))
    finally:
        conn.close()
    return hashes


def _cleanup_overlay_upload_files(metadata_root: str, client, keep_hashes: set,
                                  since: str = None) -> Dict:
    """
    Delete old overlay uploads from the Plex bundles of items cli_debrid overlaid.

    Scope: Plex items (movies/shows and seasons) that have had an overlay applied,
    limited to those applied or updated since `since` (UTC 'YYYY-MM-DD HH:MM:SS';
    None = all). Old uploads only pile up when an overlay is re-uploaded, so
    anything older was handled by an earlier run.

    For each item the poster list comes from Plex. Only uploaded posters Plex
    reports as NOT selected are deleted, and never one whose hash is selected on
    any item checked this run or is in keep_hashes (recorded overlay uploads,
    collection posters). An item whose poster list can't be fetched is skipped.

    Returns {'deleted', 'errors', 'bytes', 'stale_keys', 'item_hashes', 'season_hashes'}.
    """
    import glob as _glob
    from concurrent.futures import ThreadPoolExecutor
    import requests as _req

    window = "AND (? IS NULL OR o.overlay_applied_at >= ? OR o.updated_at >= ?)"
    conn = _get_db_connection()
    try:
        item_keys = [r[0] for r in conn.execute(f'''
            SELECT DISTINCT m.ms_item_id
            FROM media_items m
            JOIN media_overlay_state o ON m.id = o.media_item_id
            WHERE o.overlay_applied_at IS NOT NULL
              AND m.ms_item_id IS NOT NULL AND m.ms_item_id != ''
              {window}
        ''', (since, since, since))]
        season_keys = [r[0] for r in conn.execute(f'''
            SELECT DISTINCT o.season_ms_item_id
            FROM season_overlay_state o
            WHERE o.overlay_applied_at IS NOT NULL
              AND o.season_ms_item_id IS NOT NULL AND o.season_ms_item_id != ''
              {window}
        ''', (since, since, since))]
    finally:
        conn.close()

    out = {'deleted': 0, 'errors': 0, 'bytes': 0, 'stale_keys': [],
           'item_hashes': {}, 'season_hashes': {}}
    keys = [(k, False) for k in item_keys] + [(k, True) for k in season_keys]
    if not keys:
        return out
    logger.info(f"Plex poster cleanup: checking {len(item_keys)} item(s) and {len(season_keys)} season(s)")

    def _fetch(rk):
        try:
            return rk, client.get_poster_list(rk, strict=True), None
        except Exception as e:
            return rk, None, e

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda k: (_fetch(k[0]), k[1]), keys))

    poster_lists = []
    keep = set(keep_hashes)
    for (rk, posters, err), is_season in results:
        if err is not None:
            status = getattr(getattr(err, 'response', None), 'status_code', None)
            if isinstance(err, _req.exceptions.HTTPError) and status == 404 and not is_season:
                out['stale_keys'].append(rk)
            else:
                logger.warning(f"Plex poster cleanup: skipping {rk} — poster list unavailable ({err})")
                out['errors'] += 1
            continue
        for p in posters or []:
            key = p.get('ratingKey', '')
            if p.get('selected') and key.startswith(_UPLOAD_PREFIX) and key[len(_UPLOAD_PREFIX):]:
                selected = key[len(_UPLOAD_PREFIX):]
                keep.add(selected)
                keep.add(selected.split('/')[-1])
                (out['season_hashes'] if is_season else out['item_hashes'])[rk] = selected
        poster_lists.append((rk, posters or []))

    for rk, posters in poster_lists:
        for p in posters:
            key = p.get('ratingKey', '')
            if p.get('selected') or not key.startswith(_UPLOAD_PREFIX):
                continue
            suffix = key[len(_UPLOAD_PREFIX):]
            flat = suffix.split('/')[-1]
            if not flat or suffix in keep or flat in keep:
                continue
            matches = _glob.glob(os.path.join(
                metadata_root, '*', '*', '*.bundle', 'Uploads', 'posters', suffix))
            if not matches and flat != suffix:
                # Season uploads can be listed as 'seasons/N/<hash>' but stored flat
                matches = _glob.glob(os.path.join(
                    metadata_root, '*', '*', '*.bundle', 'Uploads', 'posters', flat))
            for fp in matches:
                if os.path.basename(fp) in keep:
                    continue
                try:
                    try:
                        out['bytes'] += os.path.getsize(fp)
                    except OSError:
                        pass
                    os.remove(fp)
                    out['deleted'] += 1
                    logger.debug(f"Plex poster cleanup: deleted '{fp}' (key {rk})")
                except Exception as e:
                    logger.error(f"Plex poster cleanup: failed '{fp}': {e}")
                    out['errors'] += 1
    return out


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
    if not overlays_active():
        return {'success': False, 'message': 'Overlay system is disabled (or Silo, which draws its own badges)'}

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
        total_deleted = 0
        total_errors  = 0
        total_bytes   = 0

        if not is_jellyfin_mode():
            plex_data_path = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
            plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
            plex_token = get_setting('Plex', 'token', default='')
            metadata_root = _resolve_plex_metadata_root(plex_data_path) if plex_data_path else None
            if not plex_data_path:
                logger.info("Plex poster cleanup: 'plex_data_path' not configured — skipping.")
            elif not plex_token:
                logger.warning("Plex poster cleanup: Plex token not configured — skipping.")
            elif metadata_root:
                try:
                    from .plex_client import PlexClient
                    _run_started = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                    _sc = _get_db_connection()
                    try:
                        _row = _sc.execute(
                            "SELECT value FROM overlay_sync_state WHERE key = 'last_poster_cleanup_at'"
                        ).fetchone()
                    finally:
                        _sc.close()
                    _since = _row[0] if _row else None

                    keep_hashes = _recorded_upload_hashes()
                    keep_hashes |= _collection_upload_hashes(plex_url, plex_token)
                    res = _cleanup_overlay_upload_files(
                        metadata_root, PlexClient(plex_url, plex_token, timeout=10),
                        keep_hashes, since=_since)
                    total_deleted, total_errors, total_bytes = res['deleted'], res['errors'], res['bytes']

                    _w = _get_db_connection()
                    try:
                        # Record the selected upload hash where none was stored yet
                        for _ms_id, _uhash in res['item_hashes'].items():
                            _w.execute(
                                """UPDATE media_overlay_state SET last_plex_upload_hash = ?
                                    WHERE media_item_id IN (SELECT id FROM media_items WHERE ms_item_id = ?)
                                      AND (last_plex_upload_hash IS NULL OR last_plex_upload_hash = '')""",
                                (_uhash, _ms_id))
                        for _sms_id, _suhash in res['season_hashes'].items():
                            _w.execute(
                                """UPDATE season_overlay_state SET last_plex_upload_hash = ?
                                    WHERE season_ms_item_id = ?
                                      AND (last_plex_upload_hash IS NULL OR last_plex_upload_hash = '')""",
                                (_suhash, _sms_id))
                        # Plex no longer knows these keys: clear them so the sync re-keys them
                        for _sk in res['stale_keys']:
                            _ids = [r[0] for r in _w.execute(
                                'SELECT id FROM media_items WHERE ms_item_id = ?', (_sk,))]
                            _w.execute('UPDATE media_items SET ms_item_id = NULL WHERE ms_item_id = ?', (_sk,))
                            if _ids:
                                _w.execute(
                                    f"UPDATE media_overlay_state SET status='pending', "
                                    f"reason='Stale ms_item_id cleared during poster cleanup', "
                                    f"updated_at=CURRENT_TIMESTAMP "
                                    f"WHERE media_item_id IN ({','.join('?' * len(_ids))})", _ids)
                        # Only move the window forward after a clean run, so items whose
                        # poster list couldn't be fetched are checked again next time.
                        if total_errors == 0:
                            _w.execute('''
                                INSERT INTO overlay_sync_state (key, value, updated_at)
                                VALUES ('last_poster_cleanup_at', ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                               updated_at = CURRENT_TIMESTAMP
                            ''', (_run_started,))
                        if total_deleted > 0:
                            for _k, _v in (('cleanup_total_posters', total_deleted),
                                           ('cleanup_total_bytes', total_bytes)):
                                _w.execute('''
                                    INSERT INTO overlay_sync_state (key, value, updated_at)
                                    VALUES (?, ?, CURRENT_TIMESTAMP)
                                    ON CONFLICT(key) DO UPDATE SET
                                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                                        updated_at = CURRENT_TIMESTAMP
                                ''', (_k, str(_v), _v))
                        _w.commit()
                    finally:
                        _w.close()
                    logger.info(
                        f"Plex poster cleanup: {total_deleted} deleted, "
                        f"{round(total_bytes / (1024 * 1024), 2)} MB reclaimed, {total_errors} error(s)")
                except Exception as _pce:
                    logger.warning(f"Plex poster cleanup failed: {_pce}")
                    total_errors += 1

        _activity_title = f"Cleanup: {total_removed} record(s) removed, {total_reset} reset"
        if total_deleted > 0:
            _activity_title += (f", {total_deleted} poster(s) deleted "
                                f"({round(total_bytes / (1024 * 1024), 2)} MB)")
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


def task_overlay_full_sync(force: bool = False):
    """
    Full overlay sync task.

    Processes ALL Collected media items with media server item IDs, optionally forcing
    regeneration. TV shows are deduplicated by ms_item_id. This is a heavy
    operation and should be run manually or scheduled rarely.

    Args:
        force: Force regeneration even for items with existing overlays
    """
    if not overlays_active():
        return {'success': False, 'message': 'Overlay system is disabled (or Silo, which draws its own badges)'}

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
                    _mark_siblings_applied(ms_item_id, item_type, item_id, result.get('content_hash'))
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
