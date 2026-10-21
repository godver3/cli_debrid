"""
Overlay Manager

Orchestrates the end-to-end overlay generation process.
"""

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

# ── Generate job state ────────────────────────────────────────────────────────
_gen_lock = threading.Lock()
_gen_job: Dict[str, Any] = {
    'running':     False,
    'total':       0,
    'done':        0,
    'applied':     0,
    'failed':      0,
    'skipped':     0,
    'current':     '',
    'errors':      [],
    'started_at':  None,
    'finished_at': None,
    'label':       '',  # human description e.g. "Generate all movies"
}


def get_generate_status() -> Dict[str, Any]:
    with _gen_lock:
        return dict(_gen_job)


def _update_gen(**kwargs):
    with _gen_lock:
        _gen_job.update(kwargs)


def _get_db_connection():
    """Get a database connection using the production DB access pattern."""
    from database.core import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn

from .layout_manager import LayoutManager
from .media_info import MediaInfoExtractor
from .plex_client import PlexClient
from .renderer import OverlayRenderer
from .utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token


class OverlayManager:
    """
    Manages the complete overlay generation workflow.

    Steps:
    1. Fetch media item from database
    2. Get media server item ID (Plex ratingKey or Jellyfin ItemId)
    3. Fetch media metadata from the media server
    4. Extract media info (resolution, HDR, audio, etc.)
    5. Download original poster
    6. Render overlay on poster (using template or POC renderer)
    7. Upload overlay poster to media server
    8. Update database state
    """

    def __init__(self, db_path: Optional[str], plex_base_url: str, plex_token: str,
                 asset_dir: str = "/user/config/overlay_assets",
                 template_dir: str = "/user/config/overlay_templates"):
        """
        Initialize overlay manager.

        Args:
            db_path: Unused — kept for API compatibility. DB access uses get_db_connection().
            plex_base_url: Plex server URL (ignored in Jellyfin mode)
            plex_token: Plex authentication token (ignored in Jellyfin mode)
            asset_dir: Path to overlay assets directory
            template_dir: Path to template storage directory
        """
        if is_jellyfin_mode():
            from .jellyfin_client import JellyfinClient
            self.client = JellyfinClient(get_jellyfin_url(), get_jellyfin_token())
            self.plex = None
            self._jellyfin_mode = True
        else:
            self.plex = PlexClient(plex_base_url, plex_token)
            self.client = self.plex
            self._jellyfin_mode = False
        self.extractor = MediaInfoExtractor()
        self.renderer = OverlayRenderer(asset_dir)
        self.layout_mgr = LayoutManager(None)
        self.logger = logging.getLogger(__name__)

    def generate_overlay_for_item(self, media_item_id: int,
                                  force: bool = False,
                                  layout_id: Optional[int] = None,
                                  force_fresh_poster: bool = False) -> Dict[str, Any]:
        """
        Generate overlay for a single media item.

        Args:
            media_item_id: Database ID of media item
            force: Force regeneration even if already applied
            layout_id: Optional template ID to use (if None, uses POC renderer or default template)

        Returns:
            Result dictionary with status and details
        """
        result = {
            'success': False,
            'item_id': media_item_id,
            'status': None,
            'message': None,
            'details': {}
        }

        try:
            # Get media item from database
            item = self._get_media_item(media_item_id)
            if not item:
                result['status'] = 'error'
                result['message'] = f'Media item {media_item_id} not found in database'
                return result

            result['details']['title'] = item.get('title', 'Unknown')
            result['details']['year'] = item.get('year')

            # Get media server item ID
            plex_rating_key = item.get('ms_item_id')
            if not plex_rating_key:
                result['status'] = 'error'
                result['message'] = 'No media server item ID found for item'
                return result

            result['details']['ms_item_id'] = plex_rating_key

            # Resolve the template early so we can include its hash in the skip check.
            _early_template = None
            if layout_id:
                _early_template = self.layout_mgr.get_layout(layout_id)
            else:
                db_type = item.get('type', 'movie')
                media_type = 'tv' if db_type == 'episode' else db_type
                _early_layouts = self.layout_mgr.list_layouts(media_type=media_type, active_only=True)
                if _early_layouts:
                    _early_template = _early_layouts[0]

            # Pre-fetch best-quality data once so the skip check and later
            # _build_media_info_from_db both reuse it without a second DB query.
            # Movies: scope to ms_item_id so split Plex items (unique ratingKey each)
            # only aggregate quality across their own DB rows, not all versions of the
            # same movie. This ensures each split item gets its own correct overlay.
            _item_type = item.get('type')
            _imdb_id   = item.get('imdb_id') or ''
            _ms_id     = item.get('ms_item_id') or ''
            _best_data: Optional[Dict[str, Any]] = None
            if _item_type == 'episode' and _imdb_id:
                _best_data = self._best_version(_imdb_id, 'episode')
            elif _item_type == 'movie':
                if _ms_id:
                    _best_data = self._best_version_by_ms_id(_ms_id, 'movie')
                elif _imdb_id:
                    _best_data = self._best_version(_imdb_id, 'movie')

            # Check if already processed (unless force=True).
            # Re-apply if: quality improved OR layout was updated since last apply.
            if not force:
                overlay_state = self._get_overlay_state(media_item_id)
                if overlay_state and overlay_state.get('status') == 'applied':
                    # Check layout hash first — if layout changed, always re-apply
                    if _early_template:
                        current_layout_hash = self._compute_layout_hash(_early_template)
                        stored_layout_hash = overlay_state.get('last_layout_hash')
                        if stored_layout_hash and stored_layout_hash != current_layout_hash:
                            self.logger.info(
                                f"Layout updated for {item.get('title')} "
                                f"(layout hash changed) — re-applying overlay")
                            # Fall through to re-apply
                        else:
                            # Layout unchanged — check quality hash (reuse _best_data)
                            stored_hash = overlay_state.get('last_metadata_hash')
                            if stored_hash:
                                current_hash = self._quality_hash_for_item(item, _best_data)
                                if current_hash and current_hash != stored_hash:
                                    self.logger.info(
                                        f"Quality changed for {item.get('title')} "
                                        f"(hash {stored_hash[:8]}→{current_hash[:8]}) "
                                        f"— re-applying overlay")
                                    # Fall through to re-apply
                                else:
                                    result['status'] = 'skipped'
                                    result['message'] = 'Overlay already applied, quality unchanged'
                                    result['success'] = True
                                    return result
                            else:
                                # Legacy overlay with no stored quality hash — skip now,
                                # the hash will be stored the next time it is regenerated.
                                result['status'] = 'skipped'
                                result['message'] = 'Overlay already applied (use force=True to regenerate)'
                                result['success'] = True
                                return result
                    else:
                        # No active layout — check quality hash only (reuse _best_data)
                        stored_hash = overlay_state.get('last_metadata_hash')
                        if stored_hash:
                            current_hash = self._quality_hash_for_item(item, _best_data)
                            if current_hash and current_hash != stored_hash:
                                self.logger.info(
                                    f"Quality changed for {item.get('title')} "
                                    f"(hash {stored_hash[:8]}→{current_hash[:8]}) "
                                    f"— re-applying overlay")
                            else:
                                result['status'] = 'skipped'
                                result['message'] = 'Overlay already applied, quality unchanged'
                                result['success'] = True
                                return result
                        else:
                            result['status'] = 'skipped'
                            result['message'] = 'Overlay already applied (use force=True to regenerate)'
                            result['success'] = True
                            return result

            # Try to get media info from media server, fall back to DB data.
            # Re-raises HTTPError 404 (Plex: item moved) or 400 (Jellyfin: invalid/legacy ID)
            # so we can detect stale keys.
            # Pass _best_data so it is not re-fetched inside _get_media_info.
            try:
                media_info = self._get_media_info(media_item_id, item, plex_rating_key, _best_data)
            except Exception as _meta_exc:
                import requests as _req
                _status_code = getattr(getattr(_meta_exc, 'response', None), 'status_code', None)
                _is_stale = (
                    isinstance(_meta_exc, _req.exceptions.HTTPError) and (
                        _status_code == 404 or
                        (_status_code == 400 and self._jellyfin_mode)
                    )
                )
                if _is_stale:
                    self._reset_stale_ms_key(media_item_id, plex_rating_key)
                    result['status'] = 'pending'
                    result['message'] = f'Stale ms_item_id {plex_rating_key} reset — will re-sync'
                    return result
                raise

            result['details']['media_info'] = media_info

            # Enrich media_info with title, year, TMDB clearlogo URL, and textless flag.
            # textless_poster_used is set after poster download below.
            media_info['title'] = item.get('title', '')
            media_info['year']  = item.get('year')
            media_info['clearlogo_url'] = self._fetch_clearlogo_url(
                item.get('tmdb_id'), item.get('type', 'movie'))
            # Default False — updated after poster_image is resolved below
            media_info['textless_poster_used'] = False

            # Update media_items table with extracted info
            self._update_media_item_info(media_item_id, media_info, plex_rating_key)

            # Always render onto the clean original, never the already-overlaid Plex poster.
            # Strategy:
            #   • force_fresh_poster=True → delete backup and re-download from Plex (user's
            #     current selection); used by the "Regenerate with Plex poster" action.
            #   • Backup exists & overlay currently applied (status='applied') → use backup
            #     (our overlay is the active Plex poster; downloading would give the overlaid version)
            #   • Backup exists & overlay NOT applied → check if user picked a different Plex poster
            #     since last backup; if so, update backup with that selection.
            #   • No backup → download from TMDB first (clean, unmodified), save as backup.
            poster_image = None  # ensure always defined before the try block
            try:
                from overlays.cache_cleanup import PosterCacheManager
                from PIL import Image as _PILImage
                from io import BytesIO as _BytesIO
                backup_mgr = PosterCacheManager(None)
                ms_item_id = plex_rating_key
                backup_file = backup_mgr.backup_dir / f"{ms_item_id}_original.jpg"

                # force_fresh_poster: discard old backup and cached TMDB poster URL so we
                # re-fetch a fresh English poster from TMDB on the next download attempt.
                if force_fresh_poster:
                    if backup_file.exists():
                        backup_file.unlink()
                        self.logger.info(
                            f"Deleted backup for {ms_item_id} to force fresh poster fetch")
                    _meta_file_fresh = backup_file.with_suffix('.meta')
                    if _meta_file_fresh.exists():
                        _meta_file_fresh.unlink()
                    # Clear any stale TMDB poster URL from the in-memory/disk cache so the
                    # re-fetch honours the English language filter.
                    try:
                        from routes.poster_cache import _cache, _cache_lock
                        media_type_key = 'movie' if item.get('type') == 'movie' else 'tv'
                        with _cache_lock:
                            for _k in (
                                f"{item.get('imdb_id')}_{media_type_key}",
                                f"{item.get('tmdb_id')}_{media_type_key}",
                            ):
                                _cache.pop(_k, None)
                        self.logger.info(
                            f"Cleared TMDB poster cache entries for {ms_item_id}")
                    except Exception as _ce:
                        self.logger.debug(f"Could not clear poster cache: {_ce}")

                if backup_file.exists():
                    existing_state = self._get_overlay_state(media_item_id)
                    current_status = existing_state.get('status') if existing_state else None
                    poster_image = None

                    if current_status in ('removed', 'user_removed'):
                        # Overlay was explicitly removed — user may have picked a new poster.
                        # Check whether the media server poster differs from our backup.
                        try:
                            plex_poster = self.client.download_poster(plex_rating_key)
                            if plex_poster and not self._is_blank_image(plex_poster):
                                with open(str(backup_file), 'rb') as _f:
                                    backup_img = _PILImage.open(_BytesIO(_f.read()))
                                    backup_img.load()
                                if (self._calculate_image_hash(plex_poster) !=
                                        self._calculate_image_hash(backup_img)):
                                    # User selected a different poster — update backup and use it
                                    self.logger.info(
                                        f"Media server poster differs from backup for "
                                        f"{plex_rating_key}; updating backup with user selection")
                                    backup_mgr.backup_poster(
                                        plex_rating_key,
                                        self.renderer.image_to_bytes(plex_poster))
                                    poster_image = plex_poster
                                    # User-selected Plex poster — never textless
                                    try:
                                        import json as _json
                                        _user_meta = backup_file.with_suffix('.meta')
                                        with open(str(_user_meta), 'w') as _mf:
                                            _json.dump({'textless': False}, _mf)
                                    except Exception:
                                        pass
                        except Exception as _chk_err:
                            self.logger.warning(
                                f"Could not check Plex poster change for {plex_rating_key}: "
                                f"{_chk_err}")

                    if poster_image is None:
                        with open(str(backup_file), 'rb') as _f:
                            poster_image = _PILImage.open(_BytesIO(_f.read()))
                            poster_image.load()
                        self.logger.info(f"Using original backup poster for {plex_rating_key}")
                        # Restore the textless flag from the companion .meta file saved
                        # alongside the backup when it was first downloaded.
                        # Falls back to the DB value for backups created before .meta existed.
                        _meta_file = backup_file.with_suffix('.meta')
                        try:
                            import json as _json
                            with open(str(_meta_file), 'r') as _mf:
                                _meta = _json.load(_mf)
                            poster_image._cli_textless = bool(_meta.get('textless', False))
                        except FileNotFoundError:
                            # No .meta file — legacy backup. Check the DB for the last known value.
                            try:
                                _db_conn = _get_db_connection()
                                _db_row = _db_conn.execute(
                                    'SELECT textless_poster_used FROM media_overlay_state WHERE media_item_id = ?',
                                    (media_item_id,)
                                ).fetchone()
                                _db_conn.close()
                                poster_image._cli_textless = bool(_db_row[0]) if (_db_row and _db_row[0]) else False
                                # Write the .meta file now so future loads don't need the DB
                                try:
                                    with open(str(_meta_file), 'w') as _mf:
                                        _json.dump({'textless': poster_image._cli_textless}, _mf)
                                except Exception:
                                    pass
                            except Exception:
                                poster_image._cli_textless = False
                        except Exception:
                            poster_image._cli_textless = False
                else:
                    # No backup — fetch a clean original from TMDB.
                    # Always prefer TMDB: if the overlay was previously applied, Plex's current
                    # "selected" poster is the overlaid version we uploaded, so downloading from
                    # Plex would give us overlay-on-overlay. TMDB always has the unmodified poster.
                    # Fall back to Plex only if TMDB has nothing cached.
                    self.logger.info(
                        f"Downloading clean original poster for {plex_rating_key}")
                    poster_image = self._download_poster(plex_rating_key, item,
                                                         prefer_tmdb=True)
                    if not poster_image:
                        result['status'] = 'error'
                        result['message'] = 'Failed to download poster from TMDB or Plex'
                        self._update_overlay_state(media_item_id, 'failed',
                                                   'Poster download failed')
                        return result
                    original_bytes = self.renderer.image_to_bytes(poster_image)
                    backup_mgr.backup_poster(ms_item_id, original_bytes)
                    self.logger.info(f"Saved original poster backup for {ms_item_id}")
                    # Write companion .meta file so the textless flag survives restarts.
                    try:
                        import json as _json
                        _meta_path = backup_mgr.backup_dir / f"{ms_item_id}_original.meta"
                        with open(str(_meta_path), 'w') as _mf:
                            _json.dump({'textless': bool(getattr(poster_image, '_cli_textless', False))}, _mf)
                    except Exception as _me:
                        self.logger.debug(f"Could not write poster meta for {ms_item_id}: {_me}")
            except Exception as e:
                self.logger.warning(f"Failed to manage poster backup for {plex_rating_key}: {e}")
                # Fall back to downloading normally if backup management fails
                if not poster_image:
                    poster_image = self._download_poster(plex_rating_key, item)
                if not poster_image:
                    result['status'] = 'error'
                    result['message'] = 'Failed to download poster'
                    self._update_overlay_state(media_item_id, 'failed', 'Poster download failed')
                    return result

            # Read textless flag that _tmdb_image() attached to the PIL Image object
            media_info['textless_poster_used'] = getattr(poster_image, '_cli_textless', False)
            self.logger.debug(
                f"Poster textless={media_info['textless_poster_used']} "
                f"for {item.get('title', plex_rating_key)}")

            # Calculate poster hash (for change detection)
            poster_hash = self._calculate_image_hash(poster_image)
            result['details']['poster_hash'] = poster_hash

            # Get template if specified or find default active template
            template = None
            if layout_id:
                template = self.layout_mgr.get_layout(layout_id)
                if not template:
                    result['status'] = 'error'
                    result['message'] = f'Layout {layout_id} not found'
                    return result
            else:
                # Try to find active layout matching media type.
                # DB stores 'episode' for TV; layouts use 'tv'. Map accordingly.
                db_type = item.get('type', 'movie')
                media_type = 'tv' if db_type == 'episode' else db_type
                layouts = self.layout_mgr.list_layouts(
                    media_type=media_type,
                    active_only=True
                )
                if layouts:
                    template = layouts[0]  # Use first active layout
                    self.logger.info(f"Using default layout: {template['name']}")
                else:
                    # No active layout configured for this media type — skip.
                    self.logger.debug(
                        f"No active '{media_type}' layout found, skipping overlay for {plex_rating_key}"
                    )
                    result['status'] = 'skipped'
                    result['message'] = f"No active '{media_type}' layout configured"
                    return result

            # Normalize poster to 2:3 aspect ratio before rendering to prevent
            # badge zoom/crop issues on non-standard aspect ratio sources.
            pw, ph = poster_image.size
            expected_h = int(pw * 1.5)
            if abs(ph - expected_h) > 4:  # >4px tolerance
                self.logger.debug(
                    f"Normalizing poster from {pw}x{ph} to {pw}x{expected_h} (2:3) for {plex_rating_key}"
                )
                from PIL import Image as _PILImage
                poster_image = poster_image.resize((pw, expected_h), _PILImage.LANCZOS)

            # Render overlay
            self.logger.info(f"Rendering overlay for {plex_rating_key}")
            if template:
                # Use template-based rendering
                overlay_image = self.renderer.render_from_template(
                    poster_image,
                    template['layout_json'],
                    media_info
                )
                result['details']['template_name'] = template['name']
            else:
                # Explicit layout_id was provided but not found — already returned error above.
                # This branch should not be reached.
                self.logger.warning("No template available after layout resolution — skipping")
                result['status'] = 'skipped'
                result['message'] = 'No template available'
                return result

            # Convert to bytes for upload
            overlay_bytes = self.renderer.image_to_bytes(overlay_image)

            # Upload overlay poster to media server
            self.logger.info(f"Uploading overlay poster for {plex_rating_key}")
            try:
                upload_success = self.client.upload_poster(plex_rating_key, overlay_bytes)
            except Exception as _up_exc:
                import requests as _req
                _status_code = getattr(getattr(_up_exc, 'response', None), 'status_code', None)
                _is_stale = (
                    isinstance(_up_exc, _req.exceptions.HTTPError) and (
                        _status_code == 404 or
                        (_status_code == 400 and self._jellyfin_mode)
                    )
                )
                if _is_stale:
                    self._reset_stale_ms_key(media_item_id, plex_rating_key)
                    result['status'] = 'pending'
                    result['message'] = f'Stale ms_item_id {plex_rating_key} reset — will re-sync'
                    return result
                raise

            if not upload_success:
                result['status'] = 'error'
                result['message'] = 'Failed to upload overlay poster to media server'
                self._update_overlay_state(media_item_id, 'failed',
                                          'Poster upload failed')
                return result

            # Invalidate the thumbnail cache so the overlay page shows the new poster
            try:
                from routes.overlay_routes import invalidate_poster_thumb_cache
                invalidate_poster_thumb_cache(plex_rating_key)
            except Exception:
                pass

            # Store quality hash so future runs can detect upgrades (reuse _best_data)
            quality_hash = self._quality_hash_for_item(item, _best_data) or ''

            # Store layout hash so re-renders are triggered when layout is updated
            layout_hash = self._compute_layout_hash(template) if template else None

            # Store content hash (ratings, status, version_count) so the content-
            # change checker can detect when badges need refreshing without re-rendering.
            content_hash = self._compute_content_hash(
                imdb_rating=media_info.get('imdb_rating'),
                tmdb_rating=media_info.get('tmdb_rating'),
                trakt_rating=media_info.get('trakt_rating'),
                rt_critics_score=media_info.get('rt_critics_score'),
                rt_user_score=media_info.get('rt_user_score'),
                status=media_info.get('status'),
                version_count=media_info.get('version_count'),
            )

            # Compute SHA1 of the uploaded poster so the cleanup task can identify
            # which file in Plex bundle storage is the current active overlay without
            # making a per-item API call.
            import hashlib as _hl
            _upload_sha1 = _hl.sha1(overlay_bytes).hexdigest()

            # Fetch the new thumb URL from Plex so we can detect future deselections.
            # Retry a few times since Plex may take a moment to index the new upload.
            _plex_thumb_url = None
            try:
                import time as _t
                for _ti in range(3):
                    _plex_thumb_url = self.client.get_thumb_url(plex_rating_key)
                    if _plex_thumb_url:
                        break
                    _t.sleep(1)
            except Exception:
                pass

            # Update overlay state
            self._update_overlay_state(
                media_item_id,
                status='applied',
                reason='Overlay successfully applied',
                poster_hash=poster_hash,
                metadata_hash=quality_hash,
                layout_hash=layout_hash,
                content_hash=content_hash,
                plex_upload_hash=_upload_sha1,
                textless_poster_used=media_info.get('textless_poster_used', False),
                plex_thumb_url=_plex_thumb_url,
            )

            result['success'] = True
            result['status'] = 'applied'
            result['message'] = 'Overlay successfully generated and applied'
            result['content_hash'] = content_hash  # propagated to all episodes by caller
            self.logger.info(f"Successfully applied overlay for {item.get('title', media_item_id)}")

            # For episodes: mark ALL sibling episodes sharing the same show key as
            # applied so that regenerate-all and batch operations don't leave
            # thousands of pending rows behind (only the MIN(id) representative
            # was processed; siblings share the same show poster).
            if item.get('type') == 'episode' and plex_rating_key:
                try:
                    _sib_conn = _get_db_connection()
                    # Update existing pending/failed/removed/analyzing rows
                    _sib_conn.execute(
                        """UPDATE media_overlay_state
                              SET status = 'applied',
                                  last_content_hash = COALESCE(?, last_content_hash),
                                  overlay_applied_at = CURRENT_TIMESTAMP,
                                  updated_at = CURRENT_TIMESTAMP
                            WHERE media_item_id IN (
                                SELECT id FROM media_items
                                WHERE ms_item_id = ? AND type = 'episode'
                            )
                              AND status IN ('pending', 'analyzing', 'removed', 'failed')""",
                        (content_hash, plex_rating_key)
                    )
                    # Insert applied rows for episodes with no state record yet
                    _sib_conn.execute(
                        """INSERT OR IGNORE INTO media_overlay_state
                               (media_item_id, status, reason, last_content_hash,
                                overlay_applied_at, created_at, updated_at)
                           SELECT id, 'applied', 'Marked applied via sibling', ?,
                                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                             FROM media_items
                            WHERE ms_item_id = ? AND type = 'episode'""",
                        (content_hash, plex_rating_key)
                    )
                    _sib_conn.commit()
                    _sib_conn.close()
                except Exception as _sib_err:
                    self.logger.debug(f"Sibling episode mark-applied failed: {_sib_err}")

        except Exception as e:
            self.logger.error(f"Failed to generate overlay for item {media_item_id}: {e}",
                            exc_info=True)
            result['status'] = 'error'
            result['message'] = str(e)
            self._update_overlay_state(media_item_id, 'failed', str(e))

        return result

    def _reset_stale_ms_key(self, media_item_id: int, ms_item_id: str):
        """
        Clear a stale ms_item_id and reset overlay state to pending.

        Called when the media server returns 404 for an item ID, meaning the item was
        re-indexed and has a new ID. The next sync will re-discover
        the correct ID via _sync_library_keys_for_new_items.

        All sibling episodes sharing this same stale ms_item_id (the show-level rating
        key returned by get_show_best_episode_media) are cleared in the same pass —
        otherwise only the one representative row picked by that cycle's dedup query
        gets reset, and the rest keep 404ing once per hour until their turn comes up.
        """
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM media_items WHERE ms_item_id = ?",
                (ms_item_id,)
            )
            sibling_ids = [row[0] for row in cursor.fetchall()]
            if media_item_id not in sibling_ids:
                sibling_ids.append(media_item_id)

            cursor.execute(
                "UPDATE media_items SET ms_item_id = NULL WHERE ms_item_id = ?",
                (ms_item_id,)
            )
            cursor.executemany('''
                INSERT INTO media_overlay_state (media_item_id, status, reason, updated_at, created_at)
                VALUES (?, 'pending', 'Stale ms_item_id reset — will re-sync on next run', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(media_item_id) DO UPDATE SET
                    status = 'pending',
                    reason = 'Stale ms_item_id reset — will re-sync on next run',
                    updated_at = CURRENT_TIMESTAMP
            ''', [(sid,) for sid in sibling_ids])
            conn.commit()
            conn.close()
            self.logger.warning(
                f"Reset stale ms_item_id {ms_item_id} for item {media_item_id} and "
                f"{len(sibling_ids) - 1} sibling episode(s) "
                f"(media server returned 404 — key(s) will be re-synced on next overlay run)"
            )
        except Exception as e:
            self.logger.error(f"Failed to reset stale ms_item_id for item {media_item_id}: {e}")

    def _get_media_item(self, media_item_id: int) -> Optional[Dict[str, Any]]:
        """Get media item from database."""
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, title, year, imdb_id, tmdb_id, type,
                       ms_item_id, ms_resolution, ms_hdr,
                       ms_dolby_vision, ms_audio_codec,
                       ms_audio_channels, ms_video_codec,
                       resolution, filled_by_file, location_on_disk,
                       location_basename, ms_hdr_format
                FROM media_items
                WHERE id = ?
            ''', (media_item_id,))

            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None

        except Exception as e:
            self.logger.error(f"Database error fetching media item: {e}")
            return None

    # ── Shared quality-ranking SQL fragments (single authoritative copy) ────────
    _RES_RANK = """
        CASE lower(ms_resolution)
            WHEN '2160p' THEN 6  WHEN '4k'    THEN 6  WHEN '4K' THEN 6
            WHEN '1440p' THEN 5
            WHEN '1080p' THEN 4  WHEN '1080P' THEN 4
            WHEN '720p'  THEN 3  WHEN '720P'  THEN 3
            WHEN '576p'  THEN 2  WHEN '480p'  THEN 1
            ELSE 0
        END
    """
    _HDR_RANK = """
        CASE
            WHEN ms_dolby_vision = 1 AND ms_hdr = 1 THEN 4
            WHEN ms_dolby_vision = 1                 THEN 3
            WHEN ms_hdr = 1                          THEN 2
            ELSE 1
        END
    """
    _AUD_RANK = """
        CASE lower(ms_audio_codec)
            WHEN 'truehd atmos'        THEN 10
            WHEN 'truehd+atmos'        THEN 10
            WHEN 'dts:x'               THEN 9
            WHEN 'truehd'              THEN 8
            WHEN 'dts-hd ma'           THEN 7
            WHEN 'dts-hd master audio' THEN 7
            WHEN 'dts-hd hra'          THEN 6
            WHEN 'dts-hd'              THEN 6
            WHEN 'eac3 atmos'          THEN 5
            WHEN 'dd+ atmos'           THEN 5
            WHEN 'eac3'                THEN 4
            WHEN 'dd+'                 THEN 4
            WHEN 'flac'                THEN 4
            WHEN 'dd atmos'            THEN 3
            WHEN 'ac3'                 THEN 3
            WHEN 'dolby digital'       THEN 3
            WHEN 'dd'                  THEN 3
            WHEN 'dts'                 THEN 3
            WHEN 'aac'                 THEN 2
            ELSE 1
        END
    """
    # Fields that can be filled from a lower-ranked row when missing on the best one.
    # Resolution/HDR/DV are intentionally NOT here — they define which row is "best".
    _FILLABLE = (
        'ms_audio_codec', 'ms_audio_channels', 'ms_video_codec',
        'ms_media_container', 'ms_media_bitrate',
        'filled_by_file', 'location_on_disk', 'location_basename',
    )

    def _best_version(self, imdb_id: str, item_type: str) -> Optional[Dict[str, Any]]:
        """
        Return the highest-quality row's media columns for a show or movie,
        with field-level fallback across all versions/episodes.

        Fetches all collected rows (matched by imdb_id + type) ordered by quality
        rank.  The #1 row (best resolution/HDR/audio) is used as the base.
        Any fillable field that is NULL or empty is filled in from the next
        ranked row that has it.

        item_type must be 'episode' or 'movie'.

        This prevents a badge showing blank audio/format just because the
        single best-resolution file happened to be missing those columns.
        """
        if not imdb_id or item_type not in ('episode', 'movie'):
            return None
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT ms_resolution, ms_hdr, ms_dolby_vision, ms_hdr_format,
                       ms_audio_codec, ms_audio_channels,
                       ms_video_codec, ms_media_container, ms_media_bitrate,
                       filled_by_file, location_on_disk, location_basename
                FROM media_items
                WHERE imdb_id = ?
                  AND type = ?
                  AND state IN ('Collected', 'Upgrading')
                  AND ms_resolution IS NOT NULL
                ORDER BY
                    ({self._RES_RANK}) DESC,
                    ({self._HDR_RANK}) DESC,
                    ({self._AUD_RANK}) DESC,
                    CAST(COALESCE(ms_audio_channels, '0') AS REAL) DESC
            ''', (imdb_id, item_type))
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return None

            # Start with the best row as the base result
            merged = dict(rows[0])

            # Fill any missing fillable fields from subsequent rows
            missing = [f for f in self._FILLABLE if not merged.get(f)]
            if missing:
                for row in rows[1:]:
                    for field in list(missing):
                        if row[field]:
                            merged[field] = row[field]
                            missing.remove(field)
                    if not missing:
                        break

            # Second pass: if filled_by_file is set but yields no release format,
            # scan remaining rows for one whose filename contains a format keyword.
            if merged.get('filled_by_file') and not self._extract_release_format(merged['filled_by_file']):
                for row in rows[1:]:
                    candidate = (row['filled_by_file'] or row['location_on_disk']
                                 or row['location_basename'] or '')
                    if candidate and self._extract_release_format(candidate):
                        merged['filled_by_file'] = row['filled_by_file'] or merged['filled_by_file']
                        merged['location_on_disk'] = row['location_on_disk'] or merged['location_on_disk']
                        merged['location_basename'] = row['location_basename'] or merged['location_basename']
                        break

            return merged

        except Exception as e:
            self.logger.warning(f"Best-version query failed for imdb_id={imdb_id} type={item_type}: {e}")
        return None

    def _best_version_by_ms_id(self, ms_item_id: str, item_type: str) -> Optional[Dict[str, Any]]:
        """
        Like _best_version but scoped to rows sharing the same ms_item_id rather
        than the same imdb_id. Used for movies so that Plex split items (each with
        a unique ratingKey / ms_item_id) only aggregate quality data across their
        own DB rows, not across all versions of the same movie.
        """
        if not ms_item_id or item_type not in ('episode', 'movie'):
            return None
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT ms_resolution, ms_hdr, ms_dolby_vision, ms_hdr_format,
                       ms_audio_codec, ms_audio_channels,
                       ms_video_codec, ms_media_container, ms_media_bitrate,
                       filled_by_file, location_on_disk, location_basename
                FROM media_items
                WHERE ms_item_id = ?
                  AND type = ?
                  AND state IN ('Collected', 'Upgrading')
                  AND ms_resolution IS NOT NULL
                ORDER BY
                    ({self._RES_RANK}) DESC,
                    ({self._HDR_RANK}) DESC,
                    ({self._AUD_RANK}) DESC,
                    CAST(COALESCE(ms_audio_channels, '0') AS REAL) DESC
            ''', (ms_item_id, item_type))
            rows = cursor.fetchall()
            conn.close()
            if not rows:
                return None
            merged = dict(rows[0])
            missing = [f for f in self._FILLABLE if not merged.get(f)]
            if missing:
                for row in rows[1:]:
                    for field in list(missing):
                        if row[field]:
                            merged[field] = row[field]
                            missing.remove(field)
                    if not missing:
                        break
            return merged
        except Exception as e:
            self.logger.warning(f"Best-version-by-ms-id query failed for ms_item_id={ms_item_id}: {e}")
        return None

    def _best_episode_data_for_show(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper — delegates to _best_version."""
        return self._best_version(imdb_id, 'episode')

    def _best_movie_version(self, imdb_id: str) -> Optional[Dict[str, Any]]:
        """Compatibility wrapper — delegates to _best_version."""
        return self._best_version(imdb_id, 'movie')

    def _build_media_info_from_db(self, item: Dict[str, Any],
                                   _best_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Build a media_info dict from columns already stored in media_items.

        Uses plex_* columns (populated after first successful overlay) and
        the regular pipeline 'resolution' column as fallback.

        For TV episodes, uses the highest-quality episode of the whole show
        ("best episode wins") rather than the single representative episode.
        For movies with multiple files (same imdb_id), uses the best version.

        Pass _best_data if already fetched (e.g. from _quality_hash_for_item)
        to avoid a redundant DB query.
        """
        import re as _re

        # ── Best-quality source selection ────────────────────────────────────
        # For TV shows: pick the best episode across all seasons.
        # For movies:   pick the best file/version if multiple exist.
        # We merge the best data over item so all subsequent reads see the winner's columns.
        item = dict(item)  # don't mutate caller's dict
        item_type = item.get('type')

        if _best_data is not None:
            best = _best_data
        elif item_type == 'episode':
            best = self._best_version(item.get('imdb_id') or '', 'episode')
        elif item_type == 'movie':
            # For split Plex items (each with a unique ms_item_id), only aggregate
            # quality data across DB rows that share the same ms_item_id (i.e. the
            # same Plex item / truly merged). If this item has a unique ms_item_id,
            # _best_version will only find one row and return its own data.
            # Fall back to imdb_id scope if ms_item_id is not set.
            _ms_id = item.get('ms_item_id')
            if _ms_id:
                best = self._best_version_by_ms_id(_ms_id, 'movie')
            else:
                best = self._best_version(item.get('imdb_id') or '', 'movie')
        else:
            best = None

        if best:
            self.logger.debug(
                f"Best-version for {item_type} imdb={item.get('imdb_id')}: "
                f"{best.get('ms_resolution')} / {best.get('ms_audio_codec')}")
            item.update({k: v for k, v in best.items() if v is not None})

        # Resolution: prefer ms_resolution, fall back to resolution (from filename backfill)
        resolution = item.get('ms_resolution') or item.get('resolution')
        if resolution:
            resolution = _re.sub(r'^(\d{3,4})$', r'\1p', str(resolution))

        # HDR / Dolby Vision — only ms_* columns exist
        hdr = bool(item.get('ms_hdr'))
        dolby_vision = bool(item.get('ms_dolby_vision'))

        # HDR type label: use stored ms_hdr_format first (set during last media server sync),
        # then fall back to filename parsing, then plain 'HDR'.
        file_path = (item.get('filled_by_file') or item.get('location_on_disk')
                     or item.get('location_basename') or '').strip()
        _fn_hdr_type = None
        if file_path:
            _fn_dv, _fn_hdr, _fn_hdr_type = self._extract_hdr_from_filename(file_path)
            if not hdr and not dolby_vision and (_fn_dv or _fn_hdr):
                # Media server missed it entirely — use filename detection
                dolby_vision = _fn_dv
                hdr = _fn_hdr

        # Primary: stored media server HDR format label; Secondary: filename; Fallback: 'HDR'
        hdr_label = item.get('ms_hdr_format') or _fn_hdr_type or ('HDR' if hdr else None)
        hdr_line1 = None
        hdr_line2 = None
        if dolby_vision and hdr:
            hdr_line1 = 'DV'
            hdr_line2 = hdr_label or 'HDR'
        elif dolby_vision:
            hdr_line1 = 'DV'
        elif hdr:
            hdr_line1 = hdr_label or 'HDR'

        # hdr_format kept as backward-compatible alias (now abbreviated)
        hdr_format = hdr_line1

        audio_codec    = (item.get('ms_audio_codec')    or '').strip() or None
        audio_channels = (item.get('ms_audio_channels') or '').strip() or None
        video_codec    = (item.get('ms_video_codec')    or '').strip() or None
        network        = (item.get('ms_network')        or '').strip() or None
        studio         = (item.get('ms_studio')         or '').strip() or None
        _cr_raw = (item.get('ms_content_rating') or '').strip()
        content_rating = self._normalize_content_rating(_cr_raw) if _cr_raw else None

        # Extract release format from filename (file_path already set above)
        format_str = self._extract_release_format(file_path) if file_path else None

        # Fetch ratings once here (24-hour in-process cache; all keys populated so
        # _enrich_media_info will not call _fetch_ratings a second time).
        imdb_id = item.get('imdb_id') or ''
        ratings = self._fetch_ratings(imdb_id, item_type) if imdb_id else {}

        # Fetch TV show status (only for episodes)
        status = self._fetch_show_status(imdb_id, item_type)

        # Version count — always keyed by imdb_id (consistent with _reset_content_changed_items)
        # and includes both Collected + Upgrading to avoid spurious hash mismatches when
        # an item is actively being upgraded.
        try:
            _vc_conn = _get_db_connection()
            _vc_cur  = _vc_conn.cursor()
            if item_type == 'episode' and imdb_id:
                # Count total duplicate copies per episode (episodes with >1 file),
                # summed across all seasons. e.g. ep with 2 files contributes 2.
                # Must be > 1 to match badge condition: versionCount > 1.
                _vc_cur.execute(
                    "SELECT COALESCE(SUM(ep_count), 0) FROM ("
                    "  SELECT COUNT(*) AS ep_count FROM media_items"
                    "  WHERE imdb_id = ? AND type = 'episode' AND state IN ('Collected', 'Upgrading')"
                    "  GROUP BY season_number, episode_number HAVING COUNT(*) > 1"
                    ")",
                    (imdb_id,))
            elif imdb_id:
                # For movies, scope by ms_item_id so split Plex items (unique ratingKey
                # per version) each show their own count rather than all versions of the
                # same movie being counted together. Falls back to imdb_id count when
                # ms_item_id is not set.
                _ms_id = item.get('ms_item_id')
                if _ms_id:
                    _vc_cur.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE ms_item_id = ? AND type = 'movie' AND state IN ('Collected', 'Upgrading')",
                        (_ms_id,))
                else:
                    _vc_cur.execute(
                        "SELECT COUNT(*) FROM media_items "
                        "WHERE imdb_id = ? AND type = 'movie' AND state IN ('Collected', 'Upgrading')",
                        (imdb_id,))
            else:
                _vc_cur = None
            version_count = int(_vc_cur.fetchone()[0]) if _vc_cur else 1
            _vc_conn.close()
        except Exception:
            version_count = 1

        return {
            'resolution': resolution,
            'hdr': hdr,
            'dolby_vision': dolby_vision,
            'hdr_format': hdr_format,
            'hdr_line1': hdr_line1,
            'hdr_line2': hdr_line2,
            'audio_codec': audio_codec,
            'audio_channels': audio_channels,
            'video_codec': video_codec,
            'container': None,
            'bitrate': None,
            'format': format_str,
            'imdb_rating': ratings.get('imdb_rating'),
            'tmdb_rating': ratings.get('tmdb_rating'),
            'trakt_rating': ratings.get('trakt_rating'),
            'rt_critics_score': ratings.get('rt_critics_score'),
            'rt_user_score': ratings.get('rt_user_score'),
            'network': network,
            'studio': studio,
            'content_rating': content_rating,
            'status': status,
            'version_count': version_count,
            'definition': self._resolution_to_definition(resolution),
        }

    @staticmethod
    def _resolution_to_definition(resolution: Optional[str]) -> str:
        """Map a resolution string to a definition label (UHD/QHD/FHD/HD/SD)."""
        import re as _re
        r = (resolution or '').lower().replace(' ', '')
        if _re.match(r'^(2160p?|4k|uhd)', r):  return 'UHD'
        if _re.match(r'^(1440p?|qhd)',    r):  return 'QHD'
        if _re.match(r'^(1080p?|fhd)',    r):  return 'FHD'
        if _re.match(r'^(720p?|hd)',      r):  return 'HD'
        return 'SD'

    @staticmethod
    def _extract_hdr_from_filename(file_path: str) -> tuple:
        """
        Extract HDR/DV flags and specific HDR type from a filename.

        Returns (dolby_vision: bool, hdr: bool, hdr_type: str|None).
        hdr_type is one of 'HDR10+', 'HDR10', 'HLG', 'HDR', or None.
        Ordered most-specific first so HDR10+ is never mistaken for HDR10.
        """
        import re as _re
        name = Path(file_path).name.lower() if file_path else ''

        # Dolby Vision keywords (check before generic HDR)
        dv = bool(_re.search(r'\bdv\b|\bdovi\b|dolby[\s.\-_]?vision', name))

        # HDR type — ordered most-specific to least-specific
        hdr_type = None
        if _re.search(r'\bhdr10[\s.\-_]?(?:plus|\+)\b|\bhdr10\+', name):
            hdr_type = 'HDR10+'
        elif _re.search(r'\bhdr10\b', name):
            hdr_type = 'HDR10'
        elif _re.search(r'\bhlg\b', name):
            hdr_type = 'HLG'
        elif _re.search(r'\bhdr\b', name):
            hdr_type = 'HDR'

        hdr = hdr_type is not None
        if dv:
            hdr = True  # DV implies HDR

        return dv, hdr, hdr_type

    @staticmethod
    def _extract_release_format(file_path: str) -> Optional[str]:
        """Extract release format (BluRay, WEB-DL, etc.) from a file path."""
        import re as _re
        name = Path(file_path).name if file_path else ''
        name_lower = name.lower()
        # Ordered by specificity (most specific first)
        patterns = [
            (r'\bbdremux\b',              'BDRemux'),
            (r'\bbd-?remux\b',            'BDRemux'),
            (r'\bremux\b',                'Remux'),
            (r'\bbluray\b|\bblu-?ray\b',  'BluRay'),
            (r'\bbdrip\b',                'BDRip'),
            (r'\bweb-?dl\b',              'WEB-DL'),
            (r'\bwebrip\b',               'WEBRip'),
            (r'\bweb\b',                  'WEB'),
            (r'\bhdtv\b',                 'HDTV'),
            (r'\bhdcam\b',                'HDCAM'),
            (r'\bdvd-?rip\b|\bdvdrip\b',  'DVDRip'),
            (r'\bdvd\b',                  'DVD'),
            (r'\bcam\b',                  'CAM'),
        ]
        for pattern, label in patterns:
            if _re.search(pattern, name_lower):
                return label
        return None

    # Simple in-memory ratings cache: {imdb_id: (timestamp, dict)}
    _ratings_cache: Dict[str, Any] = {}
    # Trakt rate-limit throttle: last successful call timestamp and backoff-until time.
    # Trakt allows 1000 req/5min; we space calls ~300ms apart to stay well under limit.
    _trakt_last_call: float = 0.0
    _trakt_backoff_until: float = 0.0  # set on 429; skip Trakt until this time passes

    @staticmethod
    def _normalize_content_rating(raw: str) -> str:
        """Normalize verbose content-rating strings to compact abbreviations."""
        mapping = {
            'not rated': 'NR',
            'unrated':   'NR',
            'nr':        'NR',
        }
        return mapping.get(raw.lower(), raw)

    @staticmethod
    def _extract_plex_ratings(plex_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract IMDb, TMDB, and RT ratings from Plex metadata JSON.

        Plex embeds external ratings in a 'Rating' array on each metadata item:
          [{"image": "imdb://image.rating", "value": 7.4, "type": "audience"}, ...]

        image prefixes: imdb://, themoviedb://, rottentomatoes://
        RT type="critic" → Tomatometer, type="audience" → Audience score
        Values are already on 0-10 scale for IMDb/TMDB; RT is 0-1 (multiply ×100).
        """
        ratings = {}
        for r in plex_metadata.get('Rating', []):
            image = (r.get('image') or '').lower()
            val = r.get('value')
            if val is None:
                continue
            v = float(val)
            if image.startswith('imdb://'):
                ratings['imdb_rating'] = round(v, 1)
            elif image.startswith('themoviedb://'):
                ratings['tmdb_rating'] = round(v, 1)
            elif image.startswith('rottentomatoes://'):
                # Plex RT values are 0-1 scale → convert to 0-100 percentage
                score = round(v * 100) if v <= 1.0 else round(v)
                if r.get('type') == 'audience':
                    ratings['rt_user_score'] = score
                else:
                    ratings['rt_critics_score'] = score
        return ratings

    def _fetch_ratings(self, imdb_id: str, item_type: Optional[str] = None,
                       plex_metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Fetch ratings for a media item. Sources in priority order:
          1. In-memory cache (24h TTL — avoids any I/O within a single sync run)
          2. SQLite ratings cache (7-day TTL — survives container restarts, eliminates
             cold-start API burst for large libraries)
          3. TMDB direct API (tmdb_rating only, via /find by IMDB ID)
          4. MDBList API (IMDb, TMDB, Trakt, RT — all in one call; 0.2s pre-sleep)
          5. Combined Plex metadata + Trakt fallback (when MDBList fails/rate-limited)
        """
        import time
        import json
        import requests
        cache = OverlayManager._ratings_cache

        # ── 1. In-memory cache (fastest path, no I/O) ────────────────────────
        cached = cache.get(imdb_id)
        if cached:
            ts, data = cached
            if time.time() - ts < 86400:
                try:
                    from utilities.settings import get_setting as _gs_mem
                    _trakt_ok = not _gs_mem('Trakt', 'client_id', '') or data.get('trakt_rating')
                    _mdblist_ok = not _gs_mem('MDBList', 'api_key', '') or (data.get('imdb_rating') and data.get('tmdb_rating'))
                    _in_backoff = time.time() < OverlayManager._trakt_backoff_until
                    if (_trakt_ok and _mdblist_ok) or _in_backoff:
                        return data
                except Exception:
                    return data

        # ── 2. SQLite persistent cache (survives restarts) ───────────────────
        _RATINGS_TTL = 7 * 86400  # 7 days
        try:
            from database.core import get_db_connection
            from utilities.settings import get_setting as _gs
            _trakt_configured = bool(_gs('Trakt', 'client_id', ''))
            _mdblist_configured = bool(_gs('MDBList', 'api_key', ''))
            _in_backoff = time.time() < OverlayManager._trakt_backoff_until
            _db = get_db_connection()
            _row = _db.execute(
                'SELECT ratings, fetched_at FROM overlay_ratings_cache WHERE imdb_id = ?',
                (imdb_id,)
            ).fetchone()
            _db.close()
            if _row and (time.time() - _row[1]) < _RATINGS_TTL:
                data = json.loads(_row[0])
                _trakt_miss = _trakt_configured and not data.get('trakt_rating') and not _in_backoff
                _mdblist_miss = _mdblist_configured and not (data.get('imdb_rating') and data.get('tmdb_rating'))
                if _trakt_miss or _mdblist_miss:
                    self.logger.debug(
                        f"SQLite cache for {imdb_id} incomplete (trakt_miss={_trakt_miss}, mdblist_miss={_mdblist_miss}); will re-fetch")
                else:
                    cache[imdb_id] = (_row[1], data)   # warm in-memory cache too
                    return data
        except Exception as _cache_exc:
            self.logger.debug(f"SQLite ratings cache read failed for {imdb_id}: {_cache_exc}")

        def _persist_ratings(rat: dict) -> None:
            """Write ratings to SQLite cache. Never raises — failures are logged only."""
            try:
                _c = get_db_connection()
                _c.execute(
                    'INSERT OR REPLACE INTO overlay_ratings_cache (imdb_id, ratings, fetched_at) '
                    'VALUES (?, ?, ?)',
                    (imdb_id, json.dumps(rat), time.time())
                )
                _c.commit()
                _c.close()
            except Exception as _w:
                self.logger.debug(f"SQLite ratings cache write failed for {imdb_id}: {_w}")

        # ── 3. TMDB direct API (tmdb_rating only; uses /find by IMDB ID) ────────
        ratings = {}
        try:
            from utilities.settings import get_setting as _gs_tmdb
            _tmdb_key = _gs_tmdb('TMDB', 'api_key', '')
            if _tmdb_key:
                _tmdb_resp = requests.get(
                    f'https://api.themoviedb.org/3/find/{imdb_id}',
                    params={'api_key': _tmdb_key, 'external_source': 'imdb_id'},
                    timeout=5
                )
                if _tmdb_resp.status_code == 200:
                    _tmdb_data = _tmdb_resp.json()
                    _tmdb_results = (
                        _tmdb_data.get('tv_results') or
                        _tmdb_data.get('movie_results') or []
                    )
                    if _tmdb_results:
                        _va = _tmdb_results[0].get('vote_average')
                        if _va is not None:
                            v = float(_va)
                            if v > 10:
                                v = v / 10
                            ratings['tmdb_rating'] = round(v, 1)
                            self.logger.debug(
                                f"TMDB direct rating for {imdb_id}: {ratings['tmdb_rating']}")
        except Exception as _tmdb_exc:
            self.logger.debug(f"TMDB direct fetch failed for {imdb_id}: {_tmdb_exc}")

        # ── 4. MDBList (primary — provides IMDb, TMDB, Trakt, RT in one call) ─
        mdblist_ok = False
        try:
            from utilities.mdblist_api import get_mdblist_api_key, is_mdblist_configured
            if is_mdblist_configured():
                api_key = get_mdblist_api_key()
                time.sleep(0.2)  # proactive throttle — stays well under MDBList rate limit
                resp = requests.get(
                    'https://mdblist.com/api/',
                    params={'apikey': api_key, 'i': imdb_id},
                    timeout=5
                )
                resp.raise_for_status()
                data = resp.json()
                if not data.get('response', True):
                    self.logger.warning(f"MDBList API error for {imdb_id}: {data.get('error', 'Unknown error')}")
                    # Don't cache — fall through to Plex fallback
                else:
                    for r in data.get('ratings', []):
                        src = (r.get('source') or '').lower()
                        val = r.get('value')
                        if val is None:
                            continue
                        v = float(val)
                        if src == 'imdb':
                            ratings['imdb_rating'] = round(v, 1)
                        elif src == 'tmdb':
                            if not ratings.get('tmdb_rating'):  # don't overwrite TMDB direct API result
                                if v > 10:
                                    v = v / 10
                                ratings['tmdb_rating'] = round(v, 1)
                        elif src == 'trakt':
                            if v > 10:
                                v = v / 10
                            ratings['trakt_rating'] = round(v, 1)
                        elif src == 'tomatoes':
                            ratings['rt_critics_score'] = round(v)
                        elif src in ('popcorn', 'tomatoesaudience'):
                            ratings['rt_user_score'] = round(v)
                    mdblist_ok = True
        except Exception as e:
            self.logger.debug(f"MDBList fetch failed for {imdb_id}: {e}")

        if mdblist_ok:
            # Supplement missing IMDb rating from dataset if MDBList didn't return one
            if not ratings.get('imdb_rating'):
                try:
                    from overlays.imdb_dataset import get_imdb_dataset_rating
                    ds = get_imdb_dataset_rating(imdb_id)
                    if ds is not None:
                        ratings['imdb_rating'] = ds
                except Exception:
                    pass
            cache[imdb_id] = (time.time(), ratings)
            _persist_ratings(ratings)
            self.logger.debug(f"Ratings fetched for {imdb_id} (MDBList): {ratings}")
            return ratings

        # ── 4. Combined Plex + Trakt fallback ────────────────────────────────
        # Plex metadata:  IMDb, TMDB, RT scores (already fetched — zero extra calls)
        # Trakt API:      Trakt community score (300ms throttle, 429 backoff)
        # IMDb dataset:   Fills missing IMDb rating with zero extra API calls
        if plex_metadata:
            try:
                ratings.update(self._extract_plex_ratings(plex_metadata))
            except Exception as e:
                self.logger.debug(f"Plex ratings extraction failed for {imdb_id}: {e}")

        # Supplement missing IMDb rating from dataset (zero API calls)
        if not ratings.get('imdb_rating'):
            try:
                from overlays.imdb_dataset import get_imdb_dataset_rating
                ds = get_imdb_dataset_rating(imdb_id)
                if ds is not None:
                    ratings['imdb_rating'] = ds
            except Exception:
                pass

        # Track whether Trakt was skipped due to backoff — if so, don't persist
        # to SQLite so the item is retried on the next sync run rather than
        # caching an incomplete result for 7 days.
        trakt_skipped_backoff = False
        try:
            from utilities.settings import get_setting
            client_id = get_setting('Trakt', 'client_id', '')
            if client_id:
                # Respect backoff window set by a prior 429 response
                if time.time() < OverlayManager._trakt_backoff_until:
                    trakt_skipped_backoff = True
                    self.logger.debug(
                        f"Trakt backoff active for {imdb_id}, skipping until "
                        f"{OverlayManager._trakt_backoff_until - time.time():.0f}s")
                else:
                    # Space calls ~300ms apart to stay under 1000/5min rate limit
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
                            ratings['trakt_rating'] = round(float(trakt_rating), 1)
                    elif trakt_resp.status_code == 429:
                        retry_after = int(trakt_resp.headers.get('Retry-After', 60))
                        OverlayManager._trakt_backoff_until = time.time() + retry_after
                        trakt_skipped_backoff = True
                        self.logger.warning(
                            f"Trakt rate-limited (429) for {imdb_id}; "
                            f"backing off {retry_after}s")
                    else:
                        self.logger.debug(
                            f"Trakt API returned {trakt_resp.status_code} for {imdb_id}")
        except Exception as e:
            self.logger.debug(f"Trakt fallback fetch failed for {imdb_id}: {e}")

        if ratings:
            cache[imdb_id] = (time.time(), ratings)
            # Only persist to SQLite when the result is complete — don't cache
            # items where Trakt was skipped due to rate-limit backoff so they
            # are retried on the next sync run instead of staying stale for 7 days.
            if not trakt_skipped_backoff:
                _persist_ratings(ratings)
            self.logger.info(f"Ratings from Plex+Trakt fallback for {imdb_id}: {ratings}")
        self.logger.debug(f"Ratings fetched for {imdb_id}: {ratings}")
        return ratings

    def _fetch_show_status(self, imdb_id: str, item_type: str) -> Optional[str]:
        """
        Fetch TV show status for overlay badges.

        Primary source: main DB tv_shows table (joined with 14-day upcoming-episode
        check to distinguish 'Airing' from plain 'Returning').
        Fallback: cli_battery items.media_status — useful for newly-added shows
        whose tv_shows row may not yet be populated.

        Returns one of: 'Airing', 'Returning', 'Ended', 'Canceled', or None.
        Only meaningful for TV shows (type == 'episode').
        """
        if item_type != 'episode' or not imdb_id:
            return None

        def _normalise(raw_status: str, has_upcoming: bool = False) -> Optional[str]:
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

        # ── Primary: main DB tv_shows ─────────────────────────────────────
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ts.status,
                       EXISTS(
                           SELECT 1 FROM media_items mi
                           WHERE mi.imdb_id = ts.imdb_id
                             AND mi.type = 'episode'
                             AND date(mi.release_date) BETWEEN date('now') AND date('now', '+14 days')
                           LIMIT 1
                       ) AS has_upcoming
                FROM tv_shows ts
                WHERE ts.imdb_id = ?
            """, (imdb_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row['status']:
                return _normalise(row['status'], bool(row['has_upcoming']))
        except Exception as e:
            self.logger.debug(f"Could not fetch show status from tv_shows for {imdb_id}: {e}")

        # ── Fallback: cli_battery items.media_status ──────────────────────
        # Newly-added shows may have battery metadata before tv_shows is populated.
        try:
            import os
            battery_path = os.path.join(
                os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'cli_battery.db'
            )
            import sqlite3 as _sqlite3
            bconn = _sqlite3.connect(battery_path, timeout=5)
            bconn.row_factory = _sqlite3.Row
            bcursor = bconn.cursor()
            bcursor.execute(
                "SELECT media_status FROM items WHERE imdb_id = ? AND type = 'show' LIMIT 1",
                (imdb_id,)
            )
            brow = bcursor.fetchone()
            bconn.close()
            if brow and brow['media_status']:
                return _normalise(brow['media_status'])
        except Exception as e:
            self.logger.debug(f"Could not fetch show status from cli_battery for {imdb_id}: {e}")

        return None

    def _enrich_media_info(self, info: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add computed fields to any media_info dict regardless of source.

        Sets hdr_line1/hdr_line2 (abbreviated HDR labels), format (from
        filename), and imdb/tmdb/trakt ratings (from MDBList API).
        Called on both the Plex-metadata path and the DB-fallback path.
        """
        # ── Carry file path fields into info so renderers (e.g. file_match badge)
        # can access them directly from media_info without needing the item dict.
        for _fp_field in ('filled_by_file', 'location_on_disk', 'location_basename'):
            if not info.get(_fp_field) and item.get(_fp_field):
                info[_fp_field] = item[_fp_field]

        # ── Release format + HDR from filename ───────────────────────────
        file_path = (item.get('filled_by_file') or item.get('location_on_disk')
                     or item.get('location_basename') or '').strip()

        if not info.get('format'):
            info['format'] = self._extract_release_format(file_path) if file_path else None

        # ── HDR / Dolby Vision — filename refines the specific type ─────────
        hdr          = info.get('hdr', False)
        dolby_vision = info.get('dolby_vision', False)
        _fn_hdr_type = None

        if file_path:
            _fn_dv, _fn_hdr, _fn_hdr_type = self._extract_hdr_from_filename(file_path)
            if not hdr and not dolby_vision and (_fn_dv or _fn_hdr):
                # Plex missed it entirely — use filename detection
                dolby_vision = _fn_dv
                hdr = _fn_hdr
                info['dolby_vision'] = dolby_vision
                info['hdr'] = hdr
                self.logger.debug(
                    f"HDR from filename: dv={dolby_vision} hdr={hdr} "
                    f"type={_fn_hdr_type} ({Path(file_path).name})")

        # ── Abbreviated HDR dual-line labels ─────────────────────────────
        # Priority: media server hdr_format from info dict (live data) → stored
        # ms_hdr_format from DB (persisted from last sync) → filename-derived
        # type → plain 'HDR' fallback.
        # Skip DV-tagged values ('Dolby Vision') — DV is handled by dolby_vision bool;
        # hdr_label is used for the HDR subtype label (HDR10, HDR10+, HLG, HDR).
        _raw_plex_fmt = info.get('hdr_format') or ''
        _plex_hdr_label = _raw_plex_fmt if _raw_plex_fmt.lower() not in ('dolby vision', 'dv', '') else None
        hdr_label = _plex_hdr_label or item.get('ms_hdr_format') or _fn_hdr_type or ('HDR' if hdr else None)
        hdr_line1 = None
        hdr_line2 = None
        if dolby_vision and hdr:
            hdr_line1 = 'DV'
            hdr_line2 = hdr_label or 'HDR'
        elif dolby_vision:
            hdr_line1 = 'DV'
        elif hdr:
            hdr_line1 = hdr_label or 'HDR'

        info['hdr_line1'] = hdr_line1
        info['hdr_line2'] = hdr_line2
        # hdr_format: abbreviated label for backward compat (badge display)
        info['hdr_format'] = hdr_line1
        # hdr_subtype: pure HDR standard label (HDR10+/HDR10/HLG/HDR), excluding DV.
        # This is what gets persisted to ms_hdr_format so it can be reused as
        # hdr_label on the DB-fallback path without conflating DV with the HDR type.
        info['hdr_subtype'] = hdr_label if hdr else None

        # ── Ratings (MDBList API) ─────────────────────────────────────────
        # Always fetch so RT scores are populated even when IMDb/TMDB are already set.
        # _fetch_ratings uses a 24-hour in-process cache so this is cheap.
        # Fetch ratings whenever any score is missing (cache makes this cheap).
        _needs_ratings = (
            not info.get('imdb_rating') or
            not info.get('tmdb_rating') or
            not info.get('rt_critics_score') or
            not info.get('rt_user_score')
        )
        if _needs_ratings:
            imdb_id = item.get('imdb_id') or ''
            ratings = self._fetch_ratings(
                imdb_id, item.get('type'),
                plex_metadata=item.get('_plex_metadata')
            ) if imdb_id else {}
            # Use 'or' assignment so existing None values get overwritten with real data
            info['imdb_rating']      = info.get('imdb_rating')      or ratings.get('imdb_rating')
            info['tmdb_rating']      = info.get('tmdb_rating')      or ratings.get('tmdb_rating')
            info['trakt_rating']     = info.get('trakt_rating')     or ratings.get('trakt_rating')
            info['rt_critics_score'] = info.get('rt_critics_score') or ratings.get('rt_critics_score')
            info['rt_user_score']    = info.get('rt_user_score')    or ratings.get('rt_user_score')

        # ── Network / Studio / Content Rating ────────────────────────────
        # Fall back to ms_* DB columns if not already set by media server live fetch
        if not info.get('network'):
            info['network'] = (item.get('ms_network') or '').strip() or None
        if not info.get('studio'):
            info['studio'] = (item.get('ms_studio') or '').strip() or None
        if not info.get('content_rating'):
            _cr_raw = (item.get('ms_content_rating') or '').strip()
            info['content_rating'] = self._normalize_content_rating(_cr_raw) if _cr_raw else None

        # ── Battery DB fallback for network (TV shows only) ───────────────
        # Plex often doesn't return 'network' in show metadata; use Trakt/TVDB
        # data from cli_battery as a last resort.
        if not info.get('network') and item.get('type') == 'episode':
            imdb_id = item.get('imdb_id') or ''
            if imdb_id:
                try:
                    from cli_battery.app.direct_api import DirectAPI
                    show_meta, _ = DirectAPI.get_show_metadata(imdb_id)
                    if show_meta and show_meta.get('network'):
                        info['network'] = str(show_meta['network']).strip() or None
                        self.logger.debug(
                            f"Battery network for {imdb_id}: {info['network']}")
                except Exception as _bat_exc:
                    self.logger.debug(f"Battery network lookup failed for {imdb_id}: {_bat_exc}")

        # ── Media server fallback for studio (movies) ─────────────────────────────
        # If studio is still missing and we have a ms_item_id, fetch
        # show/movie-level metadata directly from the media server.  Studio is a top-level
        # field the media server always returns even when media-stream info is incomplete.
        # Persist the fetched value to ms_studio so future renders skip this call.
        if not info.get('studio') and item.get('type') != 'episode':
            plex_key = item.get('ms_item_id') or ''
            media_item_id = item.get('id')
            if plex_key and self.client:
                try:
                    movie_meta = self.client.get_media_metadata(plex_key)
                    studio_val = (movie_meta.get('studio') or movie_meta.get('Studio') or '').strip()
                    if studio_val:
                        info['studio'] = studio_val
                        self.logger.debug(f"Media server studio fallback for {plex_key}: {studio_val}")
                        # Persist so the next render reads it from DB without a media server call
                        if media_item_id:
                            try:
                                _conn = _get_db_connection()
                                _conn.execute(
                                    "UPDATE media_items SET ms_studio = ? WHERE id = ?",
                                    (studio_val, media_item_id))
                                _conn.commit()
                                _conn.close()
                            except Exception as _db_exc:
                                self.logger.debug(f"Could not persist studio for item {media_item_id}: {_db_exc}")
                except Exception as _plex_exc:
                    self.logger.debug(f"Media server studio fallback failed for {plex_key}: {_plex_exc}")

        # ── TV Show Status ────────────────────────────────────────────────
        # Only populated for TV episodes; None for movies
        if not info.get('status'):
            imdb_id = item.get('imdb_id') or ''
            info['status'] = self._fetch_show_status(imdb_id, item.get('type', ''))

        # ── Definition label (derived from resolution) ────────────────────
        info['definition'] = self._resolution_to_definition(info.get('resolution'))

        # ── Version count (how many files exist for this title) ───────────
        if 'version_count' not in info:
            imdb_id     = item.get('imdb_id') or ''
            item_type   = item.get('type', '')
            season_num  = item.get('season_number')  # None for show posters, set for season posters
            try:
                conn = _get_db_connection()
                cur  = conn.cursor()
                if item_type == 'episode' and imdb_id:
                    # Count total duplicate copies per episode (episodes with >1 file).
                    # Scope to the specific season when season_number is available (season
                    # posters), otherwise count across all seasons (show posters).
                    # Must be > 1 to match badge condition: versionCount > 1.
                    if season_num is not None:
                        cur.execute(
                            "SELECT COALESCE(SUM(ep_count), 0) FROM ("
                            "  SELECT COUNT(*) AS ep_count FROM media_items"
                            "  WHERE imdb_id = ? AND season_number = ? AND type = 'episode'"
                            "    AND state IN ('Collected', 'Upgrading')"
                            "  GROUP BY episode_number HAVING COUNT(*) > 1"
                            ")",
                            (imdb_id, season_num))
                    else:
                        cur.execute(
                            "SELECT COALESCE(SUM(ep_count), 0) FROM ("
                            "  SELECT COUNT(*) AS ep_count FROM media_items"
                            "  WHERE imdb_id = ? AND type = 'episode' AND state IN ('Collected', 'Upgrading')"
                            "  GROUP BY season_number, episode_number HAVING COUNT(*) > 1"
                            ")",
                            (imdb_id,))
                elif imdb_id:
                    # For movies, scope by ms_item_id so split Plex items each show
                    # their own count. Falls back to imdb_id when ms_item_id not set.
                    _ms_id = item.get('ms_item_id')
                    if _ms_id:
                        cur.execute(
                            "SELECT COUNT(*) FROM media_items "
                            "WHERE ms_item_id = ? AND type = 'movie' AND state IN ('Collected', 'Upgrading')",
                            (_ms_id,))
                    else:
                        cur.execute(
                            "SELECT COUNT(*) FROM media_items "
                            "WHERE imdb_id = ? AND type = 'movie' AND state IN ('Collected', 'Upgrading')",
                            (imdb_id,))
                else:
                    cur = None
                if cur:
                    row = cur.fetchone()
                    info['version_count'] = int(row[0]) if row else 1
                else:
                    info['version_count'] = 1
                conn.close()
            except Exception:
                info['version_count'] = 1

        return info

    def _get_media_info(self, media_item_id: int, item: Dict[str, Any],
                        plex_rating_key: str,
                        _best_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Get media info, preferring media server API data but falling back to DB data.

        Does NOT block on media server analysis — if data is unavailable or
        incomplete, DB data is used so the overlay can still be generated.
        Enrichment (HDR labels, format, ratings) is applied to both paths.
        """
        if self._jellyfin_mode:
            return self._get_media_info_jellyfin(media_item_id, item, plex_rating_key, _best_data)

        # ── Plex path ─────────────────────────────────────────────────────
        # For TV shows the show-level Plex endpoint has no media streams (resolution/codec
        # only exist on individual episodes). Skip straight to best-episode fetch.
        if item.get('type') == 'episode':
            try:
                ep_metadata = self.plex.get_show_best_episode_media(plex_rating_key)
                if ep_metadata:
                    ep_info = self.extractor.extract_from_plex_metadata(ep_metadata)
                    if self.extractor.is_complete(ep_info):
                        self.logger.info(
                            f"Using best-episode Plex media info for show {plex_rating_key}: "
                            f"res={ep_info.get('resolution')}, "
                            f"audio={ep_info.get('audio_codec')}")
                        # Fetch show-level metadata for network/studio/content_rating
                        # and ratings fallback (Rating array has IMDb/TMDB/RT scores).
                        show_metadata = None
                        try:
                            show_metadata = self.plex.get_media_metadata(plex_rating_key)
                            if not ep_info.get('network') and not ep_info.get('studio'):
                                ep_info['network'] = (show_metadata.get('network') or
                                                      show_metadata.get('Network') or '').strip() or None
                                ep_info['studio'] = (show_metadata.get('studio') or
                                                     show_metadata.get('Studio') or '').strip() or None
                                if not ep_info.get('content_rating'):
                                    _cr_raw = (show_metadata.get('contentRating') or
                                               show_metadata.get('content_rating') or '').strip()
                                    ep_info['content_rating'] = self._normalize_content_rating(_cr_raw) if _cr_raw else None
                                self.logger.info(
                                    f"Show {plex_rating_key} network={ep_info.get('network')} "
                                    f"studio={ep_info.get('studio')}")
                        except Exception as _show_exc:
                            self.logger.debug(
                                f"Could not fetch show-level metadata for {plex_rating_key}: {_show_exc}")
                        # Build enrich_item starting from original item (preserves
                        # imdb_id, type, ms_* fields needed for ratings/status),
                        # then overlay file-path fields from best DB episode so
                        # format (filled_by_file) comes from the ranked episode list.
                        # Reuse _best_data if already fetched by the caller.
                        enrich_item = dict(item)
                        best_db = _best_data if _best_data is not None else self._best_version(item.get('imdb_id') or '', 'episode')
                        if best_db:
                            for _f in ('filled_by_file', 'location_on_disk',
                                       'location_basename'):
                                if best_db.get(_f):
                                    enrich_item[_f] = best_db[_f]
                        # Preserve network/studio/content_rating from ep_info if already set
                        for _f in ('network', 'studio', 'content_rating'):
                            if ep_info.get(_f):
                                enrich_item[_f] = ep_info[_f]
                        # Pass show-level Plex metadata for ratings fallback (zero extra call)
                        if show_metadata:
                            enrich_item['_plex_metadata'] = show_metadata
                        return self._enrich_media_info(ep_info, enrich_item)
            except Exception as _ep_exc:
                self.logger.warning(
                    f"Best-episode Plex fetch failed for show {plex_rating_key}: {_ep_exc}")
        else:
            # Movies — try Plex metadata directly
            try:
                self.logger.info(f"Fetching Plex metadata for {plex_rating_key}")
                metadata = self.plex.get_media_metadata(plex_rating_key)
                plex_info = self.extractor.extract_from_plex_metadata(metadata)

                if self.extractor.is_complete(plex_info):
                    self.logger.info(f"Using Plex metadata for {plex_rating_key}")
                    # Build enrich_item from original item (preserves imdb_id, type,
                    # ms_* fields), then overlay file-path fields from best DB
                    # version so format (filled_by_file) uses the ranked version.
                    # Reuse _best_data if already fetched by the caller.
                    # Movies: scope to ms_item_id so split Plex items each use only
                    # their own DB rows for file-path fields (filled_by_file etc).
                    enrich_item = dict(item)
                    imdb_id = item.get('imdb_id') or ''
                    _mv_ms_id = item.get('ms_item_id') or ''
                    if _best_data is not None:
                        best_db = _best_data
                    elif _mv_ms_id:
                        best_db = self._best_version_by_ms_id(_mv_ms_id, 'movie')
                    elif imdb_id:
                        best_db = self._best_version(imdb_id, 'movie')
                    else:
                        best_db = None
                    if best_db:
                        for _f in ('filled_by_file', 'location_on_disk',
                                   'location_basename'):
                            if best_db.get(_f):
                                enrich_item[_f] = best_db[_f]
                    # Pass Plex metadata for ratings fallback (zero extra API call)
                    enrich_item['_plex_metadata'] = metadata
                    return self._enrich_media_info(plex_info, enrich_item)

                self.logger.warning(
                    f"Plex metadata incomplete for {plex_rating_key} "
                    f"(resolution={plex_info.get('resolution')}, "
                    f"video_codec={plex_info.get('video_codec')}), "
                    f"falling back to DB data")

            except Exception as e:
                try:
                    import requests as _req
                    if isinstance(e, _req.exceptions.HTTPError) and \
                            e.response is not None and e.response.status_code == 404:
                        raise  # Propagate 404 — caller will reset the stale key
                except (ImportError, AttributeError):
                    pass
                self.logger.warning(
                    f"Could not fetch Plex metadata for {plex_rating_key}: {e}; "
                    f"using DB data instead")

        # Fall back to DB data (pass _best_data to avoid re-querying)
        db_info = self._build_media_info_from_db(item, _best_data=_best_data)
        self.logger.info(
            f"Using DB data for {item.get('title', media_item_id)}: "
            f"resolution={db_info.get('resolution')}, "
            f"audio={db_info.get('audio_codec')}")
        return db_info

    def _get_media_info_jellyfin(self, media_item_id: int, item: Dict[str, Any],
                                  ms_item_id: str,
                                  _best_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Jellyfin/Emby variant of _get_media_info.

        For TV shows: uses get_show_best_episode_media to find the best-quality episode
        then extracts media info from its MediaStreams.
        For movies: uses get_media_metadata directly.
        Falls back to DB data if Jellyfin is unavailable or returns incomplete info.
        """
        if item.get('type') == 'episode':
            try:
                ep_metadata = self.client.get_show_best_episode_media(ms_item_id)
                if ep_metadata:
                    ep_info = self.extractor.extract_from_jellyfin_metadata(ep_metadata)
                    if self.extractor.is_complete(ep_info):
                        self.logger.info(
                            f"Using best-episode Jellyfin media info for show {ms_item_id}: "
                            f"res={ep_info.get('resolution')}, "
                            f"audio={ep_info.get('audio_codec')}")
                        # Fetch show-level metadata for network/studio/content_rating
                        show_metadata = None
                        try:
                            show_metadata = self.client.get_media_metadata(ms_item_id)
                            if not ep_info.get('network') and not ep_info.get('studio'):
                                ep_info['network'] = (show_metadata.get('network') or
                                                      show_metadata.get('Network') or '').strip() or None
                                ep_info['studio'] = (show_metadata.get('studio') or
                                                     show_metadata.get('Studio') or '').strip() or None
                                if not ep_info.get('content_rating'):
                                    _cr_raw = (show_metadata.get('OfficialRating') or
                                               show_metadata.get('content_rating') or '').strip()
                                    ep_info['content_rating'] = self._normalize_content_rating(_cr_raw) if _cr_raw else None
                                self.logger.info(
                                    f"Jellyfin show {ms_item_id} network={ep_info.get('network')} "
                                    f"studio={ep_info.get('studio')}")
                        except Exception as _show_exc:
                            self.logger.debug(
                                f"Could not fetch Jellyfin show metadata for {ms_item_id}: {_show_exc}")
                        enrich_item = dict(item)
                        best_db = _best_data if _best_data is not None else self._best_version(item.get('imdb_id') or '', 'episode')
                        if best_db:
                            for _f in ('filled_by_file', 'location_on_disk', 'location_basename'):
                                if best_db.get(_f):
                                    enrich_item[_f] = best_db[_f]
                        for _f in ('network', 'studio', 'content_rating'):
                            if ep_info.get(_f):
                                enrich_item[_f] = ep_info[_f]
                        return self._enrich_media_info(ep_info, enrich_item)
            except Exception as _ep_exc:
                try:
                    import requests as _req
                    if isinstance(_ep_exc, _req.exceptions.HTTPError) and \
                            _ep_exc.response is not None and \
                            _ep_exc.response.status_code in (400, 404):
                        raise  # Propagate — caller will reset the stale key
                except (ImportError, AttributeError):
                    pass
                self.logger.warning(
                    f"Best-episode Jellyfin fetch failed for show {ms_item_id}: {_ep_exc}")
        else:
            # Movies
            try:
                self.logger.info(f"Fetching Jellyfin metadata for {ms_item_id}")
                metadata = self.client.get_media_metadata(ms_item_id)
                jf_info = self.extractor.extract_from_jellyfin_metadata(metadata)

                if self.extractor.is_complete(jf_info):
                    self.logger.info(f"Using Jellyfin metadata for {ms_item_id}")
                    enrich_item = dict(item)
                    imdb_id = item.get('imdb_id') or ''
                    best_db = _best_data if _best_data is not None else (self._best_version(imdb_id, 'movie') if imdb_id else None)
                    if best_db:
                        for _f in ('filled_by_file', 'location_on_disk', 'location_basename'):
                            if best_db.get(_f):
                                enrich_item[_f] = best_db[_f]
                    return self._enrich_media_info(jf_info, enrich_item)

                self.logger.warning(
                    f"Jellyfin metadata incomplete for {ms_item_id} "
                    f"(resolution={jf_info.get('resolution')}, "
                    f"video_codec={jf_info.get('video_codec')}), "
                    f"falling back to DB data")

            except Exception as e:
                try:
                    import requests as _req
                    if isinstance(e, _req.exceptions.HTTPError) and \
                            e.response is not None and \
                            e.response.status_code in (400, 404):
                        raise  # Propagate — caller will reset the stale key
                except (ImportError, AttributeError):
                    pass
                self.logger.warning(
                    f"Could not fetch Jellyfin metadata for {ms_item_id}: {e}; "
                    f"using DB data instead")

        # Fall back to DB data
        db_info = self._build_media_info_from_db(item, _best_data=_best_data)
        self.logger.info(
            f"Using DB data for {item.get('title', media_item_id)}: "
            f"resolution={db_info.get('resolution')}, "
            f"audio={db_info.get('audio_codec')}")
        return db_info

    def _is_blank_image(self, img) -> bool:
        """Return True if image is mostly black/blank (no real poster content)."""
        try:
            import statistics
            rgb = img.convert('RGB')
            # Sample pixels at a grid to avoid loading the full image into memory
            w, h = rgb.size
            samples = []
            for x in range(0, w, max(1, w // 20)):
                for y in range(0, h, max(1, h // 20)):
                    r, g, b = rgb.getpixel((x, y))
                    samples.append((r + g + b) / 3)
            avg = statistics.mean(samples) if samples else 0
            return avg < 15  # Average brightness below 15/255 = essentially black
        except Exception:
            return False

    def _download_poster(self, plex_rating_key: str,
                         item: Dict[str, Any],
                         prefer_tmdb: bool = False) -> Optional[Any]:
        """
        Download poster image.

        Args:
            plex_rating_key: Plex rating key for the item
            item: Media item dict (must contain imdb_id/tmdb_id for TMDB fallback)
            prefer_tmdb: When True, try TMDB first.  Use this when fetching a
                         clean original for backup — Plex may already have an
                         overlaid version that we don't want as our base.
        """
        from io import BytesIO

        imdb_id = item.get('imdb_id')
        tmdb_id = item.get('tmdb_id')
        media_type = 'movie' if item.get('type') == 'movie' else 'tv'

        def _tmdb_image():
            try:
                from routes.poster_cache import get_cached_poster_url, cache_poster_url as _cpu
                import requests as _requests
                from PIL import Image as _Image

                from utilities.settings import load_config as _lc_tmdb_pre
                _textless_pre = _lc_tmdb_pre().get('Overlay Settings', {}).get('textless_posters', False)

                # In textless mode use a dedicated cache key so standard (non-textless)
                # URLs stored under imdb_id or tmdb_id don't collide with the textless URL.
                # _from_cache_textless tracks whether the returned URL came from the textless key.
                _cache_key = f"{tmdb_id}_textless" if _textless_pre else tmdb_id
                cached_url = None
                _from_cache_textless = False

                if _textless_pre:
                    # Only look up the textless-specific key — don't fall back to the imdb_id
                    # key which may hold a non-textless URL from a prior standard-mode run.
                    if tmdb_id:
                        cached_url = get_cached_poster_url(_cache_key, media_type)
                        if cached_url:
                            _from_cache_textless = True
                else:
                    cached_url = (
                        (get_cached_poster_url(imdb_id, media_type) if imdb_id else None)
                        or (get_cached_poster_url(tmdb_id, media_type) if tmdb_id else None)
                    )

                # _is_textless_poster is set when we do a fresh TMDB API call; it records
                # whether the chosen poster genuinely has no baked-in title (iso_639_1=null).
                _is_textless_poster = None

                # Cache miss — fetch directly from TMDB API.
                # When textless_posters is enabled: prefer iso_639_1=null (no baked-in title)
                # so the Title Logo badge can add the title cleanly.  Fall back to English if
                # no textless poster exists.
                # When textless_posters is disabled (default): prefer English-language posters
                # (iso_639_1=en) which include the title text — fall back to en+null.
                if not cached_url and tmdb_id:
                    from utilities.settings import get_setting as _gs_tmdb
                    _api_key = _gs_tmdb('TMDB', 'api_key', default='')
                    _textless = _textless_pre
                    if _api_key:
                        _posters = []
                        if _textless:
                            # Textless mode: null-language first (no text), fall back to English
                            for _lang in ('null', 'en', 'en,null'):
                                _api_resp = _requests.get(
                                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images"
                                    f"?api_key={_api_key}&include_image_language={_lang}",
                                    timeout=15
                                )
                                if _api_resp.status_code == 200:
                                    _posters = _api_resp.json().get('posters', [])
                                    if _posters:
                                        break
                        else:
                            # Standard mode: English first, fall back to en+null
                            for _lang in ('en', 'en,null'):
                                _api_resp = _requests.get(
                                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images"
                                    f"?api_key={_api_key}&include_image_language={_lang}",
                                    timeout=15
                                )
                                if _api_resp.status_code == 200:
                                    _posters = _api_resp.json().get('posters', [])
                                    if _posters:
                                        break  # English-only results found — skip null fallback
                        if _posters:
                            if _textless:
                                # In textless mode filter out unvoted foreign posters
                                # (vote_count=0/1 are typically obscure regional uploads).
                                # Sort by vote_count * vote_average so popular clean posters
                                # rank above niche foreign ones with no community engagement.
                                _voted = [p for p in _posters if p.get('vote_count', 0) >= 3]
                                _candidates = _voted if _voted else _posters
                                _candidates.sort(
                                    key=lambda p: p.get('vote_count', 0) * p.get('vote_average', 0),
                                    reverse=True)
                            else:
                                _candidates = sorted(
                                    _posters,
                                    key=lambda p: p.get('vote_average', 0), reverse=True)
                            _chosen = _candidates[0]
                            cached_url = (
                                f"https://image.tmdb.org/t/p/w300"
                                f"{_chosen['file_path']}"
                            )
                            # iso_639_1=null means no baked-in title text
                            _is_textless_poster = (_chosen.get('iso_639_1') is None)
                            # Use a separate cache key for textless posters so the
                            # standard cache (used by library/stats pages) is not polluted
                            _cpu(_cache_key, media_type, cached_url)
                            self.logger.info(
                                f"Fetched TMDB poster for "
                                f"{item.get('title', plex_rating_key)} "
                                f"(textless={_is_textless_poster})")

                if not cached_url or 'image.tmdb.org' not in cached_url:
                    return None
                if '/t/p/' in cached_url:
                    path_part = cached_url.split('/t/p/', 1)[1]
                    slug = path_part.split('/', 1)[-1] if '/' in path_part else path_part
                    tmdb_img_url = f"https://image.tmdb.org/t/p/w780/{slug}"
                else:
                    tmdb_img_url = cached_url
                resp = _requests.get(tmdb_img_url, timeout=15)
                resp.raise_for_status()
                img = _Image.open(BytesIO(resp.content))
                # Determine the textless flag for this image:
                # - Fresh API call: use the actual iso_639_1 result (_is_textless_poster)
                # - Cache hit via textless key: the URL was originally chosen as the best
                #   null-language poster, so treat it as textless
                # - Cache hit via standard key (non-textless mode): not textless
                if _is_textless_poster is not None:
                    img._cli_textless = _is_textless_poster
                elif _from_cache_textless:
                    img._cli_textless = True
                else:
                    img._cli_textless = False
                self.logger.info(
                    f"Using TMDB poster for {item.get('title', plex_rating_key)} "
                    f"(textless={img._cli_textless}, {tmdb_img_url})")
                return img
            except Exception as e:
                self.logger.warning(f"TMDB poster download failed for {plex_rating_key}: {e}")
                return None

        def _server_image():
            try:
                server_img = self.client.download_poster(plex_rating_key)
                if server_img and not self._is_blank_image(server_img):
                    self.logger.info(f"Using media server poster for {plex_rating_key}")
                    return server_img
                if server_img:
                    self.logger.warning(
                        f"Media server poster for {plex_rating_key} appears blank/black")
            except Exception as e:
                self.logger.warning(f"Media server poster download failed for {plex_rating_key}: {e}")
            return None

        if prefer_tmdb:
            return _tmdb_image() or _server_image()
        else:
            return _server_image() or _tmdb_image()

    def _fetch_clearlogo_url(self, tmdb_id: Optional[str], item_type: str) -> Optional[str]:
        """Fetch the best English PNG clearlogo URL from TMDB for the given item.

        Results are cached in poster_cache.pkl under key '{tmdb_id}_clearlogo_{mtype}'
        for 7 days (same TTL as poster URLs).  A sentinel value of '' is cached when
        TMDB has no logo so we don't re-query on every run.

        Returns a full https://image.tmdb.org URL or None if unavailable.
        """
        if not tmdb_id:
            return None
        try:
            from routes.poster_cache import CACHE_EXPIRY_DAYS as _ttl_days
            from datetime import datetime as _dt, timedelta as _td
            import routes.poster_cache as _pcmod
            _pcmod._ensure_loaded()

            _mtype = 'tv' if item_type in ('episode', 'tv', 'show') else 'movie'
            _cache_key = f"{tmdb_id}_clearlogo_{_mtype}"

            # --- cache read ---
            # We store (url_or_sentinel, timestamp) tuples directly so we can distinguish
            # '' (cached no-logo) from None (cache miss).
            # Access internals via module reference so the live dict/lock are always used.
            with _pcmod._cache_lock:
                _cached_item = _pcmod._cache.get(_cache_key)
            if _cached_item is not None:
                _cached_url, _cached_ts = _cached_item
                if _dt.now() - _cached_ts < _td(days=_ttl_days):
                    # '' means TMDB confirmed no logo for this item
                    return _cached_url if _cached_url else None

            # --- cache miss: fetch from TMDB ---
            from utilities.settings import get_setting as _gs
            import requests as _req
            _api_key = _gs('TMDB', 'api_key', default='')
            if not _api_key:
                return None

            _result_url = None
            for _lang in ('en', 'en,null'):
                _resp = _req.get(
                    f"https://api.themoviedb.org/3/{_mtype}/{tmdb_id}/images"
                    f"?api_key={_api_key}&include_image_language={_lang}",
                    timeout=10
                )
                if _resp.status_code != 200:
                    break
                _logos = _resp.json().get('logos', [])
                # Prefer PNG (transparent) logos, sort by vote_average
                _png_logos = [l for l in _logos if (l.get('file_path') or '').endswith('.png')]
                _candidates = _png_logos or _logos
                if _candidates:
                    _candidates.sort(key=lambda l: l.get('vote_average', 0), reverse=True)
                    _path = _candidates[0]['file_path']
                    _result_url = f"https://image.tmdb.org/t/p/original{_path}"
                    break

            # Cache the result (url string) or '' sentinel if no logo found.
            # This prevents re-querying TMDB for items without a clearlogo on every run.
            _store_val = _result_url if _result_url else ''
            with _pcmod._cache_lock:
                _pcmod._cache[_cache_key] = (_store_val, _dt.now())
                _pcmod._cache_dirty = True

            return _result_url

        except Exception as _e:
            self.logger.debug(f"Clearlogo fetch failed for tmdb_id={tmdb_id}: {_e}")
        return None

    def _get_overlay_state(self, media_item_id: int) -> Optional[Dict[str, Any]]:
        """Get overlay state from database."""
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT * FROM media_overlay_state
                WHERE media_item_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
            ''', (media_item_id,))

            row = cursor.fetchone()
            conn.close()

            if row:
                return dict(row)
            return None

        except Exception as e:
            self.logger.error(f"Database error fetching overlay state: {e}")
            return None

    def _update_overlay_state(self, media_item_id: int, status: str, reason: str = None,
                             poster_hash: str = None, metadata_hash: str = None,
                             layout_hash: str = None, content_hash: str = None,
                             plex_upload_hash: str = None, textless_poster_used: bool = None,
                             plex_thumb_url: str = None):
        """Update or insert overlay state in database (single ON CONFLICT upsert)."""
        import time as _time
        last_exc = None
        _textless_int = (1 if textless_poster_used else 0) if textless_poster_used is not None else None
        for _attempt in range(5):
            try:
                conn = _get_db_connection()
                conn.execute('''
                    INSERT INTO media_overlay_state
                        (media_item_id, status, reason,
                         last_poster_hash, last_metadata_hash, last_layout_hash, last_content_hash,
                         last_plex_upload_hash, textless_poster_used, plex_thumb_url,
                         overlay_applied_at, last_retry, retry_count,
                         created_at, updated_at)
                    VALUES (?, ?, ?,
                            ?, ?, ?, ?,
                            ?, ?, ?,
                            CASE WHEN ? = 'applied' THEN CURRENT_TIMESTAMP ELSE NULL END,
                            CASE WHEN ? = 'failed'  THEN CURRENT_TIMESTAMP ELSE NULL END,
                            CASE WHEN ? IN ('failed', 'analyzing') THEN 1 ELSE 0 END,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT(media_item_id) DO UPDATE SET
                        status                = excluded.status,
                        reason                = excluded.reason,
                        last_poster_hash      = COALESCE(excluded.last_poster_hash,      last_poster_hash),
                        last_metadata_hash    = COALESCE(excluded.last_metadata_hash,    last_metadata_hash),
                        last_layout_hash      = COALESCE(excluded.last_layout_hash,      last_layout_hash),
                        last_content_hash     = COALESCE(excluded.last_content_hash,     last_content_hash),
                        last_plex_upload_hash = COALESCE(excluded.last_plex_upload_hash, last_plex_upload_hash),
                        textless_poster_used  = COALESCE(excluded.textless_poster_used,  textless_poster_used),
                        plex_thumb_url        = COALESCE(excluded.plex_thumb_url,        plex_thumb_url),
                        overlay_applied_at = CASE WHEN excluded.status = 'applied'
                                                  THEN CURRENT_TIMESTAMP
                                                  ELSE overlay_applied_at END,
                        last_retry         = CASE WHEN excluded.status = 'failed'
                                                  THEN CURRENT_TIMESTAMP
                                                  ELSE last_retry END,
                        retry_count        = CASE WHEN excluded.status IN ('failed', 'analyzing')
                                                  THEN retry_count + 1
                                                  ELSE retry_count END,
                        updated_at         = CURRENT_TIMESTAMP
                ''', (media_item_id, status, reason,
                      poster_hash, metadata_hash, layout_hash, content_hash,
                      plex_upload_hash, _textless_int, plex_thumb_url,
                      status, status, status))
                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                conn_ref = locals().get('conn')
                if conn_ref:
                    try: conn_ref.close()
                    except Exception: pass
                if 'database is locked' in str(e) and _attempt < 4:
                    last_exc = e
                    _time.sleep(2 ** _attempt)
                    continue
                self.logger.error(f"Database error updating overlay state: {e}")
                return
            except Exception as e:
                self.logger.error(f"Database error updating overlay state: {e}")
                return
        self.logger.error(f"Database error updating overlay state after retries: {last_exc}")

    def _update_media_item_info(self, media_item_id: int, media_info: Dict[str, Any],
                               plex_rating_key: str):
        """Update media_items table with media server info."""
        import time as _time
        last_exc = None
        for _attempt in range(5):
            try:
                conn = _get_db_connection()
                cursor = conn.cursor()

                cursor.execute('''
                    UPDATE media_items
                    SET ms_item_id = ?,
                        ms_resolution = ?,
                        ms_hdr = ?,
                        ms_dolby_vision = ?,
                        ms_hdr_format = ?,
                        ms_audio_codec = ?,
                        ms_audio_channels = ?,
                        ms_audio_track = ?,
                        ms_subtitle_track = ?,
                        ms_video_codec = ?,
                        ms_media_container = ?,
                        ms_media_bitrate = ?,
                        ms_network = ?,
                        ms_studio = ?,
                        ms_content_rating = ?,
                        ms_last_scanned = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (
                    plex_rating_key,
                    media_info.get('resolution'),
                    1 if media_info.get('hdr') else 0,
                    1 if media_info.get('dolby_vision') else 0,
                    media_info.get('hdr_subtype'),
                    media_info.get('audio_codec'),
                    media_info.get('audio_channels'),
                    media_info.get('audio_track'),
                    media_info.get('subtitle_track'),
                    media_info.get('video_codec'),
                    media_info.get('container'),
                    media_info.get('bitrate'),
                    media_info.get('network'),
                    media_info.get('studio'),
                    media_info.get('content_rating'),
                    media_item_id
                ))

                conn.commit()
                conn.close()
                return
            except sqlite3.OperationalError as e:
                conn_ref = locals().get('conn')
                if conn_ref:
                    try: conn_ref.close()
                    except Exception: pass
                if 'database is locked' in str(e) and _attempt < 4:
                    last_exc = e
                    _time.sleep(2 ** _attempt)
                    continue
                self.logger.error(f"Database error updating media item info: {e}")
                return
            except Exception as e:
                self.logger.error(f"Database error updating media item info: {e}")
                return
        self.logger.error(f"Database error updating media item info after retries: {last_exc}")

    def _calculate_image_hash(self, image) -> str:
        """Calculate hash of image for change detection."""
        try:
            from io import BytesIO
            buffer = BytesIO()
            image.save(buffer, format='PNG')
            image_bytes = buffer.getvalue()
            return hashlib.md5(image_bytes).hexdigest()
        except Exception as e:
            self.logger.error(f"Failed to calculate image hash: {e}")
            return ''

    @staticmethod
    def _compute_layout_hash(layout: Dict[str, Any]) -> str:
        """
        Hash that changes whenever the layout definition is updated.

        Uses the layout's updated_at timestamp (fast, no JSON serialisation),
        falling back to an MD5 of layout_json for safety.
        """
        updated_at = layout.get('updated_at') or ''
        if updated_at:
            return hashlib.md5(str(updated_at).encode()).hexdigest()
        layout_json = layout.get('layout_json') or ''
        return hashlib.md5(layout_json.encode()).hexdigest()

    @staticmethod
    def _compute_quality_hash(resolution, hdr, dolby_vision,
                              audio_codec, audio_channels, video_codec=None) -> str:
        """
        Stable hash of quality-relevant badge fields only.

        Intentionally excludes ratings, format strings and other metadata that
        change frequently but don't affect which badge is rendered.  This hash
        is stored as last_metadata_hash and compared before applying/skipping
        an overlay so that quality upgrades automatically trigger a re-apply.
        """
        key = json.dumps({
            'r':  (resolution    or '').lower().strip(),
            'h':  bool(hdr),
            'dv': bool(dolby_vision),
            'a':  (audio_codec   or '').lower().strip(),
            'ac': (audio_channels or '').lower().strip(),
            'vc': (video_codec   or '').lower().strip(),
        }, sort_keys=True)
        return hashlib.md5(key.encode()).hexdigest()

    @staticmethod
    def _compute_content_hash(imdb_rating=None, tmdb_rating=None, trakt_rating=None,
                               rt_critics_score=None, rt_user_score=None,
                               status=None, version_count=None) -> str:
        """
        Stable hash of content-metadata badge fields (ratings, show status, version count).

        Stored as last_content_hash alongside last_metadata_hash.  When this hash
        changes between sync runs, the overlay is reset to 'pending' so the updated
        ratings / status / version count are reflected in the rendered poster.

        Intentionally separate from quality hash so the two can be checked at
        different frequencies (quality: every sync; content: configurable interval).
        """
        key = json.dumps({
            'ir':  round(float(imdb_rating), 1)    if imdb_rating    is not None else None,
            'tr':  round(float(tmdb_rating), 1)    if tmdb_rating    is not None else None,
            'kr':  round(float(trakt_rating), 1)   if trakt_rating   is not None else None,
            'rt':  int(rt_critics_score)            if rt_critics_score is not None else None,
            'rtu': int(rt_user_score)               if rt_user_score  is not None else None,
            'st':  (status or '').lower().strip(),
            'vc':  int(version_count)               if version_count  is not None else None,
        }, sort_keys=True)
        return hashlib.md5(key.encode()).hexdigest()

    def _quality_hash_for_item(self, item: Dict[str, Any],
                               _best_data: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """
        Compute quality hash from the best available DB data for this item.

        For TV shows: uses the best episode across the whole show.
        For movies:   uses the best version (same imdb_id).
        Returns None if no quality data is available yet.

        Pass _best_data if already fetched to avoid a redundant DB query.
        """
        item_type = item.get('type')

        data = _best_data
        if data is None:
            if item_type == 'episode':
                data = self._best_version(item.get('imdb_id') or '', 'episode')
            elif item_type == 'movie':
                imdb_id = item.get('imdb_id')
                if imdb_id:
                    data = self._best_version(imdb_id, 'movie')
                if not data:
                    data = item  # single-version movie, use its own columns
        elif item_type == 'movie' and not data:
            data = item  # single-version movie fallback

        if not data or not data.get('ms_resolution'):
            return None

        return self._compute_quality_hash(
            data.get('ms_resolution'),
            data.get('ms_hdr'),
            data.get('ms_dolby_vision'),
            data.get('ms_audio_codec'),
            data.get('ms_audio_channels'),
            data.get('ms_video_codec'),
        )

    # ─────────────────────────────────────────────────────────────────────
    # Season overlay methods
    # ─────────────────────────────────────────────────────────────────────

    def _best_episode_for_season(self, show_imdb_id: str,
                                 season_number: int) -> Optional[Dict[str, Any]]:
        """
        Return the highest-quality episode's media columns for a specific season.

        Queries by imdb_id + season_number (not plex_rating_key, which stores each
        episode's own key, not the parent show key).
        """
        if not show_imdb_id:
            return None
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT ms_resolution, ms_hdr, ms_dolby_vision, ms_hdr_format,
                       ms_audio_codec, ms_audio_channels,
                       ms_video_codec, ms_media_container, ms_media_bitrate,
                       ms_network, ms_studio, ms_content_rating,
                       filled_by_file, location_on_disk, location_basename,
                       imdb_id
                FROM media_items
                WHERE imdb_id = ?
                  AND season_number = ?
                  AND type = 'episode'
                  AND state IN ('Collected', 'Upgrading')
                  AND ms_resolution IS NOT NULL
                ORDER BY
                    ({self._RES_RANK}) DESC,
                    ({self._HDR_RANK}) DESC,
                    ({self._AUD_RANK}) DESC,
                    CAST(COALESCE(ms_audio_channels, '0') AS REAL) DESC
                LIMIT 1
            ''', (show_imdb_id, season_number))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
        except Exception as e:
            self.logger.warning(
                f"Best-episode-for-season query failed for show imdb={show_imdb_id} "
                f"season {season_number}: {e}")
        return None

    def _get_season_overlay_state(self, season_ms_item_id: str) -> Optional[Dict[str, Any]]:
        """Get season overlay state from database."""
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM season_overlay_state WHERE season_ms_item_id = ?',
                (season_ms_item_id,)
            )
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            self.logger.error(f"DB error fetching season overlay state: {e}")
            return None

    def _update_season_overlay_state(self, show_ms_item_id: str,
                                     season_ms_item_id: str,
                                     season_number: int,
                                     status: str,
                                     reason: str = None,
                                     metadata_hash: str = None,
                                     layout_hash: str = None,
                                     content_hash: str = None,
                                     plex_upload_hash: str = None):
        """Upsert season overlay state in database."""
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO season_overlay_state
                    (show_ms_item_id, season_ms_item_id, season_number,
                     status, reason, last_metadata_hash, last_layout_hash, last_content_hash,
                     last_plex_upload_hash,
                     overlay_applied_at, last_retry, retry_count,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                        ?,
                        CASE WHEN ? = 'applied' THEN CURRENT_TIMESTAMP ELSE NULL END,
                        CASE WHEN ? = 'failed'  THEN CURRENT_TIMESTAMP ELSE NULL END,
                        CASE WHEN ? IN ('failed') THEN 1 ELSE 0 END,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(season_ms_item_id) DO UPDATE SET
                    status                = excluded.status,
                    reason                = excluded.reason,
                    last_metadata_hash    = COALESCE(excluded.last_metadata_hash,    last_metadata_hash),
                    last_layout_hash      = COALESCE(excluded.last_layout_hash,      last_layout_hash),
                    last_content_hash     = COALESCE(excluded.last_content_hash,     last_content_hash),
                    last_plex_upload_hash = COALESCE(excluded.last_plex_upload_hash, last_plex_upload_hash),
                    overlay_applied_at = CASE WHEN excluded.status = 'applied'
                                             THEN CURRENT_TIMESTAMP
                                             ELSE overlay_applied_at END,
                    last_retry         = CASE WHEN excluded.status = 'failed'
                                             THEN CURRENT_TIMESTAMP
                                             ELSE last_retry END,
                    retry_count        = CASE WHEN excluded.status = 'failed'
                                             THEN retry_count + 1
                                             ELSE retry_count END,
                    updated_at         = CURRENT_TIMESTAMP
            ''', (show_ms_item_id, season_ms_item_id, season_number,
                  status, reason, metadata_hash, layout_hash, content_hash,
                  plex_upload_hash,
                  status, status, status))
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.error(f"DB error updating season overlay state: {e}")

    def generate_season_overlay(self, show_plex_rating_key: str,
                                season_plex_rating_key: str,
                                season_number: int,
                                force: bool = False,
                                layout_id: Optional[int] = None,
                                force_fresh_poster: bool = False) -> Dict[str, Any]:
        """
        Generate and upload an overlay poster for a single TV show season.

        Quality metadata is drawn from the best episode in that specific season.
        The season poster is downloaded from the media server (no TMDB fallback — TMDB does
        not expose per-season posters with our existing integration).

        Args:
            show_plex_rating_key:   Media server item ID of the parent show
            season_plex_rating_key: Media server item ID of the season
            season_number:          Season index (0 = Specials)
            force:                  Force re-generation even if 'applied'
            layout_id:              Explicit layout; None → use default 'season' layout
            force_fresh_poster:      Delete backup and re-download from media server

        Returns:
            Result dict with 'success', 'status', 'message' keys.
        """
        result = {
            'success': False,
            'season_ms_item_id': season_plex_rating_key,
            'season_number': season_number,
            'status': None,
            'message': None,
        }

        try:
            from overlays.cache_cleanup import PosterCacheManager
            from PIL import Image as _PILImage
            from io import BytesIO as _BytesIO

            # ── Resolve template early for layout-hash skip check ─────────
            _season_early_template = None
            if layout_id:
                _season_early_template = self.layout_mgr.get_layout(layout_id)
            else:
                _season_early_layouts = self.layout_mgr.list_layouts(
                    media_type='season', active_only=True)
                if _season_early_layouts:
                    _season_early_template = _season_early_layouts[0]

            # ── Resolve show imdb_id from DB ──────────────────────────────
            # The media server does NOT always expose IMDb/TMDB IDs on metadata.
            # Our DB (media_items) stores the show's imdb_id on every episode row.
            # Strategy 1: look up by ms_item_id (guaranteed correct, no title collisions).
            # Strategy 2: fall back to title lookup if ms_item_id yields nothing.
            show_imdb_id = None
            show_tmdb_id = None
            _show_title_for_db = None
            _show_meta_early = None
            try:
                _show_meta_early = self.client.get_media_metadata(show_plex_rating_key)
                _show_title_for_db = _show_meta_early.get('title') or _show_meta_early.get('Name') or ''
            except Exception as _st_exc:
                self.logger.debug(f"Could not fetch show title for {show_plex_rating_key}: {_st_exc}")

            try:
                conn_tmp = _get_db_connection()
                # Primary: ms_item_id lookup — immune to title collisions
                row_tmp = conn_tmp.execute(
                    """SELECT imdb_id, tmdb_id FROM media_items
                       WHERE ms_item_id = ?
                         AND type = 'episode'
                         AND imdb_id IS NOT NULL AND imdb_id != ''
                       LIMIT 1""",
                    (show_plex_rating_key,)
                ).fetchone()
                if row_tmp and row_tmp['imdb_id']:
                    show_imdb_id = row_tmp['imdb_id']
                    show_tmdb_id = row_tmp['tmdb_id']
                # Fallback: title lookup if ms_item_id found nothing
                elif _show_title_for_db:
                    row_tmp = conn_tmp.execute(
                        """SELECT imdb_id, tmdb_id FROM media_items
                           WHERE title = ?
                             AND type = 'episode'
                             AND imdb_id IS NOT NULL AND imdb_id != ''
                           LIMIT 1""",
                        (_show_title_for_db,)
                    ).fetchone()
                    if row_tmp and row_tmp['imdb_id']:
                        show_imdb_id = row_tmp['imdb_id']
                        show_tmdb_id = row_tmp['tmdb_id']
                conn_tmp.close()
            except Exception as _db_exc:
                self.logger.debug(f"DB imdb_id lookup for show {show_plex_rating_key} ('{_show_title_for_db}') failed: {_db_exc}")

            self.logger.info(f"Season {season_number} show='{_show_title_for_db}' imdb_id={show_imdb_id} tmdb_id={show_tmdb_id}")

            # ── Skip-check ────────────────────────────────────────────────
            if not force:
                state = self._get_season_overlay_state(season_plex_rating_key)
                if state and state.get('status') == 'applied':
                    # Check layout hash first
                    if _season_early_template:
                        current_layout_hash = self._compute_layout_hash(_season_early_template)
                        stored_layout_hash = state.get('last_layout_hash')
                        if stored_layout_hash and stored_layout_hash != current_layout_hash:
                            self.logger.info(
                                f"Season layout updated for key={season_plex_rating_key} "
                                f"— re-applying overlay")
                            # Fall through to re-apply
                        else:
                            # Layout unchanged — check quality hash
                            stored_hash = state.get('last_metadata_hash')
                            if stored_hash:
                                best = self._best_episode_for_season(
                                    show_imdb_id, season_number)
                                if best and best.get('ms_resolution'):
                                    current_hash = self._compute_quality_hash(
                                        best.get('ms_resolution'),
                                        best.get('ms_hdr'),
                                        best.get('ms_dolby_vision'),
                                        best.get('ms_audio_codec'),
                                        best.get('ms_audio_channels'),
                                        best.get('ms_video_codec'),
                                    )
                                    if current_hash == stored_hash:
                                        result['status'] = 'skipped'
                                        result['message'] = 'Season overlay already applied, quality unchanged'
                                        result['success'] = True
                                        return result
                                    self.logger.info(
                                        f"Season quality changed for key={season_plex_rating_key} "
                                        f"({stored_hash[:8]}→{current_hash[:8]}) — re-applying")
                                else:
                                    result['status'] = 'skipped'
                                    result['message'] = 'Season overlay already applied (use force=True to regenerate)'
                                    result['success'] = True
                                    return result
                            else:
                                result['status'] = 'skipped'
                                result['message'] = 'Season overlay already applied (use force=True to regenerate)'
                                result['success'] = True
                                return result
                    else:
                        # No active season layout — check quality hash only
                        stored_hash = state.get('last_metadata_hash')
                        if stored_hash:
                            best = self._best_episode_for_season(
                                show_imdb_id, season_number)
                            if best and best.get('ms_resolution'):
                                current_hash = self._compute_quality_hash(
                                    best.get('ms_resolution'),
                                    best.get('ms_hdr'),
                                    best.get('ms_dolby_vision'),
                                    best.get('ms_audio_codec'),
                                    best.get('ms_audio_channels'),
                                    best.get('ms_video_codec'),
                                )
                                if current_hash == stored_hash:
                                    result['status'] = 'skipped'
                                    result['message'] = 'Season overlay already applied, quality unchanged'
                                    result['success'] = True
                                    return result
                                self.logger.info(
                                    f"Season quality changed for key={season_plex_rating_key} "
                                    f"({stored_hash[:8]}→{current_hash[:8]}) — re-applying")
                            else:
                                result['status'] = 'skipped'
                                result['message'] = 'Season overlay already applied (use force=True to regenerate)'
                                result['success'] = True
                                return result
                        else:
                            result['status'] = 'skipped'
                            result['message'] = 'Season overlay already applied (use force=True to regenerate)'
                            result['success'] = True
                            return result

            # ── Build media_info — mirrors the show overlay path exactly ─────
            # 1. Get best episode quality for this season from DB
            # 2. Build ep_info from Plex API (falls back to DB if unavailable)
            # 3. Fetch show-level metadata from Plex for network/studio/content_rating
            # 4. Build enrich_item with type='episode' + imdb_id + filled_by_file
            # 5. Call _enrich_media_info for ratings, format, status, network etc.
            best_ep = self._best_episode_for_season(show_imdb_id, season_number) if show_imdb_id else None

            # Build base ep_info from media server API (best episode in this season),
            # falling back to DB if unavailable or incomplete.
            ep_info = None
            try:
                ep_metadata = self.client.get_season_best_episode_media(season_plex_rating_key)
                if ep_metadata:
                    if self._jellyfin_mode:
                        ep_info = self.extractor.extract_from_jellyfin_metadata(ep_metadata)
                    else:
                        ep_info = self.extractor.extract_from_plex_metadata(ep_metadata)
                    if not self.extractor.is_complete(ep_info):
                        ep_info = None  # incomplete — use DB below
            except Exception as _ep_exc:
                self.logger.debug(f"Season media server API fetch failed: {_ep_exc}")

            if ep_info is None and best_ep:
                # Build ep_info dict from DB columns, same shape as extractor output
                import re as _re
                _res = best_ep.get('ms_resolution') or ''
                _res = _re.sub(r'^(\d{3,4})$', r'\1p', str(_res))
                ep_info = {
                    'resolution':    _res or None,
                    'hdr':           bool(best_ep.get('ms_hdr')),
                    'dolby_vision':  bool(best_ep.get('ms_dolby_vision')),
                    'audio_codec':   (best_ep.get('ms_audio_codec') or '').strip() or None,
                    'audio_channels':(best_ep.get('ms_audio_channels') or '').strip() or None,
                    'video_codec':   (best_ep.get('ms_video_codec') or '').strip() or None,
                    'container':     best_ep.get('ms_media_container'),
                    'bitrate':       best_ep.get('ms_media_bitrate'),
                }

            # Fetch show-level metadata for network/studio/content_rating.
            # Reuse _show_meta_early already fetched above for imdb_id resolution.
            # Always fetch so we can pass it as _plex_metadata for the ratings fallback.
            show_metadata = None
            try:
                show_metadata = _show_meta_early if _show_meta_early else self.client.get_media_metadata(show_plex_rating_key)
                if ep_info and not ep_info.get('network') and not ep_info.get('studio'):
                    ep_info['network'] = (show_metadata.get('network') or
                                          show_metadata.get('Network') or '').strip() or None
                    ep_info['studio']  = (show_metadata.get('studio') or
                                          show_metadata.get('Studio') or '').strip() or None
                    if not ep_info.get('content_rating'):
                        # Jellyfin uses 'OfficialRating'; Plex uses 'contentRating'
                        _cr_raw = (show_metadata.get('OfficialRating') or
                                   show_metadata.get('contentRating') or
                                   show_metadata.get('content_rating') or '').strip()
                        ep_info['content_rating'] = self._normalize_content_rating(_cr_raw) if _cr_raw else None
            except Exception as _smeta_exc:
                self.logger.debug(f"Could not fetch show metadata for {show_plex_rating_key}: {_smeta_exc}")

            # Build enrich_item the same way as show path: start from best_ep
            # (has filled_by_file for format extraction) with type='episode' and imdb_id.
            enrich_item = dict(best_ep) if best_ep else {}
            enrich_item['type']         = 'episode'
            enrich_item['imdb_id']      = show_imdb_id or enrich_item.get('imdb_id') or ''
            # Carry season_number so _enrich_media_info scopes version_count to this season only.
            enrich_item['season_number'] = season_number
            # Carry network/studio/content_rating into enrich_item so _enrich_media_info
            # DB fallback uses them if Plex returned them above.
            if ep_info:
                for _f in ('network', 'studio', 'content_rating'):
                    if ep_info.get(_f):
                        enrich_item[_f] = ep_info[_f]

            # Pass show-level Plex metadata for ratings fallback (zero extra API call)
            if show_metadata:
                enrich_item['_plex_metadata'] = show_metadata

            # If best_ep had no filled_by_file (e.g. plex_resolution was NULL so
            # _best_episode_for_season returned None), do a separate DB lookup
            # for filled_by_file so format can be extracted from the filename.
            if not enrich_item.get('filled_by_file') and show_imdb_id:
                try:
                    conn_tmp = _get_db_connection()
                    row_tmp = conn_tmp.execute(
                        """SELECT filled_by_file, location_on_disk, location_basename
                           FROM media_items
                           WHERE imdb_id = ?
                             AND season_number = ?
                             AND type = 'episode'
                             AND filled_by_file IS NOT NULL
                             AND filled_by_file != ''
                             AND state IN ('Collected', 'Upgrading')
                           LIMIT 1""",
                        (show_imdb_id, season_number)
                    ).fetchone()
                    conn_tmp.close()
                    if row_tmp:
                        for _f in ('filled_by_file', 'location_on_disk', 'location_basename'):
                            if row_tmp[_f]:
                                enrich_item[_f] = row_tmp[_f]
                except Exception:
                    pass

            media_info = self._enrich_media_info(ep_info or {}, enrich_item)

            self.logger.info(
                f"Season {season_number} media_info: "
                f"res={media_info.get('resolution')} "
                f"format={media_info.get('format')} "
                f"network={media_info.get('network')} "
                f"status={media_info.get('status')} "
                f"imdb={media_info.get('imdb_rating')} "
                f"tmdb={media_info.get('tmdb_rating')} "
                f"trakt={media_info.get('trakt_rating')} "
                f"hdr={media_info.get('hdr')} dv={media_info.get('dolby_vision')}")

            # ── Poster backup / download ──────────────────────────────────
            backup_mgr = PosterCacheManager(None)
            backup_file = backup_mgr.backup_dir / f"season_{season_plex_rating_key}_original.jpg"

            if force_fresh_poster and backup_file.exists():
                backup_file.unlink()
                self.logger.info(
                    f"Deleted season backup for {season_plex_rating_key} "
                    f"to force re-download from Plex")

            poster_image = None
            if backup_file.exists():
                state = self._get_season_overlay_state(season_plex_rating_key)
                current_status = state.get('status') if state else None

                if current_status in ('removed', 'user_removed'):
                    # Overlay was explicitly removed — user may have picked a new poster.
                    # Check whether the media server season poster differs from our backup.
                    try:
                        server_poster = self.client.download_poster(season_plex_rating_key)
                        if server_poster and not self._is_blank_image(server_poster):
                            with open(str(backup_file), 'rb') as _f:
                                backup_img = _PILImage.open(_BytesIO(_f.read()))
                                backup_img.load()
                            if (self._calculate_image_hash(server_poster) !=
                                    self._calculate_image_hash(backup_img)):
                                self.logger.info(
                                    f"Season poster changed in media server for {season_plex_rating_key}; "
                                    f"updating backup")
                                backup_mgr.backup_season_poster(
                                    season_plex_rating_key,
                                    self.renderer.image_to_bytes(server_poster))
                                poster_image = server_poster
                    except Exception as _chk_err:
                        self.logger.warning(
                            f"Could not check media server season poster change for "
                            f"{season_plex_rating_key}: {_chk_err}")

                if poster_image is None:
                    with open(str(backup_file), 'rb') as _f:
                        poster_image = _PILImage.open(_BytesIO(_f.read()))
                        poster_image.load()
                    self.logger.info(
                        f"Using backed-up season poster for {season_plex_rating_key}")
            else:
                # No backup — try TVDB (if key configured) → TMDB → media server.
                poster_image = None
                from utilities.settings import get_setting as _get_setting

                # TVDB season poster — primary source when API key is present
                if show_imdb_id:
                    try:
                        from cli_battery.app.tvdb_client import get_season_poster_url as _tvdb_season_poster
                        from cli_battery.app.tvdb_client import is_available as _tvdb_available
                        if _tvdb_available():
                            _tvdb_url = _tvdb_season_poster(show_imdb_id, season_number)
                            if _tvdb_url:
                                import requests as _tvdb_rq
                                from PIL import Image as _Image
                                from io import BytesIO as _BytesIO
                                _tvdb_resp = _tvdb_rq.get(_tvdb_url, timeout=15)
                                _tvdb_resp.raise_for_status()
                                poster_image = _Image.open(_BytesIO(_tvdb_resp.content))
                                poster_image.load()
                                self.logger.info(
                                    f"Using TVDB English season poster for {season_plex_rating_key} "
                                    f"(season {season_number}, imdb={show_imdb_id})")
                    except Exception as _tvdb_err:
                        self.logger.debug(f"TVDB season poster fetch failed: {_tvdb_err}")
                # TMDB season poster — fallback when TVDB unavailable or no English poster found
                _resolved_tmdb_id = show_tmdb_id if not poster_image else None
                # If tmdb_id is missing from DB but we have imdb_id, resolve via TMDB find API.
                if not poster_image and not _resolved_tmdb_id and show_imdb_id:
                    try:
                        import requests as _rq_find
                        _find_api_key = _get_setting('TMDB', 'api_key', default='')
                        if _find_api_key:
                            _find_resp = _rq_find.get(
                                f"https://api.themoviedb.org/3/find/{show_imdb_id}"
                                f"?api_key={_find_api_key}&external_source=imdb_id",
                                timeout=10
                            )
                            if _find_resp.status_code == 200:
                                _tv_results = _find_resp.json().get('tv_results', [])
                                if _tv_results:
                                    _resolved_tmdb_id = _tv_results[0].get('id')
                                    self.logger.info(
                                        f"Resolved TMDB ID {_resolved_tmdb_id} from IMDB "
                                        f"{show_imdb_id} for season poster lookup")
                    except Exception as _find_err:
                        self.logger.debug(f"TMDB find lookup failed for {show_imdb_id}: {_find_err}")

                if not poster_image and _resolved_tmdb_id:
                    try:
                        import requests as _requests
                        from PIL import Image as _Image
                        from io import BytesIO as _BytesIO2
                        _tmdb_api_key = _get_setting('TMDB', 'api_key', default='')
                        if _tmdb_api_key:
                            _season_images_url = (
                                f"https://api.themoviedb.org/3/tv/{_resolved_tmdb_id}"
                                f"/season/{season_number}/images"
                                f"?api_key={_tmdb_api_key}&include_image_language=en,null"
                            )
                            _sresp = _requests.get(_season_images_url, timeout=15)
                            _sresp.raise_for_status()
                            _sposters = _sresp.json().get('posters', [])
                            if _sposters:
                                _sposters.sort(key=lambda p: p.get('vote_average', 0), reverse=True)
                                _simg_url = f"https://image.tmdb.org/t/p/w500{_sposters[0]['file_path']}"
                                _simg_resp = _requests.get(_simg_url, timeout=15)
                                _simg_resp.raise_for_status()
                                poster_image = _Image.open(_BytesIO2(_simg_resp.content))
                                poster_image.load()
                                self.logger.info(
                                    f"Using TMDB English season poster for {season_plex_rating_key} "
                                    f"(season {season_number}, tmdb_id={_resolved_tmdb_id})")
                            else:
                                self.logger.info(
                                    f"No English season posters on TMDB for season {season_number} "
                                    f"tmdb_id={_resolved_tmdb_id} — falling back to Plex")
                    except Exception as _tmdb_season_err:
                        self.logger.info(
                            f"TMDB season poster fetch failed for {season_plex_rating_key}: {_tmdb_season_err}")

                if not poster_image:
                    self.logger.info(
                        f"Downloading season poster from media server for {season_plex_rating_key}")
                    poster_image = self.client.download_poster(season_plex_rating_key)
                if not poster_image or self._is_blank_image(poster_image):
                    # Determine why: season removed from media server vs simply no poster
                    _season_gone = False
                    try:
                        _check = self.client.get_media_metadata(season_plex_rating_key)
                        if not _check:
                            _season_gone = True
                    except Exception:
                        _season_gone = True
                    if _season_gone:
                        self.logger.info(
                            f"Season {season_plex_rating_key} no longer in media server — marking removed")
                        self._update_season_overlay_state(
                            show_plex_rating_key, season_plex_rating_key, season_number,
                            'removed', 'Season no longer in media server')
                    else:
                        self.logger.info(
                            f"Season {season_plex_rating_key} has no poster in media server — marking no_poster")
                        self._update_season_overlay_state(
                            show_plex_rating_key, season_plex_rating_key, season_number,
                            'no_poster', 'No season poster available')
                    result['status'] = 'skipped'
                    result['message'] = ('Season no longer in media server'
                                         if _season_gone else 'No season poster available')
                    return result
                # Save as backup
                original_bytes = self.renderer.image_to_bytes(poster_image)
                backup_mgr.backup_season_poster(season_plex_rating_key, original_bytes)
                self.logger.info(
                    f"Saved season poster backup for {season_plex_rating_key}")

            # ── Normalise poster to 2:3 aspect ratio ─────────────────────
            # Season posters from Plex can arrive at unusual sizes/aspect ratios.
            # Normalise to a consistent 2:3 so badge scaling matches the layout
            # builder preview (which uses 600×900 as its reference canvas).
            if poster_image:
                pw, ph = poster_image.size
                expected_h = int(pw * 1.5)   # 2:3  → height = width * 1.5
                if abs(ph - expected_h) > 4:   # more than 4px off
                    self.logger.info(
                        f"Season poster for {season_plex_rating_key} is {pw}×{ph}; "
                        f"normalising to {pw}×{expected_h} (2:3)")
                    poster_image = poster_image.resize(
                        (pw, expected_h), _PILImage.Resampling.LANCZOS)

            # ── Layout selection ──────────────────────────────────────────
            template = None
            if layout_id:
                template = self.layout_mgr.get_layout(layout_id)
                if not template:
                    result['status'] = 'error'
                    result['message'] = f'Layout {layout_id} not found'
                    return result
            else:
                # Only use an active 'season' layout — do NOT fall back to 'tv'.
                # Season overlays should only be applied when the user has explicitly
                # configured a season layout; the TV layout is for show posters only.
                layouts = self.layout_mgr.list_layouts(media_type='season', active_only=True)
                if layouts:
                    template = layouts[0]
                    self.logger.info(
                        f"Using 'season' layout for season overlay: {template['name']}")
                else:
                    self.logger.debug(
                        f"No active 'season' layout found, skipping season overlay "
                        f"for {season_plex_rating_key}"
                    )
                    result['status'] = 'skipped'
                    result['message'] = "No active 'season' layout configured"
                    return result

            # ── Render overlay ────────────────────────────────────────────
            self.logger.info(
                f"Rendering season overlay for key={season_plex_rating_key} "
                f"(show={show_plex_rating_key}, season={season_number})")
            overlay_image = self.renderer.render_from_template(
                poster_image, template['layout_json'], media_info)

            overlay_bytes = self.renderer.image_to_bytes(overlay_image)

            # ── Upload ────────────────────────────────────────────────────
            self.logger.info(f"Uploading season overlay poster for {season_plex_rating_key}")
            upload_success = self.client.upload_poster(season_plex_rating_key, overlay_bytes)

            if not upload_success:
                result['status'] = 'error'
                result['message'] = 'Failed to upload season overlay poster to media server'
                self._update_season_overlay_state(
                    show_plex_rating_key, season_plex_rating_key, season_number,
                    'failed', 'Upload failed')
                return result

            # Invalidate season thumbnail cache so the overlay page shows the new poster
            try:
                from routes.overlay_routes import invalidate_season_thumb_cache
                invalidate_season_thumb_cache(season_plex_rating_key)
            except Exception:
                pass

            # ── Store quality hash ────────────────────────────────────────
            quality_hash = ''
            if best_ep and best_ep.get('ms_resolution'):
                quality_hash = self._compute_quality_hash(
                    best_ep.get('ms_resolution'),
                    best_ep.get('ms_hdr'),
                    best_ep.get('ms_dolby_vision'),
                    best_ep.get('ms_audio_codec'),
                    best_ep.get('ms_audio_channels'),
                    best_ep.get('ms_video_codec'),
                )

            season_layout_hash = self._compute_layout_hash(template) if template else None
            season_content_hash = self._compute_content_hash(
                imdb_rating=media_info.get('imdb_rating'),
                tmdb_rating=media_info.get('tmdb_rating'),
                trakt_rating=media_info.get('trakt_rating'),
                rt_critics_score=media_info.get('rt_critics_score'),
                rt_user_score=media_info.get('rt_user_score'),
                status=media_info.get('status'),
                version_count=None,  # seasons don't display version count badge
            )
            import hashlib as _hl
            _season_upload_sha1 = _hl.sha1(overlay_bytes).hexdigest()
            self._update_season_overlay_state(
                show_plex_rating_key, season_plex_rating_key, season_number,
                'applied', 'Season overlay successfully applied',
                metadata_hash=quality_hash or None,
                layout_hash=season_layout_hash,
                content_hash=season_content_hash,
                plex_upload_hash=_season_upload_sha1)

            result['success'] = True
            result['status'] = 'applied'
            result['message'] = f'Season {season_number} overlay applied'
            self.logger.info(
                f"Successfully applied season overlay for "
                f"show={show_plex_rating_key} season={season_number}")

        except Exception as e:
            result['status'] = 'error'
            result['message'] = str(e)
            # 404 means the Plex ratingKey no longer exists (stale after library rescan).
            # Delete the row so it stops being retried — it will be re-registered with
            # the new ratingKey on the next sync.
            if '404' in str(e):
                self.logger.warning(
                    f"Season overlay skipped for {season_plex_rating_key} — stale Plex key (404), will re-sync")
                try:
                    from database.core import get_db_connection
                    _conn = get_db_connection()
                    _conn.execute(
                        "DELETE FROM season_overlay_state WHERE season_ms_item_id = ?",
                        (str(season_plex_rating_key),))
                    _conn.commit()
                    _conn.close()
                    self.logger.info(
                        f"Removed stale season_overlay_state row for {season_plex_rating_key} (404)")
                except Exception:
                    pass
            else:
                self.logger.error(
                    f"Failed to generate season overlay for {season_plex_rating_key}: {e}",
                    exc_info=True)
                try:
                    self._update_season_overlay_state(
                        show_plex_rating_key, season_plex_rating_key, season_number,
                        'failed', str(e))
                except Exception:
                    pass

        return result

    def remove_season_overlay(self, show_plex_rating_key: str,
                              season_plex_rating_key: str,
                              season_number: int,
                              user_initiated: bool = False) -> Dict[str, Any]:
        """
        Remove season overlay and restore the original season poster in Plex.

        Args:
            user_initiated: If True, marks status as 'user_removed' (sync will never
                re-apply). If False (default), marks as 'removed' so the next sync
                will re-apply it. Use True only for explicit per-item UI removal.

        Tries (in order):
        1. Upload backed-up original poster (saved before overlay was first applied)
        2. Delete all custom-uploaded posters so Plex falls back to its own metadata poster

        Returns:
            Result dict with 'success', 'status', 'message' keys.
        """
        result = {
            'success': False,
            'season_ms_item_id': season_plex_rating_key,
            'status': None,
            'message': None,
        }
        try:
            from overlays.cache_cleanup import PosterCacheManager

            backup_mgr = PosterCacheManager(None)
            backup_file = backup_mgr.backup_dir / f"season_{season_plex_rating_key}_original.jpg"

            restored = False

            # 1. Try restoring from backup (works for both Plex and Jellyfin)
            if backup_file.exists():
                with open(str(backup_file), 'rb') as _f:
                    original_bytes = _f.read()
                if len(original_bytes) > 5120:
                    if self.client.upload_poster(season_plex_rating_key, original_bytes):
                        restored = True
                        self.logger.info(
                            f"Restored season poster from backup for {season_plex_rating_key}")
                    else:
                        self.logger.warning(
                            f"Backup upload failed for season {season_plex_rating_key}, "
                            f"trying poster deletion fallback")
                else:
                    self.logger.warning(
                        f"Season backup too small ({len(original_bytes)} bytes) for "
                        f"{season_plex_rating_key}, trying poster deletion fallback")
            else:
                self.logger.warning(
                    f"No season poster backup found for {season_plex_rating_key}, "
                    f"trying poster deletion fallback")

            if not restored and self._jellyfin_mode:
                # Jellyfin: DELETE /Items/{id}/Images/Primary reverts to metadata poster
                try:
                    if self.client.delete_poster(season_plex_rating_key):
                        restored = True
                        self.logger.info(
                            f"Restored Jellyfin season poster via DELETE for {season_plex_rating_key}")
                    else:
                        self.logger.warning(
                            f"Jellyfin DELETE poster failed for season {season_plex_rating_key}")
                except Exception as _del_err:
                    self.logger.error(
                        f"Jellyfin season poster delete failed for {season_plex_rating_key}: {_del_err}")

            # 2. Plex fallback: season poster keys use 'upload://posters/seasons/N/{hash}' which
            # the Plex DELETE API rejects. Instead, find a clean metadata poster and PUT-select it.
            if not restored and not self._jellyfin_mode:
                try:
                    poster_list = self.plex.get_poster_list(season_plex_rating_key)
                    self.logger.info(
                        f"Season {season_plex_rating_key} poster list ({len(poster_list)} items): "
                        f"{[{'ratingKey': p.get('ratingKey',''), 'selected': p.get('selected'), 'provider': p.get('provider','')} for p in poster_list]}"
                    )
                    selected = next((p for p in poster_list if p.get('selected')), None)
                    selected_key = selected.get('ratingKey', '') if selected else ''
                    if not selected_key.startswith('upload://'):
                        # No overlay poster is selected — nothing to remove
                        self.logger.info(
                            f"Season {season_plex_rating_key} selected poster is not an upload "
                            f"({selected_key!r}), skipping")
                        result['status'] = 'skipped'
                        result['message'] = f'Season {season_number} has no overlay to remove'
                        return result
                    # Prefer metadata:// poster, fall back to any non-upload poster.
                    # PUT select returns 404 for all key types on seasons, so we
                    # download the clean poster image and re-upload it (Kometa's approach).
                    clean_poster = next(
                        (p for p in poster_list
                         if p.get('ratingKey', '').startswith('metadata://')
                         and not p.get('selected')),
                        None
                    ) or next(
                        (p for p in poster_list
                         if not p.get('ratingKey', '').startswith('upload://')
                         and not p.get('selected')),
                        None
                    )
                    if clean_poster:
                        poster_rating_key = clean_poster['ratingKey']
                        image_bytes = self.plex.download_poster_by_rating_key(
                            season_plex_rating_key, poster_rating_key)
                        if image_bytes and len(image_bytes) > 5120:
                            if self.plex.upload_poster(season_plex_rating_key, image_bytes):
                                restored = True
                                self.logger.info(
                                    f"Restored season {season_plex_rating_key} poster "
                                    f"by downloading and re-uploading {poster_rating_key!r}")
                            else:
                                self.logger.warning(
                                    f"upload_poster failed for season {season_plex_rating_key}")
                        else:
                            self.logger.warning(
                                f"Downloaded poster too small or empty for season "
                                f"{season_plex_rating_key} (key={poster_rating_key!r})")
                    else:
                        self.logger.warning(
                            f"No clean poster found for season {season_plex_rating_key}")
                except Exception as _del_err:
                    self.logger.error(
                        f"Season poster restore failed for {season_plex_rating_key}: {_del_err}")

            if restored:
                _status = 'user_removed' if user_initiated else 'removed'
                _msg    = 'Season overlay removed by user' if user_initiated else 'Season overlay removed'
                self._update_season_overlay_state(
                    show_plex_rating_key, season_plex_rating_key, season_number,
                    _status, _msg)

                result['success'] = True
                result['status'] = 'removed'
                result['message'] = f'Season {season_number} overlay removed'
                self.logger.info(
                    f"Removed season overlay for show={show_plex_rating_key} "
                    f"season={season_number}")
            else:
                msg = 'Could not restore season poster — no backup and no clean poster found'
                result['status'] = 'error'
                result['message'] = msg
                try:
                    self._update_season_overlay_state(
                        show_plex_rating_key, season_plex_rating_key, season_number,
                        'removal_failed', msg)
                except Exception:
                    pass

        except Exception as e:
            self.logger.error(
                f"Failed to remove season overlay for {season_plex_rating_key}: {e}",
                exc_info=True)
            result['status'] = 'error'
            result['message'] = str(e)
            try:
                self._update_season_overlay_state(
                    show_plex_rating_key, season_plex_rating_key, season_number,
                    'removal_failed', str(e))
            except Exception:
                pass

        return result

    def batch_generate_overlays(self, media_item_ids: list, force: bool = False,
                               layout_id: Optional[int] = None,
                               force_fresh_poster: bool = False) -> Dict[str, Any]:
        """
        Generate overlays for multiple items.

        Args:
            media_item_ids: List of media item IDs
            force: Force regeneration
            layout_id: Optional layout ID to use for all items

        Returns:
            Summary dictionary with results for each item
        """
        results = {
            'total': len(media_item_ids),
            'applied': 0,
            'analyzing': 0,
            'failed': 0,
            'skipped': 0,
            'items': []
        }

        # Acquire the same semaphore used by apply_overlay_for_new_item so batch
        # operations respect the 3-concurrent-image-op cap and don't bypass the
        # OOM protection when "Generate All" is running alongside new-item triggers.
        try:
            from overlays.scheduled_tasks import _overlay_semaphore as _batch_sem
        except Exception:
            _batch_sem = None

        for item_id in media_item_ids:
            if _batch_sem is not None:
                _batch_sem.acquire()
            try:
                result = self.generate_overlay_for_item(
                    item_id, force=force, layout_id=layout_id,
                    force_fresh_poster=force_fresh_poster)
            finally:
                if _batch_sem is not None:
                    _batch_sem.release()
            results['items'].append(result)

            # Update counters
            status = result.get('status', 'failed')
            if status == 'applied':
                results['applied'] += 1
            elif status == 'analyzing':
                results['analyzing'] += 1
            elif status == 'skipped':
                results['skipped'] += 1
            else:
                results['failed'] += 1

            # Update shared job state so the /generate/status endpoint can report progress
            _s = get_generate_status()
            if _s.get('running'):
                _title = result.get('details', {}).get('title') or result.get('title') or str(item_id)
                _errs = list(_s.get('errors', []))
                if status not in ('applied', 'analyzing', 'skipped'):
                    _errs.append(f"{_title}: {result.get('message', 'failed')}")
                    if len(_errs) > 50:
                        _errs = _errs[-50:]
                _update_gen(
                    done=_s['done'] + 1,
                    applied=_s['applied'] + (1 if status == 'applied' else 0),
                    failed=_s['failed'] + (1 if status not in ('applied', 'analyzing', 'skipped') else 0),
                    skipped=_s['skipped'] + (1 if status in ('skipped', 'analyzing') else 0),
                    current=_title,
                    errors=_errs,
                )

        self.logger.info(f"Batch overlay generation complete: {results['applied']}/{results['total']} applied, "
                       f"{results['analyzing']} analyzing, {results['failed']} failed, {results['skipped']} skipped")

        return results
