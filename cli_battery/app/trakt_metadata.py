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

TRAKT_API_URL = "https://api.trakt.tv"
CACHE_FILE = 'db_content/trakt_last_activity.pkl'
REQUEST_TIMEOUT = 10  # seconds
trakt_auth = TraktAuth()

class TraktMetadata:
    def __init__(self):
        self.settings = Settings()
        self.base_url = "https://api.trakt.tv"
        self.trakt_auth = TraktAuth()
        self.client_id = self.settings.Trakt.get('client_id')
        self.client_secret = self.settings.Trakt.get('client_secret')
        self.redirect_uri = "http://192.168.1.51:5001/trakt_callback"

    def _make_request(self, url):

        if not self.trakt_auth.is_authenticated():
            if not self.trakt_auth.refresh_access_token():
                logger.error("Failed to authenticate with Trakt.")
                return None

        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id,
            'Authorization': f'Bearer {self.trakt_auth.access_token}'
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
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


    def fetch_items_from_trakt(self, endpoint: str) -> List[Dict[str, Any]]:
        if not self.headers:
            return []

        full_url = f"{TRAKT_API_URL}{endpoint}"

        try:
            response = requests.get(full_url, headers=self.headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching items from Trakt: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response text: {e.response.text}")
            return []

    def get_metadata(self, imdb_id: str) -> Dict[str, Any]:
        show_data = self._get_show_data(imdb_id)
        if show_data:
            return {
                'type': 'show',
                'metadata': show_data
            }

        movie_data = self._get_movie_data(imdb_id)
        if movie_data:
            movie_metadata = {
                'type': 'movie',
                'metadata': movie_data
            }
            # Add release dates to the metadata
            release_dates = self.get_release_dates(imdb_id)
            if release_dates:
                movie_metadata['metadata']['release_dates'] = release_dates
            return movie_metadata

        return None

    def _get_show_data(self, imdb_id):
        url = f"{self.base_url}/shows/{imdb_id}?extended=full"
        response = self._make_request(url)
        if response and response.status_code == 200:
            return response.json()
        return None

    def _get_movie_data(self, imdb_id):
        url = f"{self.base_url}/movies/{imdb_id}?extended=full"
        response = self._make_request(url)
        if response and response.status_code == 200:
            return response.json()
        return None

    def get_show_seasons_and_episodes(self, imdb_id):
        url = f"{self.base_url}/shows/{imdb_id}/seasons?extended=full,episodes"
        response = self._make_request(url)
        if response and response.status_code == 200:
            seasons_data = response.json()
            processed_seasons = {}
            for season in seasons_data:
                if season['number'] is not None and season['number'] > 0:
                    season_number = season['number']
                    processed_seasons[season_number] = {
                        'episode_count': season.get('episode_count', 0),
                        'episodes': {}
                    }
                    for episode in season.get('episodes', []):
                        episode_number = episode['number']
                        processed_seasons[season_number]['episodes'][episode_number] = {
                            'title': episode.get('title', ''),
                            'overview': episode.get('overview', ''),
                            'runtime': episode.get('runtime', 0),
                            'first_aired': episode.get('first_aired'),
                            'imdb_id': episode['ids'].get('imdb')
                        }
            return processed_seasons, 'trakt'
        return None, None

    def get_show_metadata(self, imdb_id):
        url = f"{self.base_url}/shows/{imdb_id}?extended=full"
        response = self._make_request(url)
        if response and response.status_code == 200:
            show_data = response.json()
            seasons_data, _ = self.get_show_seasons_and_episodes(imdb_id)
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
                _, all_episodes = self.get_show_seasons_and_episodes(show_imdb_id)
                
                # Cache all episodes
                self.cached_episodes = all_episodes

                # Return the requested episode
                if episode_imdb_id in self.cached_episodes:
                    return {
                        'episode': self.cached_episodes[episode_imdb_id],
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
                            'imdb_id': episode['ids'].get('imdb')  # Include the IMDb ID
                        })
            return processed_episodes
        return None


    def refresh_metadata(self, imdb_id: str) -> Dict[str, Any]:
        return self.get_metadata(imdb_id)

    def get_movie_metadata(self, imdb_id, max_retries=3, retry_delay=5):
        headers = {
            'Content-Type': 'application/json',
            'trakt-api-version': '2',
            'trakt-api-key': self.client_id,
            'Authorization': f'Bearer {self.settings.Trakt["access_token"]}'
        }
        
        url = f"{self.base_url}/movies/{imdb_id}?extended=full"
        
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:  # Too Many Requests
                    retry_after = int(response.headers.get('Retry-After', retry_delay))
                    logger.warning(f"Rate limited by Trakt API. Retrying after {retry_after} seconds.")
                    time.sleep(retry_after)
                else:
                    logger.error(f"Failed to fetch movie metadata from Trakt: Status {response.status_code}, Response: {response.text}")
                    
            except RequestException as e:
                logger.error(f"Request exception when fetching movie metadata: {str(e)}")
            
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds... (Attempt {attempt + 1} of {max_retries})")
                time.sleep(retry_delay)
        
        logger.error(f"Failed to fetch movie metadata after {max_retries} attempts")
        return None

    def get_poster(self, imdb_id: str) -> str:
        return "Posters not available through Trakt API"

    def get_release_dates(self, imdb_id):
        url = f"{self.base_url}/movies/{imdb_id}/releases"
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
                        # Convert to UTC if necessary
                        if date.tzinfo is not None:
                            date = date.astimezone(timezone.utc)
                        formatted_releases[country].append({
                            'date': date.date().isoformat(),
                            'type': release_type
                        })
                    except iso8601.ParseError:
                        logger.warning(f"Could not parse date: {release_date} for {imdb_id} in {country}")
            return dict(formatted_releases)
        return None

    def convert_tmdb_to_imdb(self, tmdb_id):
        url = f"{self.base_url}/search/tmdb/{tmdb_id}?type=movie,show"
        response = self._make_request(url)
        if response and response.status_code == 200:
            data = response.json()
            if data:

                item = data[0]
                if 'movie' in item:
                    return item['movie']['ids']['imdb'], 'trakt'
                elif 'show' in item:
                    return item['show']['ids']['imdb'], 'trakt'
        return None, None
    
# Add this to your MetadataManager class
def refresh_trakt_metadata(self, imdb_id: str) -> None:
    trakt = TraktMetadata()
    new_metadata = trakt.refresh_metadata(imdb_id)
    if new_metadata:
        for key, value in new_metadata.items():
            self.add_or_update_metadata(imdb_id, key, value, 'Trakt')