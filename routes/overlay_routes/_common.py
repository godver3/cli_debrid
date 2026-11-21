"""Overlay blueprints, thumbnail cache helpers and lazily created managers shared by the overlay route modules."""

import logging
from flask import Blueprint, request, render_template
from pathlib import Path
from utilities.paths import config_dir
from routes.models import admin_required
from overlays import OverlayManager, LayoutManager
from overlays.utils import is_jellyfin_mode, get_jellyfin_url, get_jellyfin_token
from utilities.settings import get_setting

# Logo library directories (two-tier: user first, system fallback)
_SYSTEM_LOGO_DIR = Path(__file__).parent.parent.parent / 'overlays' / 'assets' / 'logos'

def _user_logo_dir() -> Path:
    return Path(config_dir('overlay_assets', 'logos'))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Poster thumbnail disk cache
# Caches the resized JPEG served by poster_applied and season_thumb so that
# the overlay page never hammers Plex with hundreds of simultaneous downloads.
# Cache lives at /user/config/poster_thumb_cache/ (persisted across restarts).
# ---------------------------------------------------------------------------
def _thumb_cache_dir() -> Path:
    return Path(config_dir('poster_thumb_cache'))

def _thumb_cache_path(key: str, prefix: str = '') -> Path:
    """Return the cache file path for a given ms_item_id / season key."""
    safe = key.replace('/', '_').replace('\\', '_')
    return _thumb_cache_dir() / f"{prefix}{safe}.jpg"

def _serve_from_thumb_cache(cache_path: Path):
    """Return a Flask Response from a cached JPEG file, or None if not cached."""
    try:
        if not cache_path.exists():
            return None
        age = __import__('time').time() - cache_path.stat().st_mtime
        if age > 86400:  # 24-hour TTL
            return None
        from flask import Response
        data = cache_path.read_bytes()
        if not data:
            return None
        return Response(data, mimetype='image/jpeg',
                        headers={'Cache-Control': 'public, max-age=86400'})
    except Exception:
        return None

def _write_thumb_cache(cache_path: Path, jpeg_bytes: bytes) -> None:
    """Atomically write JPEG bytes to cache (safe under concurrent requests)."""
    if not jpeg_bytes:
        return
    try:
        _thumb_cache_dir().mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix('.tmp')
        tmp.write_bytes(jpeg_bytes)
        tmp.rename(cache_path)
    except Exception as e:
        logger.debug(f"poster thumb cache write failed for {cache_path.name}: {e}")

def invalidate_poster_thumb_cache(ms_item_id: str) -> None:
    """Delete the cached thumbnail for a movie/show after its poster changes."""
    try:
        p = _thumb_cache_path(ms_item_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass

def invalidate_season_thumb_cache(season_key: str) -> None:
    """Delete the cached thumbnail for a season after its poster changes."""
    try:
        p = _thumb_cache_path(season_key, prefix='season_')
        if p.exists():
            p.unlink()
    except Exception:
        pass

# Preferred variation keys for the badge sample-asset endpoint, ordered by desirability.
_BADGE_SAMPLE_PREFERRED_KEYS = {
    'audio_codec':    ['truehd_atmos', 'atmos', 'truehd', 'dts_hd_ma'],
    'audio_combo':    ['truehd_atmos_7.1', 'atmos_7.1', 'dts_hd_ma_7.1'],
    'resolution':     ['4k', '1080p', '2k'],
    'hdr':            ['dolby_vision', 'hdr', 'dolby_vision_hdr'],
    'resolution_hdr': ['4k_dv_hdr', '4k_plain', '1080p_hdr'],
}

# Create two blueprints - one for HTML routes, one for API
overlay_page_bp = Blueprint('overlay_page', __name__)

overlay_bp = Blueprint('overlay_api', __name__)

@admin_required
def _admin_only():
    return None

@overlay_bp.before_request
def _overlay_api_admin_only():
    """/api/overlays/* reads and changes library-wide overlay state: admins only.
    Logo images stay readable by any logged-in user (Discover shows platform icons)."""
    if request.endpoint == 'overlay_api.serve_logo':
        return None
    return _admin_only()

# HTML routes
@overlay_page_bp.route('/overlays')
@admin_required
def overlays_page():
    """Serve the overlay management UI."""
    return render_template('overlays.html')

@overlay_page_bp.route('/overlays/builder')
@admin_required
def layout_builder_page():
    """Serve the layout builder UI."""
    from utilities.settings import load_config
    _textless = load_config().get('Overlay Settings', {}).get('textless_posters', False)
    return render_template('layout_builder.html', textless_posters_enabled=_textless)

# Initialize managers (will be configured on first use)
_overlay_manager = None

_layout_manager = None

def _get_overlay_manager():
    """Get or create OverlayManager instance (recreated when media server mode changes)."""
    global _overlay_manager
    if _overlay_manager is None:
        plex_url   = get_setting('Plex', 'url',   default='http://localhost:32400').rstrip('/')
        plex_token = get_setting('Plex', 'token', default='')
        _overlay_manager = OverlayManager(None, plex_url, plex_token)
    return _overlay_manager

def _get_media_client():
    """Get a media server client (PlexClient or JellyfinClient) using app settings."""
    if is_jellyfin_mode():
        from overlays.jellyfin_client import JellyfinClient
        return JellyfinClient(get_jellyfin_url(), get_jellyfin_token())
    from overlays.plex_client import PlexClient
    plex_url   = get_setting('Plex', 'url',   default='').rstrip('/')
    plex_token = get_setting('Plex', 'token', default='')
    return PlexClient(plex_url, plex_token)

def _get_layout_manager():
    """Get or create LayoutManager instance."""
    global _layout_manager
    if _layout_manager is None:
        _layout_manager = LayoutManager(None)
    return _layout_manager
