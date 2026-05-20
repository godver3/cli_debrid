"""
Plex Smart Collection Poster Manager

Applies a shared custom poster design to enabled Plex smart collections.

State structure in plex_smart_collection_state.json:
{
  "shared": {
    "poster_design": 6,
    "poster_accent": "#FF0000",
    "poster_eyebrow": "",
    "poster_icon": "",
    "poster_overlay_opacity": 60,
    "poster_glow_opacity": 80,
    "poster_glow_radius": 55,
    "poster_hash": "...",
    "poster_has_thumbs": true
  },
  "collections": {
    "<ratingKey>": {"enabled": true, "title": "...", "section": "...", "type": "movie"},
    ...
  }
}
"""

import json
import logging
import os
import threading
from typing import Dict, List, Optional

from utilities.settings import get_setting

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.environ.get('USER_CONFIG', '/user/config'), 'plex_smart_collection_state.json')
_state_lock = threading.Lock()


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    with _state_lock:
        try:
            with open(_STATE_FILE, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'shared': {}, 'collections': {}}


def _save_state(state: dict) -> None:
    with _state_lock:
        tmp = _STATE_FILE + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, _STATE_FILE)
        except Exception as e:
            logger.error(f"[SmartCollections] Failed to save state: {e}")


def _migrate_state(state: dict) -> dict:
    """Migrate old per-collection format to new shared format."""
    if 'shared' not in state:
        state = {'shared': {}, 'collections': {}}
    if 'collections' not in state:
        state['collections'] = {}
    return state


# ── Plex helpers ──────────────────────────────────────────────────────────────

def get_all_smart_collections() -> List[Dict]:
    """Fetch all smart collections from all Plex library sections."""
    try:
        from plexapi.server import PlexServer
        plex_url = get_setting('Plex', 'url', '').rstrip('/')
        plex_token = get_setting('Plex', 'token', '')
        if not plex_url or not plex_token:
            return []
        server = PlexServer(plex_url, plex_token, timeout=30)
        result = []
        for section in server.library.sections():
            if section.type not in ('movie', 'show'):
                continue
            for coll in section.collections():
                if coll.smart:
                    result.append({
                        'ratingKey': str(coll.ratingKey),
                        'title': coll.title,
                        'section': section.title,
                        'type': section.type,
                    })
        return result
    except Exception as e:
        logger.error(f"[SmartCollections] Failed to fetch smart collections: {e}")
        return []


# ── Main task ─────────────────────────────────────────────────────────────────

def apply_smart_collection_posters() -> None:
    """Apply the shared poster design to all enabled smart collections."""
    logger.info("[SmartCollections] Task started")
    state = _migrate_state(_load_state())
    # Read collections enabled state from config.json
    from utilities.settings import get_all_settings as _get_all
    _psc = _get_all().get('Plex Smart Collections', {})
    collections = _psc.get('collections', {}) if isinstance(_psc, dict) else {}
    if not isinstance(collections, dict):
        collections = {}

    # Read design settings from config (saved by main settings save button)
    design_id = int(get_setting('Plex Smart Collections', 'poster_design', 0))
    logger.info(f"[SmartCollections] design_id={design_id}, collections_count={len(collections)}")
    if design_id == 0:
        logger.info("[SmartCollections] No poster design set, skipping")
        return

    enabled = {rk: cfg for rk, cfg in collections.items() if cfg.get('enabled', False)}
    logger.info(f"[SmartCollections] Enabled collections: {list(enabled.keys())}")
    if not enabled:
        logger.info("[SmartCollections] No enabled smart collections, skipping")
        return

    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        logger.info("[SmartCollections] Plex URL or token not set, skipping")
        return

    from database.collection_poster_renderer import (
        render_collection_poster, upload_collection_poster,
        fetch_movie_thumbs, compute_poster_hash, DESIGNS
    )

    accent = get_setting('Plex Smart Collections', 'poster_accent', '').strip()
    if not accent or accent.lower() in ('#000000', '000000'):
        accent = DESIGNS.get(design_id, {}).get('default_accent', '#E6A800') or '#E6A800'

    eyebrow         = get_setting('Plex Smart Collections', 'poster_eyebrow', '') or ''
    icon_override   = get_setting('Plex Smart Collections', 'poster_icon', '') or ''
    overlay_opacity = int(get_setting('Plex Smart Collections', 'poster_overlay_opacity', 60))
    glow_opacity    = int(get_setting('Plex Smart Collections', 'poster_glow_opacity', 80))
    glow_radius     = int(get_setting('Plex Smart Collections', 'poster_glow_radius', 55))

    # Check if shared settings hash changed
    new_hash = compute_poster_hash(
        design_id, accent, eyebrow, icon_override,
        '__smart__', [],
        overlay_opacity, glow_opacity, glow_radius
    )
    old_hash = state.get('shared', {}).get('poster_hash', '')
    old_has_thumbs = state.get('shared', {}).get('poster_has_thumbs', False)
    hash_changed = (new_hash != old_hash)
    logger.info(f"[SmartCollections] hash_changed={hash_changed}, old_hash={old_hash[:8] if old_hash else 'none'}, new_hash={new_hash[:8] if new_hash else 'none'}")

    any_uploaded = False

    for rk, cfg in enabled.items():
        collection_name = cfg.get('title', '')
        source_type = 'Trakt Lists' if cfg.get('type') == 'show' else 'MDBList'

        try:
            thumbs = fetch_movie_thumbs(plex_url, plex_token, rk, limit=4)
            has_thumbs = any(t is not None for t in thumbs)
            logger.info(f"[SmartCollections] Processing '{collection_name}' (rk={rk}), has_thumbs={has_thumbs}, hash_changed={hash_changed}")

            if not hash_changed and old_has_thumbs:
                logger.info(f"[SmartCollections] Poster unchanged for '{collection_name}', skipping")
                continue

            poster_bytes = render_collection_poster(
                design_id=design_id,
                collection_name=collection_name,
                eyebrow=eyebrow,
                accent=accent,
                icon_override=icon_override,
                source_type=source_type,
                movie_thumbs=thumbs,
                overlay_opacity=overlay_opacity,
                glow_opacity=glow_opacity,
                glow_radius=glow_radius,
            )

            if poster_bytes:
                upload_hash = upload_collection_poster(plex_url, plex_token, rk, poster_bytes)
                any_uploaded = True
                if upload_hash:
                    state.setdefault('plex_upload_hashes', {})[rk] = upload_hash
                logger.info(f"[SmartCollections] Poster applied for '{collection_name}' (rk={rk})")
            else:
                logger.warning(f"[SmartCollections] Poster render returned None for '{collection_name}'")

        except Exception as e:
            logger.error(f"[SmartCollections] Failed for rk={rk}: {e}", exc_info=True)

    if any_uploaded:
        state['shared']['poster_hash'] = new_hash
        state['shared']['poster_has_thumbs'] = True
        _save_state(state)


def reapply_single_collection_poster(rating_key: str) -> dict:
    """Immediately reapply the poster for one smart collection."""
    from utilities.settings import get_all_settings as _gas
    _psc = _gas().get('Plex Smart Collections', {})
    colls = _psc.get('collections', {}) if isinstance(_psc, dict) else {}
    cfg = colls.get(rating_key) if isinstance(colls, dict) else None
    if not cfg or not isinstance(cfg, dict):
        return {'success': False, 'message': f'Collection rk={rating_key} not found in config'}
    if not cfg.get('enabled', False):
        return {'success': False, 'message': 'Collection is not enabled'}

    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        return {'success': False, 'message': 'Plex URL or token not configured'}

    design_id = int(get_setting('Plex Smart Collections', 'poster_design', 0))
    if design_id == 0:
        return {'success': False, 'message': 'No poster design configured'}

    from database.collection_poster_renderer import (
        render_collection_poster, upload_collection_poster,
        fetch_movie_thumbs, DESIGNS
    )

    accent = get_setting('Plex Smart Collections', 'poster_accent', '').strip()
    if not accent or accent.lower() in ('#000000', '000000'):
        accent = DESIGNS.get(design_id, {}).get('default_accent', '#E6A800') or '#E6A800'
    eyebrow         = get_setting('Plex Smart Collections', 'poster_eyebrow', '') or ''
    icon_override   = get_setting('Plex Smart Collections', 'poster_icon', '') or ''
    overlay_opacity = int(get_setting('Plex Smart Collections', 'poster_overlay_opacity', 60))
    glow_opacity    = int(get_setting('Plex Smart Collections', 'poster_glow_opacity', 80))
    glow_radius     = int(get_setting('Plex Smart Collections', 'poster_glow_radius', 55))

    collection_name = cfg.get('title', '')
    source_type = 'Trakt Lists' if cfg.get('type') == 'show' else 'MDBList'

    try:
        thumbs = fetch_movie_thumbs(plex_url, plex_token, rating_key, limit=4)
        poster_bytes = render_collection_poster(
            design_id=design_id,
            collection_name=collection_name,
            eyebrow=eyebrow,
            accent=accent,
            icon_override=icon_override,
            source_type=source_type,
            movie_thumbs=thumbs,
            overlay_opacity=overlay_opacity,
            glow_opacity=glow_opacity,
            glow_radius=glow_radius,
        )
        if not poster_bytes:
            return {'success': False, 'message': 'Poster render returned None'}
        upload_hash = upload_collection_poster(plex_url, plex_token, rating_key, poster_bytes)
        if not upload_hash:
            return {'success': False, 'message': 'Poster upload failed'}
        # Force re-render on next task run by clearing shared hash
        state = _migrate_state(_load_state())
        state.setdefault('shared', {})['poster_hash'] = ''
        state.setdefault('plex_upload_hashes', {})[rating_key] = upload_hash
        _save_state(state)
        logger.info(f"[SmartCollections] Force-reapplied poster for '{collection_name}' (rk={rating_key})")
        return {'success': True, 'message': f"Poster applied for '{collection_name}'"}
    except Exception as e:
        logger.error(f"[SmartCollections] reapply_single failed for rk={rating_key}: {e}", exc_info=True)
        return {'success': False, 'message': str(e)}


def cleanup_uploaded_posters() -> int:
    """Delete non-selected uploaded posters from managed smart collections."""
    import requests
    state = _migrate_state(_load_state())
    collections = state.get('collections', {})
    if not collections:
        return 0

    plex_url = get_setting('Plex', 'url', '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '')
    if not plex_url or not plex_token:
        return 0

    headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
    deleted = 0

    for rk in collections.keys():
        try:
            r = requests.get(f"{plex_url}/library/metadata/{rk}/posters", headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            for p in r.json().get('MediaContainer', {}).get('Metadata', []):
                pk = p.get('ratingKey', '')
                if pk.startswith('upload://') and not p.get('selected'):
                    requests.delete(
                        f"{plex_url}/library/metadata/{rk}/posters",
                        params={'url': pk},
                        headers={'X-Plex-Token': plex_token},
                        timeout=10
                    )
                    deleted += 1
        except Exception as e:
            logger.debug(f"[SmartCollections] Cleanup error for rk={rk}: {e}")

    return deleted
