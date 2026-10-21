"""
Poster Cache Cleanup

Manages poster cache, including cleanup, backup, and restoration of original posters.
"""

import hashlib
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# ── Remove-All job state ──────────────────────────────────────────────────────
_remove_all_lock = threading.Lock()
_remove_all_job: Dict[str, Any] = {
    'running':     False,
    'total':       0,
    'done':        0,
    'failed':      0,
    'current':     '',
    'errors':      [],
    'restored':    0,
    'started_at':  None,
    'finished_at': None,
}


def get_remove_all_status() -> Dict[str, Any]:
    with _remove_all_lock:
        return dict(_remove_all_job)


def _update_remove_all(**kwargs):
    with _remove_all_lock:
        _remove_all_job.update(kwargs)


def _get_db_connection():
    from database.core import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn


class PosterCacheManager:
    """
    Manages poster cache cleanup and restoration.

    Handles:
    - Detecting Plex poster cache location
    - Calculating cache size
    - Backing up original posters before overlay
    - Restoring original posters
    - Cleaning up orphaned overlay posters
    """

    def __init__(self, db_path=None, backup_dir: str = "/user/config/poster_backups"):
        """
        Initialize poster cache manager.

        Args:
            db_path: Unused — kept for API compatibility. DB access uses get_db_connection().
            backup_dir: Directory for poster backups
        """
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(__name__)

        # Common Plex cache locations
        self.plex_cache_paths = [
            Path("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Cache"),
            Path("/config/Library/Application Support/Plex Media Server/Cache"),
            Path(os.path.expanduser("~/Library/Application Support/Plex Media Server/Cache")),
        ]

    def detect_plex_cache(self) -> Optional[Path]:
        """
        Detect Plex poster cache location.

        Returns:
            Path to Plex cache directory or None if not found
        """
        for cache_path in self.plex_cache_paths:
            if cache_path.exists():
                self.logger.info(f"Found Plex cache at: {cache_path}")
                return cache_path

        self.logger.debug("Could not detect Plex cache location")
        return None

    def calculate_cache_size(self) -> Dict[str, Any]:
        """
        Calculate total Plex cache size.

        Returns:
            Dictionary with cache statistics
        """
        cache_path = self.detect_plex_cache()

        if not cache_path:
            return {
                'success': False,
                'message': 'Plex cache not found',
                'total_size': 0,
                'file_count': 0
            }

        try:
            total_size = 0
            file_count = 0

            # Walk through cache directory
            for root, dirs, files in os.walk(cache_path):
                for file in files:
                    file_path = Path(root) / file
                    try:
                        total_size += file_path.stat().st_size
                        file_count += 1
                    except:
                        pass

            return {
                'success': True,
                'cache_path': str(cache_path),
                'total_size': total_size,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'total_size_gb': round(total_size / (1024 * 1024 * 1024), 2),
                'file_count': file_count
            }

        except Exception as e:
            self.logger.error(f"Failed to calculate cache size: {e}")
            return {
                'success': False,
                'message': str(e),
                'total_size': 0,
                'file_count': 0
            }

    def backup_poster(self, ms_item_id: str, poster_data: bytes) -> bool:
        """
        Backup original poster before applying overlay.

        Args:
            ms_item_id: Media server item ID
            poster_data: Original poster image data

        Returns:
            True if backup successful
        """
        try:
            # Create backup filename
            backup_file = self.backup_dir / f"{ms_item_id}_original.jpg"

            # Save backup
            with open(backup_file, 'wb') as f:
                f.write(poster_data)

            self.logger.info(f"Backed up poster for {ms_item_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to backup poster for {ms_item_id}: {e}")
            return False

    def backup_season_poster(self, season_ms_item_id: str, poster_data: bytes) -> bool:
        """
        Backup original season poster before applying overlay.

        Stored separately from show/movie backups using a 'season_' prefix.

        Args:
            season_ms_item_id: Media server season item ID
            poster_data: Original season poster image data

        Returns:
            True if backup successful
        """
        try:
            backup_file = self.backup_dir / f"season_{season_ms_item_id}_original.jpg"
            with open(backup_file, 'wb') as f:
                f.write(poster_data)
            self.logger.info(f"Backed up season poster for {season_ms_item_id}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to backup season poster for {season_ms_item_id}: {e}")
            return False

    def _fetch_tmdb_poster_bytes(self, item: Dict[str, Any]) -> Optional[bytes]:
        """
        Fetch a clean poster from TMDB for a media item.

        Args:
            item: Dict with imdb_id, tmdb_id, type fields

        Returns:
            Raw image bytes or None
        """
        try:
            from routes.poster_cache import get_cached_poster_url
            import requests as _requests

            imdb_id = item.get('imdb_id')
            tmdb_id = item.get('tmdb_id')
            media_type = 'movie' if item.get('type') == 'movie' else 'tv'

            cached_url = (
                (get_cached_poster_url(imdb_id, media_type) if imdb_id else None)
                or (get_cached_poster_url(tmdb_id, media_type) if tmdb_id else None)
            )

            if cached_url and 'image.tmdb.org' in cached_url:
                if '/t/p/' in cached_url:
                    slug = cached_url.split('/t/p/', 1)[1].split('/', 1)[-1]
                    tmdb_img_url = f"https://image.tmdb.org/t/p/w780/{slug}"
                else:
                    tmdb_img_url = cached_url

                resp = _requests.get(tmdb_img_url, timeout=15)
                resp.raise_for_status()
                self.logger.info(f"Fetched TMDB poster from {tmdb_img_url}")
                return resp.content

        except Exception as e:
            self.logger.warning(f"TMDB poster fetch failed: {e}")

        return None

    def restore_poster(self, media_item_id: int, ms_item_id: str) -> Dict[str, Any]:
        """
        Restore original poster for an item.

        Tries (in order):
        1. Local backup file saved before overlay was applied
        2. Fresh download from TMDB
        3. Jellyfin: DELETE /Items/{id}/Images/Primary (reverts to metadata poster)
           Plex:     delete all custom uploaded posters (nuclear fallback)

        Args:
            media_item_id: Database media item ID
            ms_item_id: Media server item ID

        Returns:
            Result dictionary
        """
        try:
            from .utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token
            from utilities.settings import get_setting

            _jellyfin = is_jellyfin_mode()

            if _jellyfin:
                from .jellyfin_client import JellyfinClient
                client = JellyfinClient(get_jellyfin_url(), get_jellyfin_token())
            else:
                from .plex_client import PlexClient
                plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
                plex_token = get_setting('Plex', 'token', default='')
                client = PlexClient(plex_url, plex_token)

            poster_data = None
            source = None

            backup_file = self.backup_dir / f"{ms_item_id}_original.jpg"

            # 1. Try local backup first — fast local disk read, avoids external network calls.
            if backup_file.exists():
                backup_size = backup_file.stat().st_size
                if backup_size > 5120:
                    with open(backup_file, 'rb') as f:
                        poster_data = f.read()
                    source = 'backup'
                    self.logger.info(f"Using backup for {ms_item_id} ({backup_size} bytes)")
                else:
                    self.logger.warning(
                        f"Backup for {ms_item_id} is too small ({backup_size} bytes), skipping"
                    )

            # 2. Fallback: fetch clean original from TMDB (used when no backup exists).
            if not poster_data:
                conn = _get_db_connection()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    'SELECT imdb_id, tmdb_id, type FROM media_items WHERE ms_item_id = ? LIMIT 1',
                    (ms_item_id,)
                )
                row = cursor.fetchone()
                conn.close()
                if row:
                    self.logger.info(f"No backup — fetching TMDB poster for {ms_item_id}...")
                    poster_data = self._fetch_tmdb_poster_bytes(dict(row))
                    if poster_data:
                        source = 'tmdb'
                    else:
                        self.logger.warning(f"TMDB poster fetch failed for {ms_item_id}")

            if not poster_data:
                if _jellyfin:
                    # Jellyfin nuclear fallback: DELETE /Items/{id}/Images/Primary
                    # This removes the custom uploaded image and reverts to metadata poster.
                    try:
                        if client.delete_poster(ms_item_id):
                            self._mark_all_removed(ms_item_id, 'Overlay image deleted (Jellyfin fallback)')
                            self._increment_cleanup_stats(0)
                            try:
                                if backup_file.exists():
                                    backup_file.unlink()
                            except Exception:
                                pass
                            self._delete_season_backups(ms_item_id)
                            return {
                                'success': True,
                                'message': 'Overlay removed — Jellyfin poster image deleted'
                            }
                        else:
                            self.logger.warning(f"Jellyfin DELETE poster failed for {ms_item_id}")
                    except Exception as _jf_e:
                        self.logger.warning(f"Jellyfin DELETE poster exception for {ms_item_id}: {_jf_e}")
                    return {
                        'success': False,
                        'message': f'No backup or TMDB poster found and Jellyfin delete failed for {ms_item_id}'
                    }
                else:
                    # Plex nuclear fallback: delete all custom uploaded posters so Plex
                    # falls back to its own online metadata poster.
                    import glob as _glob
                    plex_data_path = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
                    try:
                        poster_list = client.get_poster_list(ms_item_id)
                        upload_keys = [p.get('ratingKey', '') for p in poster_list
                                       if p.get('ratingKey', '').startswith('upload://posters/')]
                        if upload_keys:
                            deleted = 0
                            for k in upload_keys:
                                upload_hash = k[len('upload://posters/'):]
                                # Try API delete first
                                if client.delete_poster(ms_item_id, k):
                                    deleted += 1
                                elif plex_data_path and os.path.isdir(plex_data_path) and upload_hash:
                                    # Fall back to disk deletion
                                    pattern = os.path.join(
                                        plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                        'Uploads', 'posters', upload_hash
                                    )
                                    for fpath in _glob.glob(pattern):
                                        try:
                                            os.remove(fpath)
                                            deleted += 1
                                            self.logger.info(
                                                f"Deleted overlay poster from disk for {ms_item_id}: {fpath}")
                                        except Exception as _fe:
                                            self.logger.warning(
                                                f"Failed to delete overlay poster from disk {fpath}: {_fe}")
                            self._mark_all_removed(ms_item_id, 'Overlay posters deleted')
                            self._increment_cleanup_stats(0)
                            try:
                                if backup_file.exists():
                                    backup_file.unlink()
                            except Exception:
                                pass
                            self._delete_season_backups(ms_item_id)
                            return {
                                'success': True,
                                'message': f'Overlay removed — {deleted} custom poster(s) deleted'
                            }
                    except Exception as _e:
                        self.logger.warning(f"Failed to delete custom posters for {ms_item_id}: {_e}")
                    return {
                        'success': False,
                        'message': f'No backup or TMDB poster found for {ms_item_id}'
                    }

            # Upload the clean poster to the media server.
            success = client.upload_poster(ms_item_id, poster_data)

            if success:
                self._mark_all_removed(ms_item_id, f'Poster restored from {source}')
                self.logger.info(f"Overlay removed for {ms_item_id} (source: {source})")
                self._increment_cleanup_stats(len(poster_data))

                # Delete the backup file — no longer needed once the overlay is removed.
                try:
                    if backup_file.exists():
                        backup_file.unlink()
                        self.logger.info(f"Deleted backup file for {ms_item_id}")
                except Exception as _be:
                    self.logger.warning(f"Could not delete backup for {ms_item_id}: {_be}")

                # Delete any season backup files for this show so a new Plex poster
                # selection isn't blocked by stale season backups.
                self._delete_season_backups(ms_item_id)

                # Plex only: delete remaining non-selected uploaded poster files from disk
                # (old overlay versions that are no longer active).
                if not _jellyfin:
                    try:
                        import glob as _glob
                        plex_data_path = get_setting('Overlay Settings', 'plex_data_path', default='').strip()
                        if plex_data_path and os.path.isdir(plex_data_path):
                            for _p in (client.get_poster_list(ms_item_id) or []):
                                if _p.get('selected'):
                                    continue
                                _rk = _p.get('ratingKey', '')
                                if not _rk.startswith('upload://posters/'):
                                    continue
                                _uhash = _rk[len('upload://posters/'):]
                                if not _uhash:
                                    continue
                                for _fp in _glob.glob(os.path.join(
                                        plex_data_path, 'Metadata', '*', '*', '*.bundle',
                                        'Uploads', 'posters', _uhash)):
                                    try:
                                        os.remove(_fp)
                                        self.logger.info(
                                            f"Deleted orphaned overlay poster for {ms_item_id}: {_fp}")
                                    except Exception as _fe:
                                        self.logger.warning(
                                            f"Failed to delete poster file {_fp}: {_fe}")
                    except Exception as _pce:
                        self.logger.warning(f"Poster file cleanup for {ms_item_id} failed: {_pce}")

                return {
                    'success': True,
                    'message': f'Poster restored successfully (source: {source})'
                }
            else:
                msg = 'Failed to upload restored poster to media server'
                self._mark_all_removal_failed(ms_item_id, msg)
                return {
                    'success': False,
                    'message': msg
                }

        except Exception as e:
            self.logger.error(f"Failed to restore poster for {ms_item_id}: {e}")
            try:
                self._mark_all_removal_failed(ms_item_id, str(e))
            except Exception:
                pass
            return {
                'success': False,
                'message': str(e)
            }

    def remove_overlay(self, media_item_id: int) -> Dict[str, Any]:
        """
        Remove overlay and restore original poster.

        Args:
            media_item_id: Database media item ID

        Returns:
            Result dictionary
        """
        try:
            # Get media item
            conn = _get_db_connection()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('''
                SELECT ms_item_id, title
                FROM media_items
                WHERE id = ?
            ''', (media_item_id,))

            item = cursor.fetchone()
            conn.close()

            if not item:
                return {
                    'success': False,
                    'message': f'Media item {media_item_id} not found'
                }

            ms_item_id = item['ms_item_id']
            if not ms_item_id:
                return {
                    'success': False,
                    'message': 'No media server item ID for this item'
                }

            # Restore poster
            result = self.restore_poster(media_item_id, ms_item_id)
            return result

        except Exception as e:
            self.logger.error(f"Failed to remove overlay for item {media_item_id}: {e}")
            return {
                'success': False,
                'message': str(e)
            }

    def batch_remove_overlays(self, media_item_ids: List[int]) -> Dict[str, Any]:
        """
        Remove overlays for multiple items.

        Args:
            media_item_ids: List of media item IDs

        Returns:
            Summary dictionary
        """
        results = {
            'total': len(media_item_ids),
            'restored': 0,
            'failed': 0,
            'items': []
        }

        for item_id in media_item_ids:
            result = self.remove_overlay(item_id)
            results['items'].append({
                'item_id': item_id,
                'success': result['success'],
                'message': result['message']
            })

            if result['success']:
                results['restored'] += 1
            else:
                results['failed'] += 1

        self.logger.info(f"Batch removal complete: {results['restored']}/{results['total']} restored")

        return results

    def cleanup_orphaned_backups(self) -> Dict[str, Any]:
        """
        Clean up orphaned backup files for items no longer in database.

        Returns:
            Cleanup results
        """
        try:
            # Get all valid keys: show/movie ms_item_ids + active season ms_item_ids
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT DISTINCT ms_item_id FROM media_items WHERE ms_item_id IS NOT NULL')
            valid_keys = set(row[0] for row in cursor.fetchall())

            # Season backup files are named season_{key}_original.jpg
            # Valid season keys come from season_overlay_state (applied or pending)
            cursor.execute('''
                SELECT DISTINCT season_ms_item_id FROM season_overlay_state
                WHERE season_ms_item_id IS NOT NULL
                  AND status NOT IN ('removed', 'user_removed')
            ''')
            valid_season_keys = set(row[0] for row in cursor.fetchall())
            conn.close()

            # Find orphaned backups — handle show/movie and season files separately
            orphaned = []
            for backup_file in self.backup_dir.glob('*_original.jpg'):
                stem = backup_file.stem.replace('_original', '')
                if stem.startswith('season_'):
                    # season_{key} → check against season_overlay_state
                    season_key = stem[len('season_'):]
                    if season_key not in valid_season_keys:
                        orphaned.append(backup_file)
                else:
                    # show/movie → check against media_items.ms_item_id
                    if stem not in valid_keys:
                        orphaned.append(backup_file)

            # Remove orphaned backups
            removed_count = 0
            for backup_file in orphaned:
                try:
                    backup_file.unlink()
                    removed_count += 1
                except Exception as e:
                    self.logger.error(f"Failed to remove {backup_file}: {e}")

            self.logger.info(f"Cleaned up {removed_count} orphaned backups")

            return {
                'success': True,
                'orphaned_found': len(orphaned),
                'orphaned_removed': removed_count
            }

        except Exception as e:
            self.logger.error(f"Failed to cleanup orphaned backups: {e}")
            return {
                'success': False,
                'message': str(e)
            }

    def _increment_cleanup_stats(self, poster_bytes: int = 0) -> None:
        """Increment the all-time cleanup counters stored in overlay_sync_state."""
        _MAX_RETRIES = 5
        for attempt in range(_MAX_RETRIES):
            try:
                conn = _get_db_connection()
                conn.execute('''
                    INSERT INTO overlay_sync_state (key, value, updated_at)
                    VALUES ('cleanup_total_posters', '1', CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST(CAST(value AS INTEGER) + 1 AS TEXT),
                        updated_at = CURRENT_TIMESTAMP
                ''')
                conn.execute('''
                    INSERT INTO overlay_sync_state (key, value, updated_at)
                    VALUES ('cleanup_total_bytes', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = CAST(CAST(value AS INTEGER) + ? AS TEXT),
                        updated_at = CURRENT_TIMESTAMP
                ''', (str(poster_bytes), poster_bytes))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as _e:
                if 'database is locked' in str(_e) and attempt < _MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self.logger.warning(f"Failed to update cleanup stats: {_e}")
                return
            except Exception as _e:
                self.logger.warning(f"Failed to update cleanup stats: {_e}")
                return

    def get_backup_stats(self) -> Dict[str, Any]:
        """
        Get statistics about poster backups.

        Returns:
            Backup statistics
        """
        try:
            backup_files = list(self.backup_dir.glob('*_original.jpg'))
            total_size = sum(f.stat().st_size for f in backup_files)

            return {
                'success': True,
                'backup_count': len(backup_files),
                'total_size': total_size,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'backup_dir': str(self.backup_dir)
            }

        except Exception as e:
            self.logger.error(f"Failed to get backup stats: {e}")
            return {
                'success': False,
                'message': str(e)
            }

    def _delete_season_backups(self, ms_item_id: str) -> None:
        """Delete all season backup files associated with a show's ms_item_id."""
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT DISTINCT season_ms_item_id FROM season_overlay_state WHERE show_ms_item_id = ?',
                (ms_item_id,)
            )
            season_keys = [row[0] for row in cursor.fetchall() if row[0]]
            conn.close()
            for sk in season_keys:
                season_file = self.backup_dir / f"season_{sk}_original.jpg"
                if season_file.exists():
                    try:
                        season_file.unlink()
                        self.logger.info(f"Deleted season backup for {sk}")
                    except Exception as _e:
                        self.logger.warning(f"Could not delete season backup {season_file}: {_e}")
        except Exception as _e:
            self.logger.warning(f"_delete_season_backups failed for {ms_item_id}: {_e}")

    def _mark_all_removed(self, ms_item_id: str, reason: str):
        """
        Mark every media_overlay_state row that shares ms_item_id as 'removed'.

        This is critical for TV shows (many episodes, one poster) and multi-version
        movies — without it, sibling rows stay 'applied' and the next quality-change
        scan or manual re-apply would treat the show as still having an overlay.
        """
        _MAX_RETRIES = 5
        for attempt in range(_MAX_RETRIES):
            try:
                conn = _get_db_connection()
                conn.execute('''
                    UPDATE media_overlay_state
                    SET status = 'removed',
                        reason = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE media_item_id IN (
                        SELECT id FROM media_items WHERE ms_item_id = ?
                    )
                ''', (reason, ms_item_id))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if 'database is locked' in str(e) and attempt < _MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self.logger.error(f"Failed to mark all removed for {ms_item_id}: {e}")
                return
            except Exception as e:
                self.logger.error(f"Failed to mark all removed for {ms_item_id}: {e}")
                return

    def _mark_all_removal_failed(self, ms_item_id: str, reason: str):
        """Mark every media_overlay_state row for ms_item_id as 'removal_failed'."""
        _MAX_RETRIES = 5
        for attempt in range(_MAX_RETRIES):
            try:
                conn = _get_db_connection()
                conn.execute('''
                    UPDATE media_overlay_state
                    SET status = 'removal_failed',
                        reason = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE media_item_id IN (
                        SELECT id FROM media_items WHERE ms_item_id = ?
                    )
                ''', (reason, ms_item_id))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                if 'database is locked' in str(e) and attempt < _MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                self.logger.error(f"Failed to mark removal_failed for {ms_item_id}: {e}")
                return
            except Exception as e:
                self.logger.error(f"Failed to mark removal_failed for {ms_item_id}: {e}")
                return

    def delete_all_backups(self) -> Dict[str, Any]:
        """
        Delete every backup file and reset all overlay states to pending.

        Used when the user wants a completely clean slate — next sync will
        re-download fresh posters from Plex and re-apply all overlays.

        Returns:
            Result dict with deleted count.
        """
        try:
            backup_files = list(self.backup_dir.glob('*_original.jpg'))
            deleted = 0
            for f in backup_files:
                try:
                    f.unlink()
                    deleted += 1
                except Exception as e:
                    self.logger.error(f"Failed to delete backup {f}: {e}")

            # Reset all overlay states to pending so the next sync re-applies everything
            conn = _get_db_connection()
            conn.execute('''
                UPDATE media_overlay_state
                SET status = 'pending', reason = 'backups cleared', updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('applied', 'skipped', 'removed')
            ''')
            conn.execute('''
                UPDATE season_overlay_state
                SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE status IN ('applied', 'skipped', 'removed')
            ''')
            conn.commit()
            conn.close()

            self.logger.info(f"Deleted all {deleted} backup files and reset overlay states to pending")
            return {'success': True, 'deleted': deleted}

        except Exception as e:
            self.logger.error(f"Failed to delete all backups: {e}")
            return {'success': False, 'message': str(e)}


def run_remove_all_job(item_ids: list, season_rows: list, overlay_manager) -> None:
    """
    Background worker for 'Remove All Overlays'.

    Runs in a daemon thread; progress is exposed via get_remove_all_status().
    item_ids: list of media_item_id integers (one per unique ms_item_id)
    season_rows: list of sqlite3.Row with show_ms_item_id / season_ms_item_id / season_number
    overlay_manager: OverlayManager instance
    """
    total = len(item_ids) + len(season_rows)
    _update_remove_all(
        running=True, total=total, done=0, failed=0, restored=0,
        current='', errors=[], started_at=datetime.utcnow().isoformat(), finished_at=None,
    )

    cache_manager = PosterCacheManager(None)
    done = 0
    restored = 0
    failed = 0
    errors = []

    # --- movie / show posters ------------------------------------------------
    for item_id in item_ids:
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT ms_item_id, title FROM media_items WHERE id = ?', (item_id,)
            )
            row = cursor.fetchone()
            conn.close()
            label = (row['title'] or row['ms_item_id']) if row else str(item_id)
            _update_remove_all(current=label)
            result = cache_manager.remove_overlay(item_id)
            if result.get('success'):
                restored += 1
            else:
                failed += 1
                errors.append(f"{label}: {result.get('message', 'unknown error')}")
                if len(errors) > 50:
                    errors = errors[-50:]
        except Exception as exc:
            failed += 1
            errors.append(f"item {item_id}: {exc}")
            if len(errors) > 50:
                errors = errors[-50:]
        done += 1
        _update_remove_all(done=done, restored=restored, failed=failed, errors=list(errors))

    # --- season posters -------------------------------------------------------
    for sr in season_rows:
        try:
            _update_remove_all(current=f"Season {sr['season_number']} ({sr['season_ms_item_id']})")
            sr_result = overlay_manager.remove_season_overlay(
                show_plex_rating_key=sr['show_ms_item_id'],
                season_plex_rating_key=sr['season_ms_item_id'],
                season_number=sr['season_number'],
            )
            if sr_result.get('success'):
                restored += 1
            else:
                msg = sr_result.get('message', 'unknown error')
                failed += 1
                errors.append(f"season {sr['season_ms_item_id']}: {msg}")
                if len(errors) > 50:
                    errors = errors[-50:]
        except Exception as exc:
            failed += 1
            errors.append(f"season {sr['season_ms_item_id']}: {exc}")
            if len(errors) > 50:
                errors = errors[-50:]
        done += 1
        _update_remove_all(done=done, restored=restored, failed=failed, errors=list(errors))

    _update_remove_all(
        running=False, current='', done=done, restored=restored,
        failed=failed, errors=list(errors),
        finished_at=datetime.utcnow().isoformat(),
    )
    logger.info(f"Remove-all job finished: {restored} restored, {failed} failed")
    try:
        from overlays.activity_logger import log_activity
        log_activity('remove_all',
                     title=f"Remove all overlays: {restored} restored, {failed} failed",
                     stats={'restored': restored, 'failed': failed,
                            'failures': errors[:20]})
    except Exception as _la:
        logger.warning(f"Failed to log remove-all activity: {_la}")


# Convenience functions for scheduled tasks
def task_cleanup_orphaned_backups():
    """Scheduled task to cleanup orphaned backup files."""
    logger.info("Starting orphaned backup cleanup task")

    try:
        manager = PosterCacheManager(None)

        result = manager.cleanup_orphaned_backups()
        logger.info(f"Cleanup complete: {result.get('orphaned_removed', 0)} orphaned backups removed")

        return result

    except Exception as e:
        logger.error(f"Orphaned backup cleanup task failed: {e}")
        return {
            'success': False,
            'message': str(e)
        }
