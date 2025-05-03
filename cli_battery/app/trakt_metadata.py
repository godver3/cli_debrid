from .logger_config import logger
import json
import time
import os
import pickle
from typing import Dict, Any, List, Tuple
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

TRAKT_API_URL = "https://api.trakt.tv"
CACHE_FILE = 'db_content/trakt_last_activity.pkl'
REQUEST_TIMEOUT = 10  # seconds
trakt_auth = TraktAuth()

class TraktMetadata:
    def __init__(self):
        self.settings = Settings()
        self.base_url = "https://api.trakt.tv"
        self.trakt_auth = TraktAuth()
        self.request_times = deque()
        self.max_requests = 1000
        self.time_window = 300  # 5 minutes in seconds

    def _check_rate_limit(self):
        current_time = time.time()
        
        # Remove old requests from the deque
        while self.request_times and current_time - self.request_times[0] > self.time_window:
            self.request_times.popleft()
        
        # Check if we've hit the rate limit
        #if len(self.request_times) >= self.max_requests:
        #    logger.warning(f"Rate limit reached. Currently at {len(self.request_times)} requests in the last {self.time_window} seconds.")
        #    return False
        
        # Add the current request time
        self.request_times.append(current_time)
        return True

    def _make_request(self, url):
        if not self._check_rate_limit():
            logger.warning("Rate limit reached. Waiting for 5 minutes before retrying.")
            time.sleep(300)  # Wait for 5 minutes
            return self._make_request(url)  # Retry the request

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
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 401:
                logger.warning("Received 401 Unauthorized. Attempting to refresh token.")
                if self.trakt_auth.refresh_access_token():
                    # Update the header with the new token and retry the request
                    headers['Authorization'] = f'Bearer {self.trakt_auth.access_token}'
                    response = requests.get(url, headers=headers, timeout=10)
                else:
                    logger.error("Failed to refresh Trakt access token after 401 error.")
                    return None
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to Trakt API: {e}")
            logger.error(f"URL: {url}")
            logger.error(f"Headers: {headers}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status code: {e.response.status_code}")
                logger.error(f"Response text: {e.response.text}")
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
            if results:
                logger.debug(f"Trakt Search: Taking first result for {imdb_id}: {results[0]}")
                return results[0]  # Return first match
        elif response:
            logger.warning(f"Trakt Search for {imdb_id} failed with status {response.status_code}: {response.text}")
        else:
            logger.warning(f"Trakt Search for {imdb_id} received no response.")
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
        # First search to get the movie's Trakt slug and ID
        search_result = self._search_by_imdb(imdb_id)
        if not search_result or search_result['type'] != 'movie':
            logger.debug(f"Trakt: Search for IMDB {imdb_id} did not return a movie. Result: {search_result}")
            return None
            
        movie = search_result['movie']
        trakt_id = movie['ids']['trakt']
        slug = movie['ids']['slug']
        if not trakt_id:
            logger.error(f"Trakt: Missing trakt ID in search result for IMDb {imdb_id}. Result: {search_result}")
            return None
        
        # Now get the full movie data using the Trakt ID
        url = f"{self.base_url}/movies/{trakt_id}?extended=full"
        logger.debug(f"Trakt: Fetching full movie data using Trakt ID '{trakt_id}' with URL: {url}")
        response = self._make_request(url)
        if response and response.status_code == 200:
            movie_data = response.json()
            logger.debug(f"Trakt: Raw response from {url}: {movie_data}")
            returned_imdb_id = movie_data.get('ids', {}).get('imdb')
            if returned_imdb_id != imdb_id:
                logger.error(f"Trakt API Mismatch! Requested IMDb {imdb_id} but /movies/{trakt_id} endpoint returned data for IMDb {returned_imdb_id}. Raw Data: {movie_data}")
                return None
            
            # Get aliases and add them to the movie data
            aliases = self._get_movie_aliases(slug)
            if aliases:
                movie_data['aliases'] = aliases

            # Get release dates and add them to the movie data
            release_dates = self.get_release_dates(imdb_id)
            if release_dates:
                movie_data['release_dates'] = release_dates
                
            return movie_data
        elif response:
            logger.warning(f"Trakt: Fetching full movie data from {url} (using Trakt ID {trakt_id}) failed with status {response.status_code}: {response.text}")
        else:
            logger.warning(f"Trakt: Fetching full movie data from {url} (using Trakt ID {trakt_id}) received no response.")
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
        # First search to get the movie's Trakt slug
        search_result = self._search_by_imdb(imdb_id)
        if not search_result or search_result['type'] != 'movie':
            return None
            
        movie = search_result['movie']
        slug = movie['ids']['slug']

        url = f"{self.base_url}/movies/{slug}/releases"
        response = self._make_request(url)
        if response and response.status_code == 200:
            releases = response.json()
            formatted_releases = defaultdict(list)
            for release in releases:
                country = release.get('country')
                release_date = release.get('release_date')
                release_type = release.get('release_type')
                if country and release_date:
                    try:
                        date = iso8601.parse_date(release_date)
                        # Convert to local timezone if necessary
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

# Add this to your MetadataManager class
def refresh_trakt_metadata(self, imdb_id: str) -> None:
    trakt = TraktMetadata()
    new_metadata = trakt.refresh_metadata(imdb_id)
    if new_metadata:
        for key, value in new_metadata.items():
            self.add_or_update_metadata(imdb_id, key, value, 'Trakt')