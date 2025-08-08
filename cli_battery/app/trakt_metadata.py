from .logger_config import logger
import json
import time
import os
import pickle
from typing import Dict, Any, List, Tuple, Optional
from urllib.parse import urlparse, urlencode
import requests
from requests.exceptions import RequestException
import time
from .settings import Settings
import trakt.core
from trakt import init
from trakt.users import User
from trakt.movies import Movie
from trakt.tv import TVShow
from flask import url_for
from datetime import datetime, timedelta
import iso8601
from datetime import timezone, datetime
from collections import defaultdict
from .trakt_auth import TraktAuth
import traceback
from collections import deque
from datetime import datetime as dt
import iso8601 as iso8601_pkg

TRAKT_API_URL = "https://api.trakt.tv"
CACHE_FILE = 'db_content/trakt_last_activity.pkl'
REQUEST_TIMEOUT = 10  # seconds

class TraktMetadata:
    def __init__(self):
        self.settings = Settings()
        self.base_url = "https://api.trakt.tv"
        self.trakt_auth = TraktAuth()
        self.request_times = deque()
        # Trakt API limits:
        # GET requests: 1000 calls every 5 minutes
        # POST/PUT/DELETE: 1 call per second
        self.max_get_requests = 1000
        self.get_time_window = 300  # 5 minutes in seconds
        self.post_requests = deque()  # Track POST requests separately
        self.post_time_window = 1  # 1 second for POST requests

    def _check_rate_limit(self, method='GET'):
        current_time = time.time()
        
        if method.upper() == 'GET':
            # Remove old GET requests from the deque
            while self.request_times and current_time - self.request_times[0] > self.get_time_window:
                self.request_times.popleft()
            
            # Check if we've hit the GET rate limit
            if len(self.request_times) >= self.max_get_requests:
                logger.warning(f"GET rate limit reached. Currently at {len(self.request_times)} requests in the last {self.get_time_window} seconds.")
                return False
            
            # Add the current request time
            self.request_times.append(current_time)
            
        else:  # POST, PUT, DELETE
            # Remove old POST requests from the deque
            while self.post_requests and current_time - self.post_requests[0] > self.post_time_window:
                self.post_requests.popleft()
            
            # Check if we've hit the POST rate limit (1 per second)
            if len(self.post_requests) >= 1:
                logger.warning(f"POST rate limit reached. Currently at {len(self.post_requests)} requests in the last {self.post_time_window} second.")
                return False
            
            # Add the current request time
            self.post_requests.append(current_time)
        
        return True

    def _format_updates_start_date(self, since_iso: str) -> str:
        """Convert ISO datetime string to YYYY-MM-DD as required by updates endpoints."""
        try:
            parsed = iso8601_pkg.parse_date(since_iso)
            return parsed.date().isoformat()
        except Exception:
            try:
                # Fallback if already a date string
                return dt.fromisoformat(since_iso).date().isoformat()
            except Exception:
                # As last resort, use today-1
                return (dt.utcnow().date()).isoformat()

    def _fetch_updates_paginated(self, url_base: str) -> list:
        """Fetch all pages for a Trakt updates endpoint, returning aggregated JSON list."""
        page = 1
        limit = 100
        aggregated = []
        while True:
            url = f"{url_base}?page={page}&limit={limit}"
            response = self._make_request(url)
            if not response:
                break
            if response.status_code != 200:
                logger.warning(f"Updates fetch failed for {url} with status {response.status_code}")
                break
            data = response.json() or []
            if not isinstance(data, list):
                logger.warning(f"Unexpected updates payload type for {url}: {type(data)}")
                break
            aggregated.extend(data)
            # Pagination headers
            try:
                page_count = int(response.headers.get('X-Pagination-Page-Count', '1'))
            except ValueError:
                page_count = 1
            if page >= page_count or not data:
                break
            page += 1
        return aggregated

    def get_updated_shows(self, since_iso: str) -> list[dict]:
        """Return list of dicts with keys: imdb_id, updated_at for shows updated since given ISO time."""
        start_date = self._format_updates_start_date(since_iso)
        url_base = f"{self.base_url}/shows/updates/{start_date}"
        items = self._fetch_updates_paginated(url_base)
        results = []
        for entry in items:
            show_obj = entry.get('show') if isinstance(entry, dict) else None
            if not show_obj:
                continue
            ids = show_obj.get('ids', {})
            imdb_id = ids.get('imdb')
            updated_at = entry.get('updated_at') or show_obj.get('updated_at')
            if imdb_id:
                results.append({'imdb_id': imdb_id, 'updated_at': updated_at})
        logger.info(f"Fetched {len(results)} updated shows since {start_date}")
        return results

    def get_updated_movies(self, since_iso: str) -> list[dict]:
        """Return list of dicts with keys: imdb_id, updated_at for movies updated since given ISO time."""
        start_date = self._format_updates_start_date(since_iso)
        url_base = f"{self.base_url}/movies/updates/{start_date}"
        items = self._fetch_updates_paginated(url_base)
        results = []
        for entry in items:
            movie_obj = entry.get('movie') if isinstance(entry, dict) else None
            if not movie_obj:
                continue
            ids = movie_obj.get('ids', {})
            imdb_id = ids.get('imdb')
            updated_at = entry.get('updated_at') or movie_obj.get('updated_at')
            if imdb_id:
                results.append({'imdb_id': imdb_id, 'updated_at': updated_at})
        logger.info(f"Fetched {len(results)} updated movies since {start_date}")
        return results

    def _make_request(self, url, method='GET', max_retries=4, initial_delay=5):
        # Always get fresh auth data before making requests
        self.trakt_auth.reload_auth()
        
        if not self.trakt_auth.is_authenticated():
            logger.info("Not authenticated. Attempting to refresh token.")
            if not self.trakt_auth.refresh_access_token():
                logger.error("Failed to refresh Trakt access token.")
                return None

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': self.trakt_auth.client_id,
            'Authorization': f'Bearer {self.trakt_auth.access_token}'
        }
        
        delay = initial_delay
        for attempt in range(max_retries):
            # Check internal rate limit before each attempt
            if not self._check_rate_limit(method):
                logger.warning(f"Internal rate limit check failed on attempt {attempt + 1}. Waiting for 5 minutes.")
                time.sleep(300)
                continue # Retry after waiting

            try:
                if method.upper() == 'GET':
                    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                elif method.upper() == 'POST':
                    response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)
                elif method.upper() == 'PUT':
                    response = requests.put(url, headers=headers, timeout=REQUEST_TIMEOUT)
                elif method.upper() == 'DELETE':
                    response = requests.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
                else:
                    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

                if response.status_code == 404:
                    logger.warning(f"Request to {url} returned 404 Not Found. Not retrying.")
                    return response # Stop immediately for 404

                if response.status_code == 401:
                    logger.warning(f"Received 401 Unauthorized on attempt {attempt + 1}. Refreshing token.")
                    if self.trakt_auth.refresh_access_token():
                        headers['Authorization'] = f'Bearer {self.trakt_auth.access_token}'
                        logger.info("Token refreshed. Retrying original request immediately.")
                        continue # Go to the next attempt immediately with the new token
                    else:
                        logger.error("Failed to refresh Trakt access token after 401 error. Aborting.")
                        return None # Abort if refresh fails

                # Successful request or non-retriable error
                if response.status_code not in [429, 502, 503, 504]:
                    response.raise_for_status() # Raise for other client/server errors
                    return response

                # Retriable error occurred
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed with status {response.status_code}. "
                    f"Retrying in {delay} seconds. URL: {url}"
                )

            except requests.exceptions.RequestException as e:
                logger.error(f"RequestException on attempt {attempt + 1}/{max_retries} for URL {url}: {e}")
                if attempt == max_retries - 1:
                    logger.error("Max retries reached. Aborting request.")
                    if hasattr(e, 'response') and e.response is not None:
                         logger.error(f"Final response status code: {e.response.status_code}")
                         logger.error(f"Final response text: {e.response.text}")
                    return None

            # Wait before the next retry
            time.sleep(delay)
            delay *= 2  # Exponential backoff

        logger.error(f"Max retries reached for URL {url}. Giving up.")
        return None

    def get_metadata(self, imdb_id: str) -> Dict[str, Any]:
        logger.debug(f"Getting metadata for {imdb_id}")
        show_data = self._get_show_data(imdb_id)
        if show_data:
            logger.debug(f"Got show data for {imdb_id}")
            return {
                'type': 'show',
                'metadata': show_data
            }

        logger.debug(f"No show data found, trying movie data for {imdb_id}")
        movie_data = self._get_movie_data(imdb_id)
        if movie_data:
            logger.debug(f"Got movie data for {imdb_id}")
            movie_metadata = {
                'type': 'movie',
                'metadata': movie_data
            }
            # Add release dates to the metadata
            release_dates = self.get_release_dates(imdb_id)
            if release_dates:
                movie_metadata['metadata']['release_dates'] = release_dates
            return movie_metadata

        logger.warning(f"No metadata found for {imdb_id} (neither show nor movie)")
        return None

    def _search_by_imdb(self, imdb_id: str):
        """Search for content by IMDB ID to get Trakt slug"""
        url = f"{self.base_url}/search/imdb/{imdb_id}?type=show,movie"
        logger.debug(f"Trakt Search URL: {url}")
        response = self._make_request(url)
        if response and response.status_code == 200:
            results = response.json()
            logger.debug(f"Trakt Search Raw Results for {imdb_id}: {results}")
            if results:  # This is true if results is not None and not an empty list/dict
                logger.debug(f"Trakt Search: Taking first result for {imdb_id}: {results[0]}")
                return results[0]  # Return first match
            else: # Explicitly log if results is empty
                logger.warning(f"Trakt Search for IMDb ID {imdb_id} returned 200 OK but with an empty result list. URL: {url}")
        elif response:
            logger.warning(f"Trakt Search for {imdb_id} failed with status {response.status_code}: {response.text}. URL: {url}")
        else:
            logger.warning(f"Trakt Search for {imdb_id} received no response from _make_request. URL: {url}")
        return None

    def _get_show_data(self, imdb_id):
        # First search to get the show's Trakt slug and ID
        search_result = self._search_by_imdb(imdb_id)
        if not search_result or search_result['type'] != 'show':
            logger.debug(f"Trakt: Search for IMDB {imdb_id} did not return a show. Result: {search_result}")
            return None
            
        show = search_result['show']
        trakt_id = show['ids']['trakt']
        if not trakt_id:
            logger.error(f"Trakt: Missing trakt ID in search result for IMDb {imdb_id}. Result: {search_result}")
            return None
        
        # Now get the full show data using the Trakt ID
        url = f"{self.base_url}/shows/{trakt_id}?extended=full"
        logger.debug(f"Trakt: Fetching full show data using Trakt ID '{trakt_id}' with URL: {url}")
        response = self._make_request(url)
        if response and response.status_code == 200:
            show_data_raw = response.json()
            logger.debug(f"Trakt: Raw response from {url}: {show_data_raw}")
            returned_imdb_id = show_data_raw.get('ids', {}).get('imdb')
            if returned_imdb_id != imdb_id:
                logger.error(f"Trakt API Mismatch! Requested IMDb {imdb_id} but /shows/{trakt_id} endpoint returned data for IMDb {returned_imdb_id}. Raw Data: {show_data_raw}")
                return None
            return show_data_raw
        elif response:
            logger.warning(f"Trakt: Fetching full show data from {url} (using Trakt ID {trakt_id}) failed with status {response.status_code}: {response.text}")
        else:
            logger.warning(f"Trakt: Fetching full show data from {url} (using Trakt ID {trakt_id}) received no response.")
        return None

    def _get_movie_data(self, imdb_id):
        # Attempt to fetch movie data directly using IMDb ID
        direct_url = f"{self.base_url}/movies/{imdb_id}?extended=full"
        logger.info(f"TraktMetadata._get_movie_data: Fetching movie data directly using IMDb ID '{imdb_id}' with URL: {direct_url}")
        response = self._make_request(direct_url)

        if response and response.status_code == 200:
            movie_data_raw = response.json()
            logger.info(f"TraktMetadata._get_movie_data: Raw response from direct movie URL {direct_url}: {movie_data_raw}")

            returned_imdb_id = movie_data_raw.get('ids', {}).get('imdb')
            if returned_imdb_id != imdb_id:
                logger.error(f"Trakt API Mismatch! Requested IMDb {imdb_id} but endpoint {direct_url} returned data for IMDb {returned_imdb_id}. Raw Data: {movie_data_raw}")
                return None

            slug = movie_data_raw.get('ids', {}).get('slug')

            if slug:
                logger.info(f"TraktMetadata._get_movie_data: Fetching aliases for movie slug {slug} (IMDb: {imdb_id})")
                aliases = self._get_movie_aliases(slug)  # _get_movie_aliases needs slug
                if aliases:
                    movie_data_raw['aliases'] = aliases
                    logger.info(f"TraktMetadata._get_movie_data: Successfully added aliases for {imdb_id}.")
                else:
                    logger.info(f"TraktMetadata._get_movie_data: No aliases found or error fetching aliases for {imdb_id}.")

                # Fetch release dates - use the SLUG endpoint as it appears more accurate
                releases_raw = None
                release_url = f"{self.base_url}/movies/{slug}/releases"
                logger.info(
                    "TraktMetadata._get_movie_data: Attempting to fetch release dates from slug endpoint: %s (IMDb: %s)",
                    release_url, imdb_id
                )
                resp_rel = self._make_request(release_url)
                if resp_rel and resp_rel.status_code == 200:
                    temp_json = resp_rel.json()
                    if temp_json:  # Non-empty list
                        releases_raw = temp_json
                        logger.info("Successfully obtained %d release records from %s", len(releases_raw), release_url)
                    else:
                        logger.info("Received empty release list from %s", release_url)
                else:
                    logger.info("Failed to obtain releases from %s (status: %s)", release_url, resp_rel.status_code if resp_rel else 'No response')

                if releases_raw is not None:
                    formatted_releases = defaultdict(list)
                    for release_item in releases_raw:
                        country = release_item.get('country')
                        release_date_str = release_item.get('release_date')
                        release_type = release_item.get('release_type')
                        if country and release_date_str:
                            try:
                                date_obj = iso8601.parse_date(release_date_str)
                                if date_obj.tzinfo is not None:
                                    from metadata.metadata import _get_local_timezone
                                    date_obj = date_obj.astimezone(_get_local_timezone())
                                formatted_releases[country].append({
                                    'date': date_obj.date().isoformat(),
                                    'type': release_type
                                })
                            except iso8601.ParseError:
                                logger.warning(
                                    "Could not parse release date: %s for movie %s (country=%s)",
                                    release_date_str, imdb_id, country
                                )
                            except Exception as e_date:
                                logger.error("Error processing release date %s for %s: %s", release_date_str, imdb_id, e_date)

                    movie_data_raw['release_dates'] = dict(formatted_releases)
                    logger.info("TraktMetadata._get_movie_data: Stored formatted release dates for %s (countries=%d)", imdb_id, len(formatted_releases))
                else:
                    logger.info("TraktMetadata._get_movie_data: No release dates found for %s via any endpoint", imdb_id)
            else:
                logger.warning(f"TraktMetadata._get_movie_data: Slug not found for IMDb {imdb_id}. Cannot fetch aliases or release dates by slug. Data: {movie_data_raw}")

            logger.info(f"TraktMetadata._get_movie_data: Successfully fetched and processed movie data for {imdb_id} using direct lookup.")
            return movie_data_raw
        elif response: # Handle non-200 responses for the direct movie lookup
            logger.warning(f"TraktMetadata._get_movie_data: Direct fetching movie data from {direct_url} failed with status {response.status_code}: {response.text}")
            if response.status_code == 404:
                 logger.warning(f"TraktMetadata._get_movie_data: Movie with IMDb ID {imdb_id} not found directly on Trakt (404). URL: {direct_url}")
        else: # No response from _make_request
            logger.warning(f"TraktMetadata._get_movie_data: Direct fetching movie data from {direct_url} received no response from _make_request.")
        
        # Fallback to search-based method if direct lookup fails and it's not a 404 or auth issue?
        # For now, if direct lookup fails, we return None.
        # The previous code using _search_by_imdb follows, now effectively a fallback or unused.
        # To avoid accidental execution of old logic, I am removing it from this flow.
        # If a fallback is desired, it needs to be explicitly designed.

        logger.warning(f"TraktMetadata._get_movie_data: Failed to get movie data for {imdb_id} via direct lookup. Previous search-based logic is now bypassed.")
        return None

    def get_show_seasons_and_episodes(self, imdb_id, include_specials: bool = False):
        # Get seasons data directly using IMDB ID
        url = f"{self.base_url}/shows/{imdb_id}/seasons?extended=full,episodes"
        logger.debug(f"Fetching seasons data from: {url} (Include Specials: {include_specials})")
        response = self._make_request(url)
        if response and response.status_code == 200:
            seasons_data_raw = response.json()
            logger.debug(f"Raw seasons response from Trakt for {imdb_id}: {json.dumps(seasons_data_raw)}")
            logger.debug(f"Raw seasons data received: {len(seasons_data_raw)} seasons")
            
            processed_seasons = {}
            for season in seasons_data_raw:
                season_number = season.get('number')
                if season_number is None:
                    logger.warning(f"Skipping season with null number for IMDb ID {imdb_id}")
                    continue

                if not include_specials and season_number == 0:
                    logger.debug(f"Skipping season 0 for {imdb_id} as include_specials is False.")
                    continue

                logger.debug(f"Processing season {season_number}")
                
                episodes = season.get('episodes', [])
                
                processed_seasons[season_number] = {
                    'episode_count': season.get('episode_count', len(episodes)),
                    'episodes': {}
                }
                
                for episode in episodes:
                    episode_number = episode.get('number')
                    if episode_number is None:
                        logger.warning(f"Episode in season {season_number} has no number: {json.dumps(episode, indent=2)}")
                        continue
                        
                    processed_seasons[season_number]['episodes'][episode_number] = {
                        'title': episode.get('title', ''),
                        'overview': episode.get('overview', ''),
                        'runtime': episode.get('runtime', 0),
                        'first_aired': episode.get('first_aired'),
                        'imdb_id': episode['ids'].get('imdb')
                    }
                
                logger.debug(f"Completed season {season_number} with {len(processed_seasons[season_number]['episodes'])} episodes")
            
            return processed_seasons, 'trakt'
            
        logger.warning(f"Failed to get seasons data. Status code: {response.status_code if response else 'No response'}")
        return None, None

    def _get_show_aliases(self, slug):
        """Get all aliases for a show using its Trakt slug"""
        url = f"{self.base_url}/shows/{slug}/aliases"
        response = self._make_request(url)
        if response and response.status_code == 200:
            aliases_data = response.json()
            # Process aliases into a more usable format
            aliases = defaultdict(list)
            for alias in aliases_data:
                country = alias.get('country', 'unknown')
                title = alias.get('title')
                if title:
                    aliases[country].append(title)
            return dict(aliases)
        return None

    def get_show_metadata(self, imdb_id):
        url = f"{self.base_url}/shows/{imdb_id}?extended=full"
        response = self._make_request(url)
        if response and response.status_code == 200:
            show_data = response.json()
            logger.debug(f"Initial show data received for {imdb_id}")
            
            # Get the show's slug and fetch aliases
            slug = show_data['ids']['slug']
            aliases = self._get_show_aliases(slug)
            if aliases:
                show_data['aliases'] = aliases
                logger.debug(f"Added aliases for {imdb_id}")
            
            logger.debug(f"Fetching seasons data for {imdb_id}")
            seasons_data, source = self.get_show_seasons_and_episodes(imdb_id)
            logger.debug(f"Received seasons data for {imdb_id}: {seasons_data is not None}")
            if seasons_data:
                logger.debug(f"Season numbers received: {list(seasons_data.keys())}")
                # Ensure seasons data is properly structured
                for season_num in seasons_data:
                    if 'episodes' not in seasons_data[season_num]:
                        logger.warning(f"Season {season_num} missing episodes key")
                        seasons_data[season_num]['episodes'] = {}
                    if 'episode_count' not in seasons_data[season_num]:
                        logger.warning(f"Season {season_num} missing episode_count key")
                        seasons_data[season_num]['episode_count'] = len(seasons_data[season_num].get('episodes', {}))
                logger.debug(f"Final seasons data structure: {json.dumps(seasons_data, indent=2)}")
            show_data['seasons'] = seasons_data
            return show_data
        return None

    def get_episode_metadata(self, episode_imdb_id):
        # First, check if we have the episode metadata cached
        if hasattr(self, 'cached_episodes') and episode_imdb_id in self.cached_episodes:
            return self.cached_episodes[episode_imdb_id]

        # If not cached, fetch the show data
        url = f"{self.base_url}/search/imdb/{episode_imdb_id}?type=episode"
        response = self._make_request(url)
        if response and response.status_code == 200:
            data = response.json()
            if data:
                episode_data = data[0]['episode']
                show_data = data[0]['show']
                show_imdb_id = show_data['ids']['imdb']

                # Fetch all episodes for this show
                all_episodes, _ = self.get_show_seasons_and_episodes(show_imdb_id)
                
                # Cache all episodes
                self.cached_episodes = all_episodes

                # Return the requested episode
                for season in all_episodes.values():
                    for episode in season['episodes'].values():
                        if episode['imdb_id'] == episode_imdb_id:
                            return {
                                'episode': episode,
                                'show': {
                                    'imdb_id': show_imdb_id,
                                    'metadata': show_data
                                }
                            }

        return None

    def get_show_episodes(self, imdb_id):
        url = f"{self.base_url}/shows/{imdb_id}/seasons?extended=full,episodes"
        response = self._make_request(url)
        if response and response.status_code == 200:
            seasons_data = response.json()
            processed_episodes = []
            for season in seasons_data:
                if season['number'] is not None and season['number'] > 0:
                    for episode in season.get('episodes', []):
                        first_aired = None
                        if episode.get('first_aired'):
                            try:
                                first_aired = iso8601.parse_date(episode['first_aired'])
                            except iso8601.ParseError:
                                logger.warning(
                                    f"Could not parse date: {episode['first_aired']} for episode {episode['number']} "
                                    f"of season {season['number']} in {imdb_id}"
                                )

                        processed_episodes.append({
                            'season': season['number'],
                            'episode': episode['number'],
                            'title': episode.get('title', ''),
                            'overview': episode.get('overview', ''),
                            'runtime': episode.get('runtime', 0),
                            'first_aired': first_aired,
                            'imdb_id': episode['ids'].get('imdb')
                        })
            return processed_episodes
        return None

    def refresh_metadata(self, imdb_id: str) -> Dict[str, Any]:
        """Refresh metadata for either a show or movie"""
        logger.debug(f"TraktMetadata: Refreshing metadata for {imdb_id}")
        metadata = self.get_metadata(imdb_id)
        if metadata:
            logger.debug(f"TraktMetadata: Successfully got metadata for {imdb_id}")
            return metadata
        else:
            logger.warning(f"TraktMetadata: Failed to get metadata for {imdb_id}")
            return None

    def get_movie_metadata(self, imdb_id, max_retries=3, retry_delay=5):
        return self._get_movie_data(imdb_id)

    def get_poster(self, imdb_id: str) -> str:
        return "Posters not available through Trakt API"

    def get_release_dates(self, imdb_id):
        """Get all release dates for a movie using its Trakt slug (more reliable)."""
        logger.info(f"Fetching release dates for movie IMDb ID: {imdb_id} by finding its slug.")
        
        # First search to get the movie's Trakt slug
        search_result = self._search_by_imdb(imdb_id)
        if not search_result or search_result.get('type') != 'movie':
            logger.warning(f"Could not find a movie with IMDb ID {imdb_id} via search.")
            return None
            
        movie = search_result.get('movie')
        if not movie:
            logger.warning(f"Search result for {imdb_id} did not contain a 'movie' object.")
            return None

        slug = movie.get('ids', {}).get('slug')
        if not slug:
            logger.warning(f"Could not find slug for {imdb_id} in search result.")
            return None

        logger.info(f"Found slug '{slug}' for IMDb ID {imdb_id}. Fetching releases.")
        url = f"{self.base_url}/movies/{slug}/releases"
        response = self._make_request(url)

        if response and response.status_code == 200:
            releases = response.json()
            if not releases:
                logger.warning(f"No release dates found for slug {slug} from {url} (empty list).")
                return None

            formatted_releases = defaultdict(list)
            for release in releases:
                country = release.get('country')
                release_date = release.get('release_date')
                release_type = release.get('release_type')
                if country and release_date:
                    try:
                        date = iso8601.parse_date(release_date)
                        if date.tzinfo is not None:
                            from metadata.metadata import _get_local_timezone
                            date = date.astimezone(_get_local_timezone())
                        formatted_releases[country].append({
                            'date': date.date().isoformat(),
                            'type': release_type
                        })
                    except iso8601.ParseError:
                        logger.warning(f"Could not parse date: {release_date} for {imdb_id} in {country}")
            return dict(formatted_releases)

        elif response:
            logger.warning(
                f"Failed to fetch release dates for slug {slug} from {url}. "
                f"Status: {response.status_code}, Response: {response.text}"
            )
            return None
        else: # No response
            logger.warning(f"No response from {url} when fetching release dates for slug {slug}.")
            return None

    def convert_tmdb_to_imdb(self, tmdb_id, media_type=None):
        """
        Convert TMDB ID to IMDB ID
        Args:
            tmdb_id: The TMDB ID to convert
            media_type: Either 'movie' or 'show' to specify what type of content to look for
        """
        url = f"{self.base_url}/search/tmdb/{tmdb_id}?type=movie,show"
        logger.debug(f"Making request to Trakt API: {url}")
        
        response = self._make_request(url)
        if response and response.status_code == 200:
            data = response.json()
            logger.debug(f"Received response from Trakt API: {json.dumps(data, indent=2)}")
            
            if data:
                for item in data:
                    logger.debug(f"Processing item from response: {json.dumps(item, indent=2)}")
                    
                    # If media_type is specified, only look for that type
                    if media_type:
                        if media_type == 'show' and 'show' in item:
                            logger.debug(f"Found show: {item['show']['title']} with IMDb ID: {item['show']['ids']['imdb']}")
                            return item['show']['ids']['imdb'], 'trakt'
                        elif media_type == 'movie' and 'movie' in item:
                            logger.debug(f"Found movie: {item['movie']['title']} with IMDb ID: {item['movie']['ids']['imdb']}")
                            return item['movie']['ids']['imdb'], 'trakt'
                    else:
                        # Fallback to original behavior if no media_type specified
                        if 'show' in item:
                            logger.debug(f"Found show: {item['show']['title']} with IMDb ID: {item['show']['ids']['imdb']}")
                            return item['show']['ids']['imdb'], 'trakt'
                        elif 'movie' in item:
                            logger.debug(f"Found movie: {item['movie']['title']} with IMDb ID: {item['movie']['ids']['imdb']}")
                            return item['movie']['ids']['imdb'], 'trakt'
                
                logger.warning(f"No matching {'media type' if media_type else 'content'} found for TMDB ID: {tmdb_id}")
            else:
                logger.warning("Received empty data array from Trakt API")
        else:
            logger.error(f"Failed to get response from Trakt API. Status code: {response.status_code if response else 'No response'}")
        
        return None, None
    
    def _get_movie_aliases(self, slug):
        """Get all aliases for a movie using its Trakt slug"""
        url = f"{self.base_url}/movies/{slug}/aliases"
        response = self._make_request(url)
        if response and response.status_code == 200:
            aliases_data = response.json()
            # Process aliases into a more usable format
            aliases = defaultdict(list)
            for alias in aliases_data:
                country = alias.get('country', 'unknown')
                title = alias.get('title')
                if title:
                    aliases[country].append(title)
            return dict(aliases)
        return None

    def search_media(self, query: str, year: Optional[int] = None, media_type: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """
        Search Trakt for movies or shows based on query, optionally filtering by year and type.
        Args:
            query: The search query (title).
            year: Optional year to filter by. (NO LONGER USED FOR API CALL)
            media_type: Optional type ('movie' or 'show') to filter by.
        Returns:
            A list of search result dictionaries, or None if an error occurs.
            Each dictionary contains keys like 'title', 'year', 'imdb_id', 'tmdb_id', 'type'.
        """
        logger.debug(f"Searching Trakt: query='{query}', year={year} (year not used in API call), type={media_type}")
        
        # Determine search type filter for URL
        search_type = media_type if media_type in ['movie', 'show'] else 'movie,show'
        
        # Construct URL parameters
        params = {'query': query, 'extended': 'full'}
        # if year: # <-- REMOVE OR COMMENT OUT THIS BLOCK
        #     params['years'] = str(year)
            
        encoded_params = urlencode(params)
        url = f"{self.base_url}/search/{search_type}?{encoded_params}"
        
        logger.debug(f"Constructed Trakt Search URL (without year filter): {url}")
        
        response = self._make_request(url)
        
        if response and response.status_code == 200:
            try:
                results_raw = response.json()
                logger.debug(f"Raw search results from Trakt: {results_raw}")
                
                processed_results = []
                if isinstance(results_raw, list):
                    for item_raw in results_raw:
                        item_type = item_raw.get('type')
                        item_data = item_raw.get(item_type) # Get 'movie' or 'show' object
                        
                        if item_type and item_data and isinstance(item_data, dict):
                            # Perform year filtering client-side IF a year was provided
                            if year and item_data.get('year') != year:
                                logger.debug(f"Skipping item '{item_data.get('title')}' (year {item_data.get('year')}) due to year mismatch with requested year {year}.")
                                continue

                            ids = item_data.get('ids', {})
                            processed_results.append({
                                'title': item_data.get('title'),
                                'year': item_data.get('year'),
                                'imdb_id': ids.get('imdb'),
                                'tmdb_id': ids.get('tmdb'),
                                'type': item_type
                            })
                        else:
                            logger.warning(f"Skipping invalid search result item: {item_raw}")
                            
                    logger.info(f"Processed {len(processed_results)} search results for query '{query}' (after potential client-side year filtering)")
                    return processed_results
                else:
                    logger.error(f"Trakt search response was not a list: {type(results_raw)}")
                    return None
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode JSON response from Trakt search: {e}")
                return None
            except Exception as e:
                 logger.error(f"Unexpected error processing Trakt search results: {e}", exc_info=True)
                 return None
        elif response: # Covers 500 errors and other non-200s if raise_for_status wasn't hit or was handled
            logger.warning(f"Trakt Search for '{query}' failed with status {response.status_code}: {response.text}")
            return None
        else: # No response from _make_request (e.g., network error, pre-request auth failure)
            logger.warning(f"Trakt Search for '{query}' received no response from _make_request.")
            return None

# Add this to your MetadataManager class
def refresh_trakt_metadata(self, imdb_id: str) -> None:
    trakt = TraktMetadata()
    new_metadata = trakt.refresh_metadata(imdb_id)
    if new_metadata:
        for key, value in new_metadata.items():
            self.add_or_update_metadata(imdb_id, key, value, 'Trakt')