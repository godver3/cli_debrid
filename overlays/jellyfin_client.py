"""
Jellyfin/Emby API Client

Handles interactions with Jellyfin or Emby Media Server API for overlay operations.
Implements the same interface as PlexClient so OverlayManager can swap clients cleanly.

Auth: X-Emby-Token header (works for both Jellyfin and Emby).
Settings: Debug.emby_jellyfin_url / Debug.emby_jellyfin_token
"""

import base64
import logging
from io import BytesIO
from typing import Dict, Any, Optional, List

import requests
from PIL import Image


class JellyfinClient:
    """
    Jellyfin/Emby Media Server API client for overlay operations.

    Handles:
    - Fetching media metadata (MediaSources / MediaStreams)
    - Downloading poster images
    - Uploading overlay posters
    - Deleting custom poster images (API-native — no filesystem access needed)
    - Library enumeration with external IDs (IMDb / TMDB)
    - Season and episode navigation
    """

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        """
        Initialize Jellyfin/Emby client.

        Args:
            base_url: Server base URL (e.g. "http://jellyfin:8096")
            token:    API key / token (X-Emby-Token)
            timeout:  Default request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            'X-Emby-Token': self.token,
            'Accept': 'application/json',
        })
        self._user_id: Optional[str] = None  # cached on first use

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make authenticated request to Jellyfin/Emby API."""
        url = f"{self.base_url}{endpoint}"
        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout
        self.logger.debug(f"{method} {url}")
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def _get_user_id(self) -> str:
        """
        Return the first admin user ID. Cached after first call.

        Jellyfin/Emby require a userId on some endpoints (e.g. Items listing).
        We use the first administrator account found.
        """
        if self._user_id:
            return self._user_id
        try:
            resp = self._make_request('GET', '/Users')
            users = resp.json()
            for u in users:
                if u.get('Policy', {}).get('IsAdministrator'):
                    self._user_id = u['Id']
                    return self._user_id
            # fallback: first user
            if users:
                self._user_id = users[0]['Id']
                return self._user_id
        except Exception as e:
            self.logger.warning(f"Could not fetch Jellyfin user list: {e}")
        return ''

    # ─────────────────────────────────────────────────────────────────────────
    # Metadata
    # ─────────────────────────────────────────────────────────────────────────

    def get_media_metadata(self, item_id: str) -> Dict[str, Any]:
        """
        Fetch complete metadata for a media item, including MediaSources and MediaStreams.

        Args:
            item_id: Jellyfin/Emby item ID (UUID string)

        Returns:
            Item metadata dict with MediaSources/MediaStreams populated.

        Raises:
            requests.exceptions.RequestException: On API failure (including 404)
        """
        user_id = self._get_user_id()
        params = {
            'Fields': 'MediaSources,MediaStreams,ProviderIds,Overview',
        }
        if user_id:
            endpoint = f"/Users/{user_id}/Items/{item_id}"
        else:
            endpoint = f"/Items/{item_id}"
        response = self._make_request('GET', endpoint, params=params)
        data = response.json()
        self.logger.debug(f"Fetched metadata for item {item_id}: {data.get('Name', '?')}")
        return data

    # ─────────────────────────────────────────────────────────────────────────
    # Poster operations
    # ─────────────────────────────────────────────────────────────────────────

    def download_poster(self, item_id: str) -> Optional[Image.Image]:
        """
        Download the Primary poster image for a media item.

        Args:
            item_id: Jellyfin/Emby item ID

        Returns:
            PIL Image object or None on failure
        """
        try:
            response = self._make_request('GET', f"/Items/{item_id}/Images/Primary")
            img = Image.open(BytesIO(response.content))
            self.logger.info(f"Downloaded poster for {item_id} ({img.size[0]}x{img.size[1]})")
            return img
        except Exception as e:
            self.logger.error(f"Failed to download poster for {item_id}: {e}")
            return None

    def upload_poster(self, item_id: str, image_bytes: bytes) -> bool:
        """
        Upload overlay poster as the Primary image for a media item.

        Jellyfin/Emby require the image body to be base64-encoded (not raw binary)
        when POSTing to /Items/{id}/Images/Primary.

        Args:
            item_id:     Jellyfin/Emby item ID
            image_bytes: JPEG image data (raw bytes — will be base64-encoded internally)

        Returns:
            True on success, False otherwise
        """
        try:
            encoded = base64.b64encode(image_bytes)
            self._make_request(
                'POST', f"/Items/{item_id}/Images/Primary",
                data=encoded,
                headers={'Content-Type': 'image/jpeg'},
            )
            self.logger.info(f"Uploaded overlay poster for {item_id}")
            return True
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise  # Let caller handle stale item ID
            self.logger.error(f"Failed to upload poster for {item_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to upload poster for {item_id}: {e}")
            return False

    def delete_poster(self, item_id: str) -> bool:
        """
        Delete the custom Primary poster for a media item, causing the server
        to revert to its metadata-sourced poster.

        Jellyfin/Emby support full API-native deletion — no filesystem access needed.

        Args:
            item_id: Jellyfin/Emby item ID

        Returns:
            True on success, False otherwise
        """
        try:
            self._make_request('DELETE', f"/Items/{item_id}/Images/Primary")
            self.logger.info(f"Deleted custom poster for {item_id}")
            return True
        except Exception as e:
            self.logger.debug(f"Failed to delete poster for {item_id}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Library enumeration
    # ─────────────────────────────────────────────────────────────────────────

    def get_all_items_with_guids(self, media_type: str = 'movie') -> List[Dict[str, Any]]:
        """
        Enumerate all library items of a given type with their external IDs.

        Args:
            media_type: 'movie' or 'episode' (maps to Jellyfin IncludeItemTypes)

        Returns:
            List of dicts: {ratingKey (=ItemId), title, year, imdb_id, tmdb_id}
            'ratingKey' key name is intentionally kept to match PlexClient's return value
            so callers (e.g. _sync_library_keys_for_new_items) work identically.
        """
        # 'Movie' → Movie, 'Series' → Series (for show-level ID matching),
        # anything else (legacy 'episode') → Episode
        if media_type in ('Movie', 'movie'):
            jf_type = 'Movie'
        elif media_type in ('Series', 'series'):
            jf_type = 'Series'
        else:
            jf_type = 'Episode'
        user_id = self._get_user_id()

        results = []
        start_index = 0
        page_size = 500

        while True:
            params = {
                'IncludeItemTypes': jf_type,
                'Recursive': 'true',
                'Fields': 'ProviderIds',
                'StartIndex': str(start_index),
                'Limit': str(page_size),
            }
            try:
                if user_id:
                    endpoint = f"/Users/{user_id}/Items"
                else:
                    endpoint = "/Items"
                resp = self._make_request('GET', endpoint, params=params)
                data = resp.json()
                items = data.get('Items', [])

                for item in items:
                    item_id = item.get('Id', '')
                    if not item_id:
                        continue
                    provider_ids = item.get('ProviderIds', {})
                    imdb_id = provider_ids.get('Imdb') or provider_ids.get('IMDB') or None
                    tmdb_id = provider_ids.get('Tmdb') or provider_ids.get('TMDB') or None
                    results.append({
                        'ratingKey': item_id,
                        'title': item.get('Name', ''),
                        'year': item.get('ProductionYear'),
                        'imdb_id': imdb_id,
                        'tmdb_id': str(tmdb_id) if tmdb_id else None,
                        # grandparentRatingKey for episodes (SeriesId)
                        'grandparentRatingKey': item.get('SeriesId') or None,
                    })

                total = data.get('TotalRecordCount', 0)
                start_index += len(items)
                if start_index >= total or not items:
                    break

            except Exception as e:
                self.logger.error(f"Failed to fetch {jf_type} items (offset={start_index}): {e}")
                break

        self.logger.info(f"get_all_items_with_guids({media_type}): found {len(results)} items")
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Season / episode navigation
    # ─────────────────────────────────────────────────────────────────────────

    def get_show_seasons(self, show_item_id: str) -> List[Dict[str, Any]]:
        """
        Get all seasons for a TV series.

        Args:
            show_item_id: Jellyfin/Emby series item ID

        Returns:
            List of dicts: {ratingKey, title, index (season number)}
        """
        try:
            resp = self._make_request('GET', f"/Shows/{show_item_id}/Seasons")
            data = resp.json()
            seasons = []
            for item in data.get('Items', []):
                seasons.append({
                    'ratingKey': item.get('Id', ''),
                    'title': item.get('Name', ''),
                    'index': item.get('IndexNumber', 0),
                })
            seasons.sort(key=lambda s: s['index'])
            self.logger.info(f"Found {len(seasons)} season(s) for series {show_item_id}")
            return seasons
        except Exception as e:
            self.logger.error(f"Failed to get seasons for {show_item_id}: {e}")
            return []

    def get_season_best_episode_media(self, season_item_id: str) -> Optional[Dict[str, Any]]:
        """
        Return full metadata for the highest-resolution episode in a season.

        Args:
            season_item_id: Jellyfin/Emby season item ID

        Returns:
            Full item metadata dict for the best episode, or None
        """
        try:
            user_id = self._get_user_id()
            params = {
                'ParentId': season_item_id,
                'IncludeItemTypes': 'Episode',
                'Recursive': 'false',
                'Fields': 'MediaSources,MediaStreams,ProviderIds',
            }
            if user_id:
                endpoint = f"/Users/{user_id}/Items"
            else:
                endpoint = "/Items"
            resp = self._make_request('GET', endpoint, params=params)
            episodes = resp.json().get('Items', [])

            _RES = {'3840': 6, '2160': 6, '1440': 5, '1920': 4, '1080': 4,
                    '1280': 3, '720': 3, '576': 2, '480': 1}

            def _rank(ep):
                sources = ep.get('MediaSources') or []
                if not sources:
                    return 0
                streams = sources[0].get('MediaStreams') or []
                for s in streams:
                    if s.get('Type') == 'Video':
                        w = str(s.get('Width', 0))
                        return _RES.get(w, 0)
                return 0

            episodes_with_media = [e for e in episodes if e.get('MediaSources')]
            if not episodes_with_media:
                return None

            best = max(episodes_with_media, key=_rank)
            return self.get_media_metadata(best['Id'])

        except Exception as e:
            self.logger.error(f"Failed to get best episode for season {season_item_id}: {e}")
            return None

    def get_show_best_episode_media(self, show_item_id: str) -> Optional[Dict[str, Any]]:
        """
        Return full metadata for the highest-resolution episode across all seasons.

        Args:
            show_item_id: Jellyfin/Emby series item ID

        Returns:
            Full item metadata dict for the best episode, or None
        """
        try:
            user_id = self._get_user_id()
            params = {
                'SeriesId': show_item_id,
                'IncludeItemTypes': 'Episode',
                'Recursive': 'true',
                'Fields': 'MediaSources,MediaStreams',
                'Limit': '500',
            }
            if user_id:
                endpoint = f"/Users/{user_id}/Items"
            else:
                endpoint = "/Items"
            resp = self._make_request('GET', endpoint, params=params)
            episodes = resp.json().get('Items', [])

            _RES = {'3840': 6, '2160': 6, '1440': 5, '1920': 4, '1080': 4,
                    '1280': 3, '720': 3, '576': 2, '480': 1}
            _HDR_RANK = {'DOLBYVISION': 3, 'HDR10PLUS': 2, 'HDR10': 2, 'HDR': 1, 'SDR': 0}

            def _rank(ep):
                sources = ep.get('MediaSources') or []
                if not sources:
                    return (0, 0)
                streams = sources[0].get('MediaStreams') or []
                res_rank = 0
                hdr_rank = 0
                for s in streams:
                    if s.get('Type') == 'Video':
                        res_rank = _RES.get(str(s.get('Width', 0)), 0)
                        vr = (s.get('VideoRangeType') or '').upper().replace(' ', '').replace('-', '')
                        hdr_rank = _HDR_RANK.get(vr, 0)
                        break
                return (res_rank, hdr_rank)

            episodes_with_media = [e for e in episodes if e.get('MediaSources')]
            if not episodes_with_media:
                return None

            best = max(episodes_with_media, key=_rank)
            return self.get_media_metadata(best['Id'])

        except Exception as e:
            self.logger.error(f"Failed to get best episode for series {show_item_id}: {e}")
            return None
