import logging
from typing import Dict, Any, List, Tuple, Optional
from settings import get_setting
from api_tracker import api
from scraper.scraper import scrape
from debrid.real_debrid import extract_hash_from_magnet, add_to_real_debrid, is_cached_on_rd
from queues.adding_queue import AddingQueue
import re
from fuzzywuzzy import fuzz
from poster_cache import get_cached_poster_url, cache_poster_url, get_cached_media_meta, cache_media_meta
from metadata.metadata import get_overseerr_movie_details, get_overseerr_show_details, get_overseerr_cookies

def search_overseerr(search_term: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    search_url = f"{overseerr_url}/api/v1/search?query={api.utils.quote(search_term)}"

    try:
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()

        if data['results']:
            # Sort results based on year match and title similarity
            sorted_results = sorted(
                data['results'],
                key=lambda x: (
                    # Prioritize exact year matches
                    (x.get('releaseDate', '')[:4] == str(year) if year else False),
                    # Then by title similarity
                    fuzz.ratio(search_term.lower(), (x.get('title', '') or x.get('name', '')).lower()),
                    # Then by popularity
                    x.get('popularity', 0)
                ),
                reverse=True
            )
            
            logging.info(f"Sorted results: {sorted_results[:5]}")  # Log top 5 results for debugging
            return sorted_results
        else:
            logging.warning(f"No results found for search term: {search_term}")
            return []
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []
    
def overseerr_genre(ids: str) -> List[Dict[str, Any]]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return []

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    search_url = f"{overseerr_url}/api/v1/genres/tv"


    try:
        genresnames = []
        response = api.get(search_url, headers=headers)
        response.raise_for_status()
        data = response.json()
        for genres in data:
            for idx in ids:
                if genres['id'] == idx:
                   genresnames.append(genres['name'])
        return genresnames
    except api.exceptions.RequestException as e:
        logging.error(f"Error searching Overseerr: {e}")
        return []

def get_media_meta(tmdb_id, media_type):
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return None

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    url = f"{overseerr_url}/api/v1/{media_type}/{tmdb_id}"
    
    try:
        response = api.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        poster_path = data.get('posterPath', '')
        if poster_path:
            poster_path = f"https://image.tmdb.org/t/p/w300{poster_path}"
            cache_poster_url(tmdb_id, media_type, poster_path)

        show_overview = data.get('overview', '')
        genres = [genre['name'] for genre in data.get('genres', [])]
        vote_average = data.get('voteAverage', '')
        backdrop_path = data.get('backdropPath', '')

        media_meta = (poster_path, show_overview, genres, vote_average, backdrop_path)
        cache_media_meta(tmdb_id, media_type, media_meta)
        logging.info(f"Cached poster and metadata for {media_type} (TMDB ID: {tmdb_id})")

        return media_meta
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching media meta from Overseerr: {e}")
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
    # Extract year if present
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', search_term)
    year = int(year_match.group(1)) if year_match else None
    
    # Remove year from search term for further parsing
    search_term_without_year = re.sub(r'\b(19\d{2}|20\d{2})\b', '', search_term).strip()
    
    # Match patterns like "S01E01", "s01e01", "S01", "s01"
    match = re.search(r'[Ss](\d+)(?:[Ee](\d+))?', search_term_without_year)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2)) if match.group(2) else None
        base_title = re.sub(r'[Ss]\d+(?:[Ee]\d+)?', '', search_term_without_year).strip()
        multi = episode is None  # Set multi to True if only season is specified
        return base_title, season, episode, year, multi
    return search_term_without_year, None, None, year, True  # Default to multi=True if no season/episode specified

def web_scrape(search_term: str, version: str) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for search term: {search_term}, version: {version}")
    
    base_title, season, episode, year, multi = parse_search_term(search_term)
    logging.info(f"Parsed search term: title='{base_title}', season={season}, episode={episode}, year={year}, multi={multi}")
    
    search_results = search_overseerr(base_title, year)
    if not search_results:
        logging.warning(f"No results found for search term: {base_title} ({year if year else 'no year specified'})")
        return {"error": "No results found"}

    logging.info(f"Found results: {search_results}")

    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    cookies = get_overseerr_cookies(overseerr_url)

    detailed_results = []
    for result in search_results:
        if result['mediaType'] != 'person' and result['posterPath'] is not None:
            tmdb_id = result['id']
            media_type = result['mediaType']

            if media_type == 'movie':
                details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
            else:  # TV show
                details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)

            if details:
                genres = details.get('keywords', [])

                logging.info(f"Genres: {genres}")   

                detailed_result = {
                    "id": tmdb_id,
                    "title": details.get('title') or details.get('name', ''),
                    "year": details.get('releaseDate', '')[:4] if media_type == 'movie' else details.get('firstAirDate', '')[:4],
                    "media_type": media_type,
                    "show_overview": details.get('overview', ''),
                    "poster_path": details.get('posterPath', ''),
                    "genre_ids": genres,
                    "vote_average": details.get('voteAverage', ''),
                    "backdrop_path": details.get('backdropPath', ''),
                    "season": season,
                    "episode": episode,
                    "multi": multi,
                    "genres": genres,
                    "imdb_id": details.get('externalIds', {}).get('imdbId', '')
                }
                detailed_results.append(detailed_result)

    return {"results": detailed_results}

def web_scrape_tvshow(media_id: int, title: str, year: int, season: Optional[int] = None) -> Dict[str, Any]:
    logging.info(f"Starting web scrape for TV Show: {title}, media_id: {media_id}")
    results=[]
    search_results = overseerr_tvshow(title, media_id = media_id, season = season)
    if not search_results:
        logging.warning(f"No results found for search term: {title} ({year if year else 'no year specified'})")
        return {"error": "No results found"}

    logging.info(f"Found results: {search_results}")
    if media_id and season is not None:
        return {
            "episodeResults": [
                {
                    "id": media_id,
                    "title": title,
                    "episode_title": result.get('name', ''),
                    "season_id": result['id'],
                    "season_num": result['seasonNumber'],
                    "episode_num": result['episodeNumber'],
                    "year": year,
                    "media_type": 'tv',
                    "still_path": result['stillPath'],
                    "air_date": result['airDate'],
                    "vote_average": result.get('voteAverage', ''),
                    "multi": False
                }
                for result in search_results
                if result['airDate'] is not None
                if result['episodeNumber'] != 0
            ]
        }
    else:
        return {
            "results": [
                {
                    "id": media_id,
                    "title": title,
                    "season_id": result['id'],
                    "season_num": result['seasonNumber'],
                    "year": year,
                    "media_type": 'tv',
                    "poster_path": result['posterPath'],
                    "air_date": result['airDate'],
                    "season_overview": result.get('overview', ''),
                    "episode_count": result.get('episodeCount', ''),
                    "multi": True
                }
                for result in search_results
                if result['airDate'] is not None
                if result['seasonNumber'] != 0
            ]
        }

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
                    poster_path = None

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
                "backdrop_path": media_meta[4] if media_meta else ''
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
                    poster_path = None

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
                "backdrop_path": media_meta[4] if media_meta else ''
            })

        return {"trendingShows": trending_shows}
    except api.exceptions.RequestException as e:
        logging.error(f"Error retrieving Trakt Trending Shows: {e}")
        return []

def process_media_selection(media_id: str, title: str, year: str, media_type: str, season: Optional[int], episode: Optional[int], multi: bool, version: str, genres: List[str]) -> List[Dict[str, Any]]:
    logging.info(f"Processing media selection: {media_id}, {title}, {year}, {media_type}, S{season or 'None'}E{episode or 'None'}, multi={multi}, version={version}, genres={genres}")

    details = get_media_details(media_id, media_type)
    imdb_id = details.get('externalIds', {}).get('imdbId', '')
    tmdb_id = str(details.get('id', ''))

    movie_or_episode = 'episode' if media_type == 'tv' else 'movie'

    # Adjust multi flag based on season and episode
    if movie_or_episode == 'movie':
        multi = False
    elif season is not None and episode is None:
        multi = True

    logging.info(f"Adjusted scraping parameters: imdb_id={imdb_id}, tmdb_id={tmdb_id}, title={title}, year={year}, "
                 f"movie_or_episode={movie_or_episode}, season={season}, episode={episode}, multi={multi}, version={version}")

    genres = [genre['name'] for genre in genres if 'name' in genre]

    # Call the scraper function with the version parameter
    scrape_results, filtered_out_results = scrape(imdb_id, tmdb_id, title, int(year), movie_or_episode, version, season, episode, multi, genres)

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
                    hashes.append(magnet_hash)
                    processed_results.append(result)
                else:
                    processed_results.append(result)

    # Check cache status for all hashes at once
    cache_status = is_cached_on_rd(hashes) if hashes else {}
    logging.info(f"Cache status returned: {cache_status}")

    # Update processed_results with cache status
    for result in processed_results:
        result_hash = result.get('hash')
        if result_hash:
            is_cached = cache_status.get(result_hash, False)
            result['cached'] = 'Yes' if is_cached else 'No'
            logging.info(f"Cache status for {result['title']} (hash: {result_hash}): {result['cached']}")

    return processed_results, cache_status

def get_available_versions():
    scraping_versions = get_setting('Scraping', 'versions', default={})
    return list(scraping_versions.keys())

def get_media_details(media_id: str, media_type: str) -> Dict[str, Any]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    details_url = f"{overseerr_url}/api/v1/{media_type}/{media_id}"

    try:
        response = api.get(details_url, headers=headers)
        response.raise_for_status()
        return response.json()
    except api.exceptions.RequestException as e:
        logging.error(f"Error fetching media details: {e}")
        return {}

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
            result = add_to_real_debrid(magnet_link)
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