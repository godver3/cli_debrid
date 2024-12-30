import logging
from typing import Dict, Any, List, Tuple, Optional
from settings import get_setting
from api_tracker import api
from scraper.scraper import scrape
from debrid import extract_hash_from_magnet
from queues.adding_queue import AddingQueue
import re
from fuzzywuzzy import fuzz
from poster_cache import get_cached_poster_url, cache_poster_url, get_cached_media_meta, cache_media_meta
from metadata.metadata import get_metadata, get_imdb_id_if_missing, get_all_season_episode_counts, get_show_airtime_by_imdb_id
import asyncio
import aiohttp
from database.poster_management import get_poster_url
from flask import request, url_for
from urllib.parse import urlparse
from debrid.base import DebridProvider
from debrid import get_debrid_provider

def search_trakt(search_term: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }

    search_url = f"https://api.trakt.tv/search/movie,show?query={api.utils.quote(search_term)}&extended=full"

    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data:
            # Sort results based on year match and title similarity
            sorted_results = sorted(
                data,
                key=lambda x: (
                    (str(x['movie' if x['type'] == 'movie' else 'show']['year']) == str(year) if year else False),
                    fuzz.ratio(search_term.lower(), x['movie' if x['type'] == 'movie' else 'show']['title'].lower()),
                    x['movie' if x['type'] == 'movie' else 'show'].get('votes', 0)
                ),
                reverse=True
            )
            
            # Convert Trakt results and include poster paths
            converted_results = []
            for result in sorted_results:
                media_type = result['type']
                item = result['movie' if media_type == 'movie' else 'show']
                
                tmdb_id = item['ids']['tmdb']
                cached_poster_url = get_cached_poster_url(tmdb_id, media_type)
                cached_media_meta = get_cached_media_meta(tmdb_id, media_type)

                if cached_poster_url and cached_media_meta:
                    poster_path = cached_poster_url
                    media_meta = cached_media_meta
                else:
                    logging.info(f"Fetching data for {media_type} {item['title']} (TMDB ID: {tmdb_id})")
                    media_meta = get_media_meta(tmdb_id, media_type)
                    if media_meta:
                        poster_path = media_meta[0]
                        cache_poster_url(tmdb_id, media_type, poster_path)
                        cache_media_meta(tmdb_id, media_type, media_meta)
                        logging.info(f"Cached poster and metadata for {media_type} {item['title']} (TMDB ID: {tmdb_id})")
                    else:
                        poster_path = None
                        media_meta = (None, '', [], 0, '')

                converted_results.append({
                    'mediaType': media_type,
                    'id': tmdb_id,
                    'title': item['title'],
                    'year': item['year'],
                    'posterPath': poster_path,
                    'overview': media_meta[1],
                    'genres': media_meta[2],
                    'voteAverage': media_meta[3],
                    'backdropPath': media_meta[4]
                })
            
            logging.info(f"Sorted results: {converted_results[:5]}")  # Log top 5 results for debugging
            return converted_results
        else:
            logging.warning(f"No results found for search term: {search_term}")
            return []
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Trakt: {e}")
        return []

def get_media_meta(tmdb_id: str, media_type: str) -> Optional[Tuple[str, str, list, float, str]]:
    tmdb_api_key = get_setting('TMDB', 'api_key')
    
    if not tmdb_api_key:
        logging.error("TMDb API key not set. Please configure in settings.")
        return None

    cached_poster_url = get_cached_poster_url(tmdb_id, media_type)
    cached_media_meta = get_cached_media_meta(tmdb_id, media_type)

    if cached_poster_url and cached_media_meta:
        logging.info(f"Using cached data for {media_type} (TMDB ID: {tmdb_id})")
        return cached_media_meta

    # Use the correct endpoints for TV shows and movies
    if media_type == 'tv' or media_type == 'show':
        details_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    else:
        details_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={tmdb_api_key}&language=en-US"
    
    try:
        # Fetch details
        details_response = api.get(details_url)
        details_response.raise_for_status()
        details_data = details_response.json()

        # Use asyncio to run the async get_poster_url function
        async def fetch_poster_url():
            async with aiohttp.ClientSession() as session:
                return await get_poster_url(session, tmdb_id, media_type)

        poster_url = asyncio.run(fetch_poster_url())
        
        overview = details_data.get('overview', '')
        genres = [genre['name'] for genre in details_data.get('genres', [])]
        vote_average = details_data.get('vote_average', 0)
        backdrop_path = details_data.get('backdrop_path', '')
        if backdrop_path:
            backdrop_path = f"https://image.tmdb.org/t/p/original{backdrop_path}"

        media_meta = (poster_url, overview, genres, vote_average, backdrop_path)
        
        if poster_url:
            cache_poster_url(tmdb_id, media_type, poster_url)
        cache_media_meta(tmdb_id, media_type, media_meta)
        logging.info(f"Cached metadata for {media_type} (TMDB ID: {tmdb_id})")

        return media_meta
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching media meta from TMDb: {e}")
        return None
    
def overseerr_tvshow(title: str, year: Optional[int] = None, media_id: Optional[int] = None, season: Optional[int] = None) -> List[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    if media_id and season is not None:
        search_url = f"{overseerr_url}/api/v1/tv/{media_id}/season/{season}"
    else:
        search_url = f"{overseerr_url}/api/v1/tv/{media_id}"


    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data.get('seasons', False):
            # Grab TV show seasons
            seasons = data['seasons']
            
            logging.info(f"Sorted seasons: {seasons}")
            return seasons
        elif data.get('episodes', False):
            # Grab TV show seasons
            episodes = data['episodes']
            
            logging.info(f"Sorted episodes: {episodes}")
            return episodes
        else:
            logging.warning(f"No results found for show: {title}")
            return []
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []
    
def parse_search_term(search_term: str) -> Tuple[str, Optional[int], Optional[int], Optional[int], bool]:
    # First, check if the entire search term is a year
    if re.match(r'^(19\d{2}|20\d{2})$', search_term):
        return search_term, None, None, None, True

    # Try to match a year at the end of the string
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b\s*$', search_term)
    if year_match:
        year = int(year_match.group(1))
        base_title = search_term[:year_match.start()].strip()
    else:
        year = None
        base_title = search_term

    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', base_title)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        base_title = re.sub(r'[Ss]\d+(?:[Ee]\d+)?', '', base_title).strip()
        multi = episode is None  # Set multi to True if only season is specified
        return base_title, season, episode, year, multi

    return base_title, None, None, year, True  # Default to multi=True if no season/episode specified

async def fetch_poster_url(tmdb_id, media_type):
    async with aiohttp.ClientSession() as session:
        return await get_poster_url(session, tmdb_id, media_type)

def web_scrape(search_term: str, version: str) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for search term: {search_term}, version: {version}")
    
    base_title, season, episode, year, multi = parse_search_term(search_term)
    logging.info(f"Parsed search term: title='{base_title}', season={season}, episode={episode}, year={year}, multi={multi}")
    
    search_results = search_trakt(base_title, year)
    if not search_results:
        logging.warning(f"No results found for search term: {base_title} ({year if year else 'no year specified'})")
        return {"error": "No results found"}

    logging.info(f"Found results: {search_results}")

    detailed_results = []
    for result in search_results:
        if result['mediaType'] != 'person' and result['posterPath'] is not None:
            tmdb_id = result['id']
            media_type = result['mediaType']
            logging.info(f"Processing media tmdb_id: {tmdb_id}")
            logging.info(f"Processing media type: {media_type}")

            cached_poster_url = get_cached_poster_url(tmdb_id, media_type)
            cached_media_meta = get_cached_media_meta(tmdb_id, media_type)

            if cached_poster_url and cached_media_meta:
                logging.info(f"Using cached data for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, media_type)
                if media_meta:
                    poster_path = asyncio.run(fetch_poster_url(tmdb_id, media_type))
                    cache_poster_url(tmdb_id, media_type, poster_path)
                    cache_media_meta(tmdb_id, media_type, media_meta)
                    logging.info(f"Cached poster and metadata for {media_type} {result['title']} (TMDB ID: {tmdb_id})")
                else:
                    poster_path = None
                    media_meta = (None, '', [], 0, '')

            logging.info(f"Genres for {result['title']}: {media_meta[2]}")

            detailed_result = {
                "id": tmdb_id,
                "title": result['title'],
                "year": result['year'],
                "media_type": media_type,
                "show_overview": media_meta[1],
                "poster_path": poster_path,
                "genre_ids": media_meta[2],
                "vote_average": media_meta[3],
                "backdrop_path": media_meta[4],
                "season": season,
                "episode": episode,
                "multi": multi,
                "imdb_id": result.get('imdb_id', '')
            }
            detailed_results.append(detailed_result)

    logging.info(f"Processed results: {detailed_results}")
    return detailed_results

def get_tmdb_data(tmdb_id: int, media_type: str, season: Optional[int] = None, episode: Optional[int] = None) -> Dict[str, Any]:
    tmdb_api_key = get_setting('TMDB', 'api_key')
    
    if not tmdb_api_key:
        logging.error("TMDb API key not set. Please configure in settings.")
        return {}

    base_url = "https://api.themoviedb.org/3"
    if media_type == 'tv' or media_type == 'show':
        if season is not None and episode is not None:
            url = f"{base_url}/tv/{tmdb_id}/season/{season}/episode/{episode}?api_key={tmdb_api_key}"
        elif season is not None:
            url = f"{base_url}/tv/{tmdb_id}/season/{season}?api_key={tmdb_api_key}"
        else:
            url = f"{base_url}/tv/{tmdb_id}?api_key={tmdb_api_key}"
    else:
        url = f"{base_url}/movie/{tmdb_id}?api_key={tmdb_api_key}"

    try:
        response = api.get(url)
        response.raise_for_status()
        data = response.json()
        return data
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching TMDb data: {e}")
        return {}

def web_scrape_tvshow(media_id: int, title: str, year: int, season: Optional[int] = None) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for TV Show: {title}, media_id: {media_id}")
    
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID not set. Please configure in settings.")
        return {"error": "Trakt Client ID not set"}

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }

    # First, convert TMDB ID to Trakt ID
    tmdb_to_trakt_url = f"https://api.trakt.tv/search/tmdb/{media_id}?type=show"
    
    try:
        response = api.get(tmdb_to_trakt_url, headers=headers)
        response.raise_for_status()
        search_data = response.json()
        
        if not search_data:
            logging.error(f"No Trakt show found for TMDB ID: {media_id}")
            return {"error": "Show not found on Trakt"}
        
        trakt_id = search_data[0]['show']['ids']['trakt']
    except api.exceptions.RequestException as e:
        logging.error(f"Error converting TMDB ID to Trakt ID: {e}")
        return {"error": f"Error converting TMDB ID to Trakt ID: {str(e)}"}

    # Now use the Trakt ID for further requests
    if season is not None:
        search_url = f"https://api.trakt.tv/shows/{trakt_id}/seasons/{season}?extended=full,episodes"
    else:
        search_url = f"https://api.trakt.tv/shows/{trakt_id}/seasons?extended=full"

    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        trakt_data = response.json()

        if not trakt_data:
            logging.warning(f"No results found for show: {title}")
            return {"error": "No results found"}

        logging.info(f"Found results: {trakt_data}")

        # Fetch TMDB data
        tmdb_data = get_tmdb_data(media_id, 'tv', season)

        if season is not None:
            return {
                "episodeResults": [
                    {
                        "id": media_id,
                        "title": title,
                        "episode_title": episode.get('title', ''),
                        "season_id": episode['ids']['trakt'],
                        "season_num": episode['season'],
                        "episode_num": episode['number'],
                        "year": year,
                        "media_type": 'tv',
                        "still_path": get_tmdb_data(media_id, 'tv', episode['season'], episode['number']).get('still_path'),
                        "air_date": episode.get('first_aired'),
                        "vote_average": episode.get('rating', 0),
                        "multi": False
                    }
                    for episode in trakt_data
                    if episode.get('first_aired') is not None
                    if episode['number'] != 0
                ]
            }
        else:
            return {
                "results": [
                    {
                        "id": media_id,
                        "title": title,
                        "season_id": season['ids']['trakt'],
                        "season_num": season['number'],
                        "year": year,
                        "media_type": 'tv',
                        "poster_path": tmdb_data.get('poster_path'),
                        "air_date": season.get('first_aired'),
                        "season_overview": season.get('overview', ''),
                        "episode_count": season.get('episode_count', 0),
                        "multi": True
                    }
                    for season in trakt_data
                    if season.get('first_aired') is not None
                    if season['number'] != 0
                ]
            }
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Trakt: {e}")
        return {"error": f"Error searching Trakt: {str(e)}"}

def trending_movies():
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID key not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }
    api_url = "https://api.trakt.tv/movies/watched/weekly?extended=full"

    try:
        response = api.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        trending_movies = []
        for result in data:
            tmdb_id = result['movie']['ids']['tmdb']
            cached_poster_url = get_cached_poster_url(tmdb_id, 'movie')
            cached_media_meta = get_cached_media_meta(tmdb_id, 'movie')

            if cached_poster_url and cached_media_meta:
                #logging.info(f"Using cached data for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, 'movie')
                if media_meta:
                    poster_path = media_meta[0]
                    cache_poster_url(tmdb_id, 'movie', poster_path)
                    cache_media_meta(tmdb_id, 'movie', media_meta)
                    logging.info(f"Cached poster and metadata for movie {result['movie']['title']} (TMDB ID: {tmdb_id})")
                else:
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        poster_path = placeholder_url

            # Check if TMDB API key is set
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            tmdb_api_key_set = bool(tmdb_api_key)

            trending_movies.append({
                "title": result['movie']['title'],
                "year": result['movie']['year'],
                "imdb_id": result['movie']['ids']['imdb'],
                "tmdb_id": tmdb_id,
                "rating": result['movie']['rating'],
                "watcher_count": result['watcher_count'],
                "poster_path": poster_path,
                "movie_overview": media_meta[1] if media_meta else '',
                "genre_ids": media_meta[2] if media_meta else [],
                "vote_average": media_meta[3] if media_meta else 0,
                "backdrop_path": media_meta[4] if media_meta else '',
                "tmdb_api_key_set": tmdb_api_key_set
            })

        return {"trendingMovies": trending_movies}
    except api.exceptions.RequestException as e:
        logging.error(f"Error retrieving Trakt Trending Movies: {e}")
        return []

def trending_shows():
    trakt_client_id = get_setting('Trakt', 'client_id')
    
    if not trakt_client_id:
        logging.error("Trakt Client ID key not set. Please configure in settings.")
        return []

    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': trakt_client_id
    }
    api_url = "https://api.trakt.tv/shows/watched/weekly?extended=full"

    try:
        response = api.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        trending_shows = []
        for result in data:
            tmdb_id = result['show']['ids']['tmdb']
            cached_poster_url = get_cached_poster_url(tmdb_id, 'tv')
            cached_media_meta = get_cached_media_meta(tmdb_id, 'tv')

            if cached_poster_url and cached_media_meta:
                #logging.info(f"Using cached data for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                poster_path = cached_poster_url
                media_meta = cached_media_meta
            else:
                logging.info(f"Fetching data for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                media_meta = get_media_meta(tmdb_id, 'tv')
                if media_meta:
                    poster_path = media_meta[0]
                    cache_poster_url(tmdb_id, 'tv', poster_path)
                    cache_media_meta(tmdb_id, 'tv', media_meta)
                    logging.info(f"Cached poster and metadata for show {result['show']['title']} (TMDB ID: {tmdb_id})")
                else:
                    if get_setting('TMDB', 'api_key') == "":
                        logging.warning("TMDB API key not set, using placeholder images")
                        
                        # Generate the placeholder URL
                        placeholder_url = url_for('static', filename='images/placeholder.png', _external=True)
                        
                        # Check if the request is secure (HTTPS)
                        if request.is_secure:
                            # If it's secure, ensure the URL uses HTTPS
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='https').geturl()
                        else:
                            # If it's not secure, use HTTP
                            parsed_url = urlparse(placeholder_url)
                            placeholder_url = parsed_url._replace(scheme='http').geturl()
                        
                        poster_path = placeholder_url
                    
            # Check if TMDB API key is set
            tmdb_api_key = get_setting('TMDB', 'api_key', '')
            tmdb_api_key_set = bool(tmdb_api_key)

            trending_shows.append({
                "title": result['show']['title'],
                "year": result['show']['year'],
                "imdb_id": result['show']['ids']['imdb'],
                "tmdb_id": tmdb_id,
                "rating": result['show']['rating'],
                "watcher_count": result['watcher_count'],
                "poster_path": poster_path,
                "show_overview": media_meta[1] if media_meta else '',
                "genre_ids": media_meta[2] if media_meta else [],
                "vote_average": media_meta[3] if media_meta else 0,
                "backdrop_path": media_meta[4] if media_meta else '',
                "tmdb_api_key_set": tmdb_api_key_set
            })

        return {"trendingShows": trending_shows}
    except api.exceptions.RequestException as e:
        logging.error(f"Error retrieving Trakt Trending Shows: {e}")
        return []

def process_media_selection(media_id: str, title: str, year: str, media_type: str, season: Optional[int], episode: Optional[int], multi: bool, version: str, genres: List[str]) -> List[Dict[str, Any]]:
    logging.info(f"Processing media selection: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}, genres={genres}")

    # Convert TMDB ID to IMDB ID using the metadata battery
    tmdb_id = int(media_id)
    metadata = get_metadata(tmdb_id=tmdb_id, item_media_type=media_type)
    imdb_id = metadata.get('imdb_id')

    if not imdb_id:
        # Try to get IMDb ID directly from our database mapping
        from cli_battery.app.direct_api import DirectAPI
        imdb_id, _ = DirectAPI.tmdb_to_imdb(str(tmdb_id), media_type='show' if media_type == 'tv' else media_type)

    if not imdb_id:
        logging.error(f"Could not find IMDB ID for TMDB ID {tmdb_id}")
        return [], []  # Return empty lists for both torrent results and cache status

    movie_or_episode = 'episode' if media_type == 'tv' or media_type == 'show' else 'movie'

    # Adjust multi flag based on season and episode
    if movie_or_episode == 'movie':
        multi = False
    elif season is not None and episode is None:
        multi = True

    logging.info(f"Adjusted scraping parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, "
                 f"movie_or_episode={movie_or_episode}, season={season}, episode={episode}, multi={multi}, version={version}")

    logging.info(f"Genres: {genres}")

    # Call the scraper function with the version parameter
    scrape_results, filtered_out_results = scrape(imdb_id, str(tmdb_id), title, int(year), movie_or_episode, version, season, episode, multi, genres)

    for result in scrape_results:
        logging.info(f"Scrape result: {result}")

    # Process the results
    processed_results = []
    hashes = []
    for result in scrape_results:
        if isinstance(result, dict):
            magnet_link = result.get('magnet')
            if magnet_link:
                if 'magnet:?xt=urn:btih:' in magnet_link:
                    magnet_hash = extract_hash_from_magnet(magnet_link)
                    result['hash'] = magnet_hash
                    # Only add to hashes if the source is not Jackett or Prowlarr
                    if result.get('source') not in ['jackett', 'prowlarr']:
                        hashes.append(magnet_hash)
                processed_results.append(result)

    # Get the debrid provider and check if it supports direct cache checking
    debrid_provider = get_debrid_provider()
    supports_cache_check = debrid_provider.supports_direct_cache_check

    # Check cache status for all hashes at once if supported
    cache_status = {}
    if supports_cache_check and hashes:
        cache_status = debrid_provider.is_cached(hashes)
        logging.info(f"Cache status returned: {cache_status}")

    # Update processed_results with cache status
    for result in processed_results:
        result_hash = result.get('hash')
        if result_hash:
            if result.get('source') in ['jackett', 'prowlarr'] or not supports_cache_check:
                result['cached'] = 'N/A'
            elif result_hash in cache_status:
                is_cached = cache_status[result_hash]
                result['cached'] = 'Yes' if is_cached else 'No'
            logging.info(f"Cache status for {result['title']} (hash: {result_hash}): {result['cached']}")

    return processed_results, cache_status

def get_available_versions():
    scraping_versions = get_setting('Scraping', 'versions', default={})
    return list(scraping_versions.keys())

def get_media_details(media_id: str, media_type: str) -> Dict[str, Any]:
    logging.info(f"Fetching media details for ID: {media_id}, Type: {media_type}")

    # If media_id is a TMDB ID, convert it to IMDb ID
    if media_type == 'movie':
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(media_id)})
    else:
        imdb_id = get_imdb_id_if_missing({'tmdb_id': int(media_id)})

    if not imdb_id:
        logging.error(f"Could not find IMDB ID for TMDB ID: {media_id}")
        return {}

    # Fetch metadata using the IMDb ID
    metadata = get_metadata(imdb_id=imdb_id, item_media_type=media_type)

    if not metadata:
        logging.error(f"Could not fetch metadata for IMDb ID: {imdb_id}")
        return {}

    # Add additional details that might be needed
    metadata['media_type'] = media_type
    if media_type == 'tv':
        metadata['seasons'] = get_all_season_episode_counts(imdb_id)
        metadata['airtime'] = get_show_airtime_by_imdb_id(imdb_id)

    logging.info(f"Successfully fetched media details for {metadata.get('title', 'Unknown Title')}")
    return metadata

def parse_season_episode(search_term: str) -> Tuple[int, int, bool]:
    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', search_term)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else 1
        multi = not bool(match.group(2))  # If episode is not specified, set multi to True
        return season, episode, multi
    return 1, 1, True  # Default to S01E01 with multi=True if no match
    
def process_torrent_selection(torrent_index: int, torrent_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    logging.info(f"Processing torrent selection: {torrent_index}")
    
    if 0 <= torrent_index < len(torrent_results):
        selected_torrent = torrent_results[torrent_index]
        magnet_link = selected_torrent.get('magnet')
        
        if magnet_link:
            logging.info(f"Selected torrent: {selected_torrent}")
            logging.info(f"Magnet link: {magnet_link}")
            result = debrid_provider.add_to_debrid(magnet_link)
            if result:
                logging.info(f"Torrent result: {result}")
                if result == 'downloading' or result == 'queued':
                    logging.info("Uncached torrent added to Real-Debrid successfully")
                    return {
                        "success": True,
                        "message": "Uncached torrent added to Real-Debrid successfully",
                        "torrent_info": selected_torrent
                    }
                else:
                    logging.info("Cached torrent added to Real-Debrid successfully")
                    return {
                        "success": True,
                        "message": "Cached torrent added to Real-Debrid successfully",
                        "torrent_info": selected_torrent
                    }
            else:
                logging.error("Failed to add torrent to Real-Debrid")
                return {
                    "success": False,
                    "error": "Failed to add torrent to Real-Debrid"
                }
        else:
            logging.error("No magnet link found for the selected torrent")
            return {
                "success": False,
                "error": "No magnet link found for the selected torrent"
            }
    else:
        logging.error(f"Invalid torrent index: {torrent_index}")
        return {
            "success": False,
            "error": "Invalid torrent index"
        }

def parse_torrent_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed_results = []
    for result in results:
        parsed_result = {
            "name": result.get("name", "Unknown"),
            "size": result.get("size", "Unknown"),
            "seeders": result.get("seeders", 0),
            "magnet": result.get("magnet", "")
        }
        parsed_results.append(parsed_result)
    return parsed_results