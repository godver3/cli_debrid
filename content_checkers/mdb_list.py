from settings import get_setting
import requests
import logging
from plexapi.server import PlexServer
from logging_config import get_logger, get_log_messages
from content_checkers.overseer_checker import get_unavailable_content

logger = get_logger()

def get_mdblists():
    mdblist_api_key = get_setting('MDBList', 'api_key')
    mdblist_urls = get_setting('MDBList', 'urls')

    if not mdblist_api_key or not mdblist_urls:
        logger.error("MDBList API key or URLs not set. Please configure in settings.")
        return []

    # Split the URLs into a list and append '/json' to each URL
    url_list = [url.strip() + '/json' for url in mdblist_urls.split(',')]

    headers = {
        'Authorization': f'Bearer {mdblist_api_key}',
        'Accept': 'application/json'
    }

    all_mdblist_content = []

    for url in url_list:
        try:
            logger.info(f"Fetching MDBList content from URL: {url}")
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            all_mdblist_content.extend(data)
            logger.debug(f"Successfully fetched content from URL: {url}")
        except requests.RequestException as e:
            logger.error(f"Error fetching content from MDBList: {e}")
        except Exception as e:
            logger.error(f"Unexpected error while processing MDBList response: {e}")

    unavailable_movies = [
        {
            'imdb_id': item.get('imdb_id', 'Unknown IMDb ID'),
            'tmdb_id': item.get('id'),
            'title': item.get('title', 'Unknown Title'),
            'year': str(item.get('release_year', 'Unknown Year')),
        }
        for item in all_mdblist_content if item.get('mediatype') == 'movie'
    ]

    logger.debug(f"Fetched {len(unavailable_movies)} movies from MDBList")

    
    return unavailable_movies

def get_overseerr_requests(overseerr_url, overseerr_api_key):
    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    all_requests = []
    take = 50
    skip = 0

    while True:
        try:
            logger.debug(f"Fetching Overseerr requests, skip: {skip}, take: {take}")
            response = requests.get(
                f"{overseerr_url}/api/v1/request?take={take}&skip={skip}&sort=added",
                headers=headers
            )
            response.raise_for_status()
            data = response.json()
            requests_batch = data.get('results', [])
            total_results = data.get('pageInfo', {}).get('totalResults', 0)
            all_requests.extend(requests_batch)
            logger.debug(f"Fetched {len(requests_batch)} requests, total results so far: {len(all_requests)}")
            if len(requests_batch) < take:
                break  # No more results to fetch
            skip += take
        except requests.RequestException as e:
            logger.error(f"Error fetching requests from Overseerr: {e}")
            break
        except KeyError as e:
            logger.error(f"Unexpected response structure from Overseerr: {e}")
            break
        except Exception as e:
            logger.error(f"Unexpected error while processing Overseerr response: {e}")
            break

    logger.debug(f"Total requests fetched from Overseerr: {len(all_requests)}")
    return all_requests

def filter_mdblist_content(mdblist_content, overseerr_requests):
    overseerr_tmdb_ids = {req['media']['tmdbId'] for req in overseerr_requests if 'media' in req}

    filtered_content = [
        movie for movie in mdblist_content if movie['tmdb_id'] not in overseerr_tmdb_ids
    ]

    logger.debug(f"Filtered MDBList content, {len(filtered_content)} movies remaining out of {len(mdblist_content)}")
    return filtered_content

def filter_plex_library(plex, movies):
    library_imdb_ids = set()
    logger.info("Fetching Plex library sections...")
    for section in plex.library.sections():
        logger.debug(f"Processing section: {section.title}")
        if section.TYPE == 'movie':
            for movie in section.all():
                try:
                    for guid in movie.guids:
                        if guid.id.startswith("imdb://"):
                            library_imdb_ids.add(guid.id.split('/')[-1])
                except Exception as e:
                    logger.error(f"Error processing movie {movie.title}: {e}")

    logger.debug(f"Total movies in Plex library with IMDb IDs: {len(library_imdb_ids)}")
    filtered_content = [movie for movie in movies if movie['imdb_id'] not in library_imdb_ids]

    logger.debug(f"Filtered Plex library content, {len(filtered_content)} movies remaining out of {len(movies)}")
    return filtered_content

def add_requests_to_overseerr(overseerr_url, overseerr_api_key, movies):
    headers = {
        'X-Api-Key': overseerr_api_key,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    for movie in movies:
        # Convert imdb_id to tvdbId only if it's numeric
        tvdb_id = movie['imdb_id']
        if tvdb_id.isdigit():
            tvdb_id = int(tvdb_id)
        else:
            tvdb_id = None

        payload = {
            'mediaType': 'movie',
            'mediaId': movie['tmdb_id'],
            'imdbId': tvdb_id,
        }
        
        try:
            logger.info(f"Adding request to Overseerr for movie: {movie['title']} (TMDB ID: {movie['tmdb_id']})")
            logger.debug(f"Payload: {payload}")
            response = requests.post(f"{overseerr_url}/api/v1/request", json=payload, headers=headers)
            response.raise_for_status()
            logger.debug(f"Successfully added request for movie: {movie['title']}")
        except requests.RequestException as e:
            logger.error(f"Error adding request to Overseerr: {e}")
            logger.error(f"Response content: {response.content}")
        except Exception as e:
            logger.error(f"Unexpected error while adding request to Overseerr: {e}")

def sync_mdblist_with_overseerr():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    plex_url = get_setting('Plex', 'url')
    plex_token = get_setting('Plex', 'token')

    if not overseerr_url or not overseerr_api_key or not plex_url or not plex_token:
        logger.error("Overseerr URL/API key or Plex URL/token not set. Please configure in settings.")
        return

    try:
        plex = PlexServer(plex_url, plex_token)
        logger.debug(f"Connected to Plex server at {plex_url}")
    except Exception as e:
        logger.error(f"Error connecting to Plex server: {e}")
        return

    mdblist_content = get_mdblists()
    overseerr_requests = get_overseerr_requests(overseerr_url, overseerr_api_key)
    filtered_mdblist_content = filter_mdblist_content(mdblist_content, overseerr_requests)
    final_filtered_content = filter_plex_library(plex, filtered_mdblist_content)

    logger.info(f"Final filtered MDBList content: {final_filtered_content}")

    add_requests_to_overseerr(overseerr_url, overseerr_api_key, final_filtered_content)

    return final_filtered_content

# Example usage
if __name__ == "__main__":
    filtered_content = sync_mdblist_with_overseerr()
    print(filtered_content)
