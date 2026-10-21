"""
Poster Reset

Foreign overlay removal — restores clean TMDB/TVDB posters for all or selected
media items regardless of whether overlays were applied by cli_debrid or any other app.

Completely separate from the internal remove/restore flow.
Does NOT touch media_overlay_state — items are left as-is or set to 'pending'
so the user decides when to re-apply overlays.

After reset, the existing cleanup task (task_overlay_cleanup / cleanup poster cache)
should be run to sweep orphaned overlay uploads from the media server.
"""

import logging
import threading
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# ── Job state ────────────────────────────────────────────────────────────────
# Stored in-process; survives as long as the worker process is up.
# A simple dict is sufficient — only one reset job runs at a time.
_job_lock = threading.Lock()
_job: Dict[str, Any] = {
    'running': False,
    'total':   0,
    'done':    0,
    'failed':  0,
    'skipped': 0,
    'current': '',
    'errors':  [],   # list of "title: reason" strings (capped at 50)
    'started_at': None,
    'finished_at': None,
    'cancelled': False,
}


def get_job_status() -> Dict[str, Any]:
    with _job_lock:
        return dict(_job)


def cancel_job():
    with _job_lock:
        if _job['running']:
            _job['cancelled'] = True


def _update_job(**kwargs):
    with _job_lock:
        _job.update(kwargs)


def _add_error(msg: str):
    with _job_lock:
        if len(_job['errors']) < 50:
            _job['errors'].append(msg)


# ── TMDB season poster helper ─────────────────────────────────────────────────

def _fetch_tmdb_season_poster(tmdb_id: str, season_number: int) -> Optional[bytes]:
    """
    Fetch a season poster from TMDB directly via API.
    Falls back gracefully if TMDB has no season poster.
    """
    try:
        import requests as _req
        from utilities.settings import get_setting
        api_key = get_setting('TMDB', 'api_key', default='') or get_setting('Metadata', 'tmdb_api_key', default='')
        if not api_key or not tmdb_id:
            return None
        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}/images"
        resp = _req.get(url, params={'api_key': api_key}, timeout=10)
        resp.raise_for_status()
        posters = resp.json().get('posters', [])
        if not posters:
            return None
        best = sorted(posters, key=lambda p: p.get('vote_average', 0), reverse=True)[0]
        img_path = best.get('file_path')
        if not img_path:
            return None
        img_url = f"https://image.tmdb.org/t/p/w780{img_path}"
        img_resp = _req.get(img_url, timeout=15)
        img_resp.raise_for_status()
        return img_resp.content
    except Exception as e:
        logger.debug(f"TMDB season poster fetch failed (tmdb_id={tmdb_id} s{season_number}): {e}")
        return None


# ── Per-item reset ────────────────────────────────────────────────────────────

def _reset_one_item(plex_client, cache_mgr, row: Dict) -> Dict[str, str]:
    """
    Reset a single movie or show poster to a clean TMDB original.
    Returns {'status': 'ok'|'skipped'|'failed', 'message': str}
    """
    ms_item_id = row.get('ms_item_id')
    title = row.get('title', ms_item_id)

    if not ms_item_id:
        return {'status': 'skipped', 'message': 'No ms_item_id'}

    # Fetch clean TMDB poster
    poster_bytes = cache_mgr._fetch_tmdb_poster_bytes(row)
    if not poster_bytes or len(poster_bytes) < 5120:
        return {'status': 'skipped', 'message': f'No TMDB poster available for {title}'}

    # Upload to media server
    try:
        if plex_client.upload_poster(ms_item_id, poster_bytes):
            return {'status': 'ok', 'message': f'Reset poster for {title}'}
        else:
            return {'status': 'failed', 'message': f'Upload failed for {title}'}
    except Exception as e:
        return {'status': 'failed', 'message': f'Upload error for {title}: {e}'}


def _reset_one_season(plex_client, season: Dict, show_tmdb_id: str) -> Dict[str, str]:
    """
    Reset a single season poster. Tries TMDB season poster, falls back to show poster download.
    """
    season_key = season.get('ratingKey')
    season_num  = season.get('index', 0)
    label = f"Season {season_num} ({season_key})"

    poster_bytes = None

    # 1. Try TMDB season-specific poster
    if show_tmdb_id:
        poster_bytes = _fetch_tmdb_season_poster(show_tmdb_id, season_num)

    # 2. Fallback: download current metadata:// poster from Plex and re-upload
    #    (same technique as our existing remove_season_overlay)
    if not poster_bytes or len(poster_bytes) < 5120:
        try:
            poster_list = plex_client.get_poster_list(season_key)
            clean = next(
                (p for p in poster_list if p.get('ratingKey', '').startswith('metadata://') and not p.get('selected')),
                None
            ) or next(
                (p for p in poster_list if not p.get('ratingKey', '').startswith('upload://') and not p.get('selected')),
                None
            )
            if clean:
                poster_bytes = plex_client.download_poster_by_rating_key(season_key, clean['ratingKey'])
        except Exception as e:
            logger.debug(f"Season poster fallback failed for {label}: {e}")

    if not poster_bytes or len(poster_bytes) < 5120:
        return {'status': 'skipped', 'message': f'No clean poster found for {label}'}

    if plex_client.upload_poster(season_key, poster_bytes):
        return {'status': 'ok', 'message': f'Reset {label}'}
    else:
        return {'status': 'failed', 'message': f'Upload failed for {label}'}


# ── Worker ────────────────────────────────────────────────────────────────────

def _run_reset_job(ms_item_ids: Optional[List[str]], reset_seasons: bool):
    """
    Background worker. ms_item_ids=None means reset ALL items.
    """
    from database.core import get_db_connection
    from overlays.cache_cleanup import PosterCacheManager
    from overlays.plex_client import PlexClient
    from utilities.settings import get_setting
    from overlays.scheduled_tasks import task_overlay_cleanup
    from overlays.activity_logger import log_activity
    import sqlite3

    plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
    plex_token = get_setting('Plex', 'token', default='')
    plex_client = PlexClient(plex_url, plex_token)
    cache_mgr   = PosterCacheManager(None)

    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        if ms_item_ids is None:
            # Reset ALL collected items
            rows = conn.execute('''
                SELECT MIN(id) as id, MAX(ms_item_id) as ms_item_id,
                       MAX(imdb_id) as imdb_id, MAX(tmdb_id) as tmdb_id,
                       MAX(type) as type, MAX(title) as title
                FROM media_items
                WHERE ms_item_id IS NOT NULL AND ms_item_id != ''
                  AND state IN ('Collected', 'Upgrading')
                GROUP BY ms_item_id
            ''').fetchall()
        else:
            placeholders = ','.join('?' * len(ms_item_ids))
            rows = conn.execute(f'''
                SELECT MIN(id) as id, MAX(ms_item_id) as ms_item_id,
                       MAX(imdb_id) as imdb_id, MAX(tmdb_id) as tmdb_id,
                       MAX(type) as type, MAX(title) as title
                FROM media_items
                WHERE ms_item_id IN ({placeholders})
                  AND ms_item_id IS NOT NULL AND ms_item_id != ''
                GROUP BY ms_item_id
            ''', ms_item_ids).fetchall()

        conn.close()

        rows = [dict(r) for r in rows]
        _update_job(total=len(rows), done=0, failed=0, skipped=0, errors=[])
        logger.info(f"Poster reset job started: {len(rows)} item(s)")

        done = failed = skipped = 0

        for row in rows:
            if get_job_status().get('cancelled'):
                logger.info("Poster reset job cancelled by user")
                break

            title = row.get('title', row.get('ms_item_id', '?'))
            _update_job(current=title)

            try:
                result = _reset_one_item(plex_client, cache_mgr, row)
            except Exception as _item_exc:
                logger.warning(f"Poster reset skipping '{title}' due to error: {_item_exc}")
                result = {'status': 'failed', 'message': str(_item_exc)}

            if result['status'] == 'ok':
                done += 1
                # Handle seasons for shows
                if reset_seasons and row.get('type') == 'episode':
                    show_key = row['ms_item_id']
                    show_tmdb = row.get('tmdb_id', '')
                    try:
                        seasons = plex_client.get_show_seasons(show_key)
                        for season in seasons:
                            if get_job_status().get('cancelled'):
                                break
                            _update_job(current=f"{title} — Season {season.get('index', '?')}")
                            sr = _reset_one_season(plex_client, season, show_tmdb)
                            if sr['status'] == 'failed':
                                failed += 1
                                _add_error(sr['message'])
                            elif sr['status'] == 'skipped':
                                skipped += 1
                    except Exception as se:
                        logger.warning(f"Season reset failed for {show_key}: {se}")
            elif result['status'] == 'failed':
                failed += 1
                _add_error(result['message'])
            else:
                skipped += 1

            _update_job(done=done, failed=failed, skipped=skipped)
            # Small throttle to avoid hammering media server
            time.sleep(0.1)

        # Mark all processed items as pending so user can re-apply overlays when ready
        if ms_item_ids is None:
            # All items
            try:
                conn2 = get_db_connection()
                conn2.execute(
                    "UPDATE media_overlay_state SET status = 'pending', "
                    "reason = 'Reset to original poster', updated_at = CURRENT_TIMESTAMP"
                )
                conn2.execute(
                    "UPDATE season_overlay_state SET status = 'pending', "
                    "reason = 'Reset to original poster', updated_at = CURRENT_TIMESTAMP "
                    "WHERE status != 'user_removed'"
                )
                conn2.commit()
                conn2.close()
            except Exception as e:
                logger.warning(f"Failed to reset overlay states after full reset: {e}")
        elif ms_item_ids:
            try:
                conn2 = get_db_connection()
                ph = ','.join('?' * len(ms_item_ids))
                conn2.execute(f'''
                    UPDATE media_overlay_state SET status = 'pending',
                    reason = 'Reset to original poster', updated_at = CURRENT_TIMESTAMP
                    WHERE media_item_id IN (
                        SELECT id FROM media_items WHERE ms_item_id IN ({ph})
                    )
                ''', ms_item_ids)
                conn2.execute(f'''
                    UPDATE season_overlay_state SET status = 'pending',
                    reason = 'Reset to original poster', updated_at = CURRENT_TIMESTAMP
                    WHERE show_ms_item_id IN ({ph})
                      AND status != 'user_removed'
                ''', ms_item_ids)
                conn2.commit()
                conn2.close()
            except Exception as e:
                logger.warning(f"Failed to reset overlay states for selected items: {e}")

        # Run existing overlay cleanup to sweep orphaned upload posters from media server
        logger.info("Poster reset complete — running overlay cleanup task")
        try:
            task_overlay_cleanup()
        except Exception as ce:
            logger.warning(f"Post-reset cleanup task failed: {ce}")

        final_status = get_job_status()
        log_activity(
            'poster_reset',
            triggered_by='manual',
            result='success' if final_status['failed'] == 0 else 'partial',
            title=f"Poster reset: {done} reset, {failed} failed, {skipped} skipped",
            stats={
                'reset': done, 'failed': failed, 'skipped': skipped,
                'total': len(rows),
                'failures': final_status['errors'] if final_status['errors'] else None,
            }
        )

        _update_job(
            running=False,
            finished_at=time.time(),
            current='',
        )
        logger.info(f"Poster reset job finished: {done} reset, {failed} failed, {skipped} skipped")

    except Exception as e:
        logger.error(f"Poster reset job crashed: {e}", exc_info=True)
        _update_job(running=False, finished_at=time.time(), current='')
        _add_error(f"Job crashed: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def start_reset_job(ms_item_ids: Optional[List[str]] = None, reset_seasons: bool = True) -> bool:
    """
    Start a background poster reset job.
    ms_item_ids=None → reset all items.
    Returns False if a job is already running.
    """
    with _job_lock:
        if _job['running']:
            return False
        _job.update({
            'running': True,
            'total': 0,
            'done': 0,
            'failed': 0,
            'skipped': 0,
            'current': 'Starting…',
            'errors': [],
            'started_at': time.time(),
            'finished_at': None,
            'cancelled': False,
        })

    t = threading.Thread(
        target=_run_reset_job,
        args=(ms_item_ids, reset_seasons),
        daemon=True,
        name='poster-reset-worker',
    )
    t.start()
    return True


def get_preview_items() -> List[Dict]:
    """
    Return all library items with their current poster thumbnail URL and overlay status
    for the Phase 2 review grid.
    """
    from database.core import get_db_connection
    from routes.poster_cache import get_cached_poster_url
    import sqlite3

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    rows = conn.execute('''
        SELECT
            MIN(m.id)                           AS id,
            MAX(m.ms_item_id)                   AS ms_item_id,
            MAX(m.imdb_id)                      AS imdb_id,
            MAX(m.tmdb_id)                      AS tmdb_id,
            MAX(m.type)                         AS type,
            MAX(m.title)                        AS title,
            MAX(m.year)                         AS year,
            COALESCE(MAX(o.status), 'pending')  AS overlay_status
        FROM media_items m
        LEFT JOIN media_overlay_state o ON m.id = o.media_item_id
        WHERE m.ms_item_id IS NOT NULL AND m.ms_item_id != ''
          AND m.state IN ("Collected", "Upgrading")
        GROUP BY m.ms_item_id
        ORDER BY MAX(m.title)
    ''').fetchall()
    conn.close()

    items = []
    for row in rows:
        r = dict(row)
        media_type = 'movie' if r.get('type') == 'movie' else 'tv'
        tmdb_url = (
            get_cached_poster_url(r.get('imdb_id'), media_type)
            or get_cached_poster_url(r.get('tmdb_id'), media_type)
        )
        r['tmdb_poster_url'] = tmdb_url
        items.append(r)

    return items
