"""
Plex API Client

Handles interactions with Plex Media Server API for overlay operations.
"""

import logging
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urljoin

import requests
from PIL import Image


class PlexClient:
    """
    Plex Media Server API client.

    Handles:
    - Fetching media metadata
    - Downloading poster images
    - Uploading overlay posters
    - Triggering media analysis
    - Checking analysis status
    """

    def __init__(self, base_url: str, token: str, timeout: int = 30):
        """
        Initialize Plex client.

        Args:
            base_url: Plex server base URL (e.g., "http://localhost:32400")
            token: Plex authentication token (X-Plex-Token)
            timeout: Default request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'application/json'})

    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """
        Make authenticated request to Plex API.

        Args:
            method: HTTP method (GET, PUT, POST, etc.)
            endpoint: API endpoint (e.g., "/library/metadata/12345")
            **kwargs: Additional arguments for requests

        Returns:
            Response object

        Raises:
            requests.exceptions.RequestException: On request failure
        """
        url = urljoin(self.base_url, endpoint)
        params = kwargs.get('params', {})
        params['X-Plex-Token'] = self.token
        kwargs['params'] = params

        if 'timeout' not in kwargs:
            kwargs['timeout'] = self.timeout

        self.logger.debug(f"{method} {url}")
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def get_media_metadata(self, rating_key: str) -> Dict[str, Any]:
        """
        Fetch complete metadata for a media item.

        Args:
            rating_key: Plex rating key (item ID)

        Returns:
            Dictionary containing media metadata

        Raises:
            requests.exceptions.RequestException: On API failure
        """
        endpoint = f"/library/metadata/{rating_key}"
        response = self._make_request('GET', endpoint)

        # Try JSON first
        try:
            data = response.json()
            if 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
                metadata = data['MediaContainer']['Metadata'][0]
                self.logger.info(f"Fetched metadata for rating_key {rating_key}: {metadata.get('title', 'Unknown')}")
                return metadata
        except (ValueError, KeyError):
            # Response is XML — fall through to XML parser
            pass

        # Fallback to XML parsing
        tree = ET.fromstring(response.content)
        metadata_elem = tree.find('.//Video') or tree.find('.//Movie') or tree.find('.//Episode')

        if metadata_elem is None:
            raise ValueError(f"No metadata found for rating_key {rating_key}")

        metadata = dict(metadata_elem.attrib)
        self.logger.info(f"Fetched metadata for rating_key {rating_key}: {metadata.get('title', 'Unknown')}")
        return metadata

    def get_poster_url(self, rating_key: str) -> Optional[str]:
        """
        Get poster image URL for a media item.

        Args:
            rating_key: Plex rating key

        Returns:
            Full poster URL or None if not available
        """
        try:
            metadata = self.get_media_metadata(rating_key)
            thumb = metadata.get('thumb')
            if thumb:
                # Convert relative path to full URL
                if thumb.startswith('/'):
                    poster_url = f"{self.base_url}{thumb}?X-Plex-Token={self.token}"
                    return poster_url
        except Exception as e:
            self.logger.error(f"Failed to get poster URL for {rating_key}: {e}")
        return None

    def get_thumb_url(self, rating_key: str) -> Optional[str]:
        """Return the current thumb path for a media item (e.g. /library/metadata/18036/thumb/1779017573).
        Used to detect poster deselection — the timestamp suffix changes when selected poster changes."""
        try:
            metadata = self.get_media_metadata(rating_key)
            return metadata.get('thumb')
        except Exception:
            return None

    def get_bulk_thumb_urls(self, section_id: str) -> dict:
        """Fetch current thumb URL for all items in a library section in one API call.
        Returns {rating_key: thumb_path} dict."""
        result = {}
        try:
            url = f"{self.base_url}/library/sections/{section_id}/all"
            params = {'X-Plex-Token': self.token, 'X-Plex-Container-Start': 0, 'X-Plex-Container-Size': 100000}
            headers = {'Accept': 'application/xml'}
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code != 200:
                return result
            import xml.etree.ElementTree as _ET
            root = _ET.fromstring(resp.content)
            for item in root:
                rk = item.get('ratingKey')
                thumb = item.get('thumb')
                if rk and thumb:
                    result[rk] = thumb
        except Exception as e:
            self.logger.warning(f"get_bulk_thumb_urls section {section_id}: {e}")
        return result

    def download_poster(self, rating_key: str) -> Optional[Image.Image]:
        """
        Download original poster image for a media item.

        Args:
            rating_key: Plex rating key

        Returns:
            PIL Image object or None if download fails
        """
        try:
            metadata = self.get_media_metadata(rating_key)
            thumb = metadata.get('thumb')
            if not thumb:
                self.logger.warning(f"No poster thumb found for rating_key {rating_key}")
                return None
            # thumb is always a relative path (e.g. '/library/metadata/123/thumb/456')
            # _make_request prepends base_url and appends the auth token
            response = self._make_request('GET', thumb)
            img = Image.open(BytesIO(response.content))
            self.logger.info(f"Downloaded poster for rating_key {rating_key} ({img.size[0]}x{img.size[1]})")
            return img
        except Exception as e:
            self.logger.error(f"Failed to download poster for {rating_key}: {e}")
            return None

    def get_poster_list(self, rating_key: str) -> list:
        """
        Get all poster versions for a media item.

        Args:
            rating_key: Plex rating key

        Returns:
            List of poster dicts with 'thumb', 'selected', and other metadata keys.
            Custom-uploaded posters have ratingKey = 'upload://posters/{hash}' and no 'provider'
            field. Pass ratingKey (not key) as the url param when deleting.
        """
        try:
            response = self._make_request('GET', f"/library/metadata/{rating_key}/posters")
            data = response.json()
            return data.get('MediaContainer', {}).get('Metadata', [])
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise  # Let caller handle stale rating key
            self.logger.error(f"Failed to get poster list for {rating_key}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Failed to get poster list for {rating_key}: {e}")
            return []

    def delete_poster(self, rating_key: str, poster_rating_key: str) -> bool:
        """
        Delete a specific uploaded poster version.

        Args:
            rating_key: Plex media item rating key
            poster_rating_key: The ratingKey of the poster to delete (e.g. 'upload://posters/{hash}').

        Returns:
            True if deletion successful, False otherwise
        """
        try:
            endpoint = f"/library/metadata/{rating_key}/posters"
            self._make_request('DELETE', endpoint, params={'url': poster_rating_key})
            self.logger.info(f"Deleted poster for rating_key {rating_key}")
            return True
        except requests.exceptions.HTTPError as e:
            self.logger.debug(
                f"Delete poster HTTP {e.response.status_code} for {rating_key} "
                f"ratingKey={poster_rating_key!r}"
            )
            return False
        except Exception as e:
            self.logger.debug(f"Failed to delete poster for {rating_key}: {e}")
            return False

    def download_poster_by_rating_key(self, rating_key: str, poster_rating_key: str) -> bytes:
        """
        Download the image bytes for a poster using its ratingKey.

        For metadata:// and upload:// posters uses Plex's /file?url= endpoint.
        For external https:// URLs downloads directly.

        Args:
            rating_key: Plex season/show/movie rating key
            poster_rating_key: The poster's ratingKey from the poster list

        Returns:
            Image bytes, or empty bytes on failure
        """
        try:
            if poster_rating_key.startswith('http'):
                response = requests.get(poster_rating_key, timeout=self.timeout)
                response.raise_for_status()
                return response.content
            else:
                response = self._make_request(
                    'GET', f"/library/metadata/{rating_key}/file",
                    params={'url': poster_rating_key}
                )
                return response.content
        except Exception as e:
            self.logger.warning(
                f"Failed to download poster {poster_rating_key!r} for {rating_key}: {e}")
            return b''

    def select_poster(self, rating_key: str, poster_rating_key: str) -> bool:
        """
        Explicitly select (activate) an uploaded poster in Plex.

        Plex's POST /posters endpoint uploads and *usually* auto-selects the new
        poster, but when an existing custom-uploaded poster is already active Plex
        may not switch automatically.  This PUT call forces the selection.

        Args:
            rating_key: Plex media item rating key
            poster_rating_key: ratingKey of the poster to select
                               (e.g. 'upload://posters/{sha1_hash}')

        Returns:
            True if selection successful, False otherwise
        """
        try:
            endpoint = f"/library/metadata/{rating_key}/posters"
            self._make_request('PUT', endpoint, params={'url': poster_rating_key})
            self.logger.info(
                f"Selected poster {poster_rating_key} for rating_key {rating_key}")
            return True
        except requests.exceptions.HTTPError as e:
            self.logger.warning(
                f"Select poster HTTP {e.response.status_code} for {rating_key} "
                f"ratingKey={poster_rating_key!r}"
            )
            return False
        except Exception as e:
            self.logger.warning(f"Failed to select poster for {rating_key}: {e}")
            return False

    def upload_poster(self, rating_key: str, image_bytes: bytes) -> bool:
        """
        Upload overlay poster to Plex.

        Args:
            rating_key: Plex rating key
            image_bytes: Image data as bytes

        Returns:
            True if upload successful, False otherwise
        """
        endpoint = f"/library/metadata/{rating_key}/posters"

        try:
            # Plex expects raw binary data in the POST body (NOT multipart).
            # Sending multipart results in a blank/ignored poster.
            response = self._make_request(
                'POST', endpoint,
                data=image_bytes,
                headers={'Content-Type': 'image/jpeg'},
            )
            self.logger.info(f"Successfully uploaded overlay poster for rating_key {rating_key}")
            return True
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise  # Let caller handle stale rating key
            self.logger.error(f"Failed to upload poster for {rating_key}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to upload poster for {rating_key}: {e}")
            return False

    def is_analyzed(self, rating_key: str) -> bool:
        """
        Check if Plex has finished analyzing this media item.

        Uses the same pattern as original plexmeta scripts:
        - Query /library/metadata/{rating_key}/tree endpoint
        - Check if duration != -1 for all media parts
        - duration=-1 means not analyzed yet

        Args:
            rating_key: Plex rating key

        Returns:
            True if fully analyzed, False otherwise
        """
        endpoint = f"/library/metadata/{rating_key}/tree"

        try:
            response = self._make_request('GET', endpoint)
            tree = ET.fromstring(response.content)

            # Find all MediaPart elements
            media_parts = tree.findall(".//MediaPart")

            if not media_parts:
                self.logger.warning(f"No media parts found for rating_key {rating_key}")
                return False

            # Check if all parts are analyzed (duration > -1)
            for part in media_parts:
                duration = int(part.get('duration', -1))
                if duration == -1:
                    self.logger.debug(f"Media part not analyzed yet: {part.get('file', 'unknown')}")
                    return False

            self.logger.info(f"Media item {rating_key} is fully analyzed")
            return True

        except Exception as e:
            self.logger.error(f"Failed to check analysis status for {rating_key}: {e}")
            return False

    def is_analyze_running(self, rating_key: Optional[str] = None) -> bool:
        """
        Check if Plex Scanner is currently running analysis.

        Uses the same pattern as original plexmeta scripts:
        - Check for Scanner processes with --manual flag
        - Optionally check if specific item is being analyzed

        Args:
            rating_key: Optional specific rating key to check

        Returns:
            True if analysis is running, False otherwise
        """
        try:
            # Check for Scanner processes
            proc = subprocess.run(
                ['ps', '-eaf'],
                capture_output=True, text=True, timeout=5
            )
            result = '\n'.join(
                line for line in proc.stdout.splitlines()
                if 'Scanner' in line and '--manual' in line
            )
            process_count = result.count('analyze')

            # More than 4 Scanner processes = system busy
            if process_count > 4:
                self.logger.debug(f"Plex Scanner busy: {process_count} analyze processes running")
                return True

            # Check if specific item is being analyzed
            if rating_key and f'item {rating_key}' in result:
                self.logger.debug(f"Item {rating_key} is currently being analyzed")
                return True

            return False

        except Exception as e:
            self.logger.error(f"Failed to check analyze status: {e}")
            return False

    def trigger_analyze(self, rating_key: str, timeout: int = 600) -> bool:
        """
        Trigger Plex's internal media analyzer for an item.

        Uses the same pattern as original plexmeta scripts:
        - PUT request to /library/metadata/{rating_key}/analyze
        - This delegates to Plex's built-in ffprobe/analyzer

        Args:
            rating_key: Plex rating key
            timeout: Analysis timeout in seconds

        Returns:
            True if trigger successful, False otherwise
        """
        endpoint = f"/library/metadata/{rating_key}/analyze"

        try:
            self._make_request('PUT', endpoint, timeout=timeout)
            self.logger.info(f"Triggered Plex analysis for rating_key {rating_key}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to trigger analysis for {rating_key}: {e}")
            return False

    def wait_for_analysis(self, rating_key: str, max_wait: int = 300,
                         check_interval: int = 10) -> bool:
        """
        Wait for Plex to finish analyzing a media item.

        Args:
            rating_key: Plex rating key
            max_wait: Maximum wait time in seconds
            check_interval: Seconds between status checks

        Returns:
            True if analysis completed, False if timeout
        """
        start_time = time.time()

        while time.time() - start_time < max_wait:
            if self.is_analyzed(rating_key):
                elapsed = int(time.time() - start_time)
                self.logger.info(f"Analysis completed for {rating_key} after {elapsed} seconds")
                return True

            self.logger.debug(f"Waiting for analysis of {rating_key}... ({int(time.time() - start_time)}s)")
            time.sleep(check_interval)

        self.logger.warning(f"Analysis timeout for {rating_key} after {max_wait} seconds")
        return False

    def get_library_items(self, library_section: str, media_type: str = 'movie') -> list:
        """
        Get all items from a Plex library section.

        Args:
            library_section: Library section ID or name
            media_type: Media type filter (movie, show, etc.)

        Returns:
            List of media items with basic metadata
        """
        endpoint = f"/library/sections/{library_section}/all"
        params = {'type': '1' if media_type == 'movie' else '2'}  # 1=movie, 2=show

        try:
            response = self._make_request('GET', endpoint, params=params)
            data = response.json()

            if 'MediaContainer' in data and 'Metadata' in data['MediaContainer']:
                items = data['MediaContainer']['Metadata']
                self.logger.info(f"Found {len(items)} {media_type}s in library section {library_section}")
                return items

            return []
        except Exception as e:
            self.logger.error(f"Failed to get library items: {e}")
            return []

    def get_all_sections(self) -> list:
        """
        Get all library sections from Plex.

        Returns:
            List of section dicts with keys: key, title, type
        """
        try:
            response = self._make_request('GET', '/library/sections')
            data = response.json()
            sections = []
            for section in data.get('MediaContainer', {}).get('Directory', []):
                sections.append({
                    'key': section.get('key'),
                    'title': section.get('title'),
                    'type': section.get('type'),  # 'movie' or 'show'
                })
            self.logger.info(f"Found {len(sections)} library sections")
            return sections
        except Exception as e:
            self.logger.error(f"Failed to get library sections: {e}")
            return []

    def get_all_seasons_for_section(self, section_key: str) -> list:
        """
        Fetch every season in a library section in a single API call.

        Uses /library/sections/{key}/all?type=3 (type 3 = season).
        Returns a list of dicts: {ratingKey, parentRatingKey (show), index}.
        Much faster than calling get_show_seasons() per show when backfilling.
        """
        endpoint = f"/library/sections/{section_key}/all"
        try:
            response = self._make_request('GET', endpoint, params={'type': 3})
            data = response.json()
            seasons = []
            for item in data.get('MediaContainer', {}).get('Metadata', []):
                seasons.append({
                    'ratingKey': str(item.get('ratingKey', '')),
                    'parentRatingKey': str(item.get('parentRatingKey', '')),  # show key
                    'index': int(item.get('index', 0)),
                    'title': item.get('title', ''),
                })
            self.logger.info(
                f"Section {section_key}: fetched {len(seasons)} season(s) in one call")
            return seasons
        except Exception as e:
            self.logger.error(
                f"Failed to get all seasons for section {section_key}: {e}")
            return []

    def get_show_seasons(self, show_rating_key: str) -> list:
        """
        Get all seasons for a TV show.

        Args:
            show_rating_key: Plex rating key of the show

        Returns:
            List of dicts: {ratingKey, title, index (season number), thumb_url}
        """
        endpoint = f"/library/metadata/{show_rating_key}/children"
        try:
            response = self._make_request('GET', endpoint)
            data = response.json()
            seasons = []
            for item in data.get('MediaContainer', {}).get('Metadata', []):
                if item.get('type') == 'season':
                    thumb = item.get('thumb', '')
                    seasons.append({
                        'ratingKey': str(item.get('ratingKey', '')),
                        'title': item.get('title', f"Season {item.get('index', 0)}"),
                        'index': int(item.get('index', 0)),
                        'thumb': thumb,
                        'thumb_url': (f"{self.base_url}{thumb}?X-Plex-Token={self.token}" if thumb else None),
                    })
            seasons.sort(key=lambda s: s['index'])
            self.logger.info(f"Found {len(seasons)} season(s) for show {show_rating_key}")
            return seasons
        except Exception as e:
            self.logger.error(f"Failed to get seasons for show {show_rating_key}: {e}")
            return []

    def get_season_best_episode_media(self, season_rating_key: str) -> Optional[Dict[str, Any]]:
        """
        Return full metadata (with Media/Part/Stream) for the highest-resolution
        episode in a specific season.

        Steps:
          1. Fetch the season's episodes via /children.
          2. Pick the episode with the best videoResolution.
          3. Re-fetch that single episode for full Stream detail.
        """
        endpoint = f"/library/metadata/{season_rating_key}/children"
        try:
            response = self._make_request('GET', endpoint)
            data = response.json()
            episodes = data.get('MediaContainer', {}).get('Metadata', [])
            episodes_with_media = [ep for ep in episodes if ep.get('Media')]
            if not episodes_with_media:
                self.logger.debug(f"No episodes with media found for season {season_rating_key}")
                return None

            _RES = {'2160': 6, '4k': 6, '1440': 5, '1080': 4, '720': 3, '576': 2, '480': 1}

            def _rank(ep):
                res = str(ep.get('Media', [{}])[0].get('videoResolution', '')).lower().replace('p', '')
                return _RES.get(res, 0)

            best = max(episodes_with_media, key=_rank)
            best_key = str(best.get('ratingKey', ''))
            if not best_key:
                return None

            self.logger.info(
                f"Best episode for season {season_rating_key}: key={best_key} "
                f"res={best.get('Media', [{}])[0].get('videoResolution', '?')}"
            )
            return self.get_media_metadata(best_key)

        except Exception as e:
            self.logger.error(f"Failed to get best episode for season {season_rating_key}: {e}")
            return None

    def get_show_best_episode_media(self, show_rating_key: str) -> Optional[Dict[str, Any]]:
        """
        Return full metadata (with Media/Part/Stream) for the highest-resolution
        episode of a TV show.  Used to get audio-codec / HDR info for the show
        overlay when the show-level Plex endpoint has no media streams.

        Steps:
          1. Fetch all episodes via /allLeaves (includes basic Media info).
          2. Pick the episode with the best videoResolution.
          3. Re-fetch that single episode to get full Stream detail.
        """
        endpoint = f"/library/metadata/{show_rating_key}/allLeaves"
        try:
            response = self._make_request(
                'GET', endpoint,
                params={'X-Plex-Container-Start': '0', 'X-Plex-Container-Size': '500'},
            )
            data = response.json()
            episodes = data.get('MediaContainer', {}).get('Metadata', [])
            episodes_with_media = [ep for ep in episodes if ep.get('Media')]
            if not episodes_with_media:
                self.logger.debug(f"No episodes with media found for show {show_rating_key}")
                return None

            _RES = {'2160': 6, '4k': 6, '1440': 5, '1080': 4, '720': 3, '576': 2, '480': 1}

            def _rank(ep):
                media = ep.get('Media', [{}])[0]
                res = str(media.get('videoResolution', '')).lower().replace('p', '')
                res_rank = _RES.get(res, 0)
                # Rank HDR/DV within same resolution so DV > HDR > SDR
                profile = str(media.get('videoProfile', '')).lower()
                if 'dolby vision' in profile or 'dovi' in profile:
                    hdr_rank = 2
                elif 'hdr' in profile:
                    hdr_rank = 1
                else:
                    hdr_rank = 0
                return (res_rank, hdr_rank)

            best = max(episodes_with_media, key=_rank)
            best_key = str(best.get('ratingKey', ''))
            if not best_key:
                return None

            _best_media = best.get('Media', [{}])[0]
            self.logger.info(
                f"Best episode for show {show_rating_key}: key={best_key} "
                f"res={_best_media.get('videoResolution', '?')} "
                f"profile={_best_media.get('videoProfile', '?')}"
            )
            # Fetch full metadata so Stream nodes (codec/HDR) are included
            return self.get_media_metadata(best_key)

        except Exception as e:
            self.logger.error(f"Failed to get best episode for show {show_rating_key}: {e}")
            return None

    def get_all_items_with_guids(self, plex_type: int, page_size: int = 1000) -> list:
        """
        Get all items of a given Plex type from all library sections, including GUIDs.

        Args:
            plex_type: Plex item type (1=movie, 2=show, 4=episode)
            page_size: Number of items per page

        Returns:
            List of dicts with ratingKey, title, year, imdb_id, tmdb_id.
            For shows (type=2), also grandparentRatingKey is not applicable.
        """
        sections = self.get_all_sections()
        results = []
        seen_keys = set()

        for section in sections:
            # movies belong in movie sections (type='movie'), shows in show sections
            if plex_type == 1 and section['type'] != 'movie':
                continue
            if plex_type in (2, 4) and section['type'] != 'show':
                continue

            section_key = section['key']
            endpoint = f"/library/sections/{section_key}/all"
            container_start = 0

            while True:
                params = {
                    'type': str(plex_type),
                    'includeGuids': '1',
                    'X-Plex-Container-Start': str(container_start),
                    'X-Plex-Container-Size': str(page_size),
                }
                try:
                    response = self._make_request('GET', endpoint, params=params, timeout=60)
                    data = response.json()
                    container = data.get('MediaContainer', {})
                    items = container.get('Metadata', [])

                    for item in items:
                        rk = str(item.get('ratingKey', ''))
                        if not rk or rk in seen_keys:
                            continue
                        seen_keys.add(rk)

                        imdb_id = None
                        tmdb_id = None
                        for guid in item.get('Guid', []):
                            gid = guid.get('id', '')
                            if gid.startswith('imdb://'):
                                imdb_id = gid[7:]
                            elif gid.startswith('tmdb://'):
                                tmdb_id = gid[7:]

                        # Extract file paths from Media[].Part[].file so the sync
                        # can match split Plex items to DB rows by location_on_disk.
                        file_paths = []
                        for media in item.get('Media', []):
                            for part in media.get('Part', []):
                                fp = part.get('file')
                                if fp:
                                    file_paths.append(fp)

                        results.append({
                            'ratingKey': rk,
                            'title': item.get('title', ''),
                            'year': item.get('year'),
                            'imdb_id': imdb_id,
                            'tmdb_id': tmdb_id,
                            'grandparentRatingKey': str(item.get('grandparentRatingKey', '')) or None,
                            'file_paths': file_paths,
                        })

                    fetched = container_start + len(items)
                    total = int(container.get('totalSize', container.get('size', 0)))
                    self.logger.debug(
                        f"Section {section['title']} type={plex_type}: fetched {fetched}/{total}")

                    if len(items) < page_size or fetched >= total:
                        break
                    container_start += page_size

                except Exception as e:
                    self.logger.error(
                        f"Failed to fetch section {section['title']} type={plex_type} offset={container_start}: {e}")
                    break

        self.logger.info(
            f"get_all_items_with_guids(type={plex_type}): fetched {len(results)} items total")
        return results
