import sys
import os
import logging
import re
import requests
from typing import Tuple
from scraper.scraper import scrape
from utilities.result_viewer import display_results
from debrid.real_debrid import add_to_real_debrid, extract_hash_from_magnet
from settings import get_setting
from content_checkers.overseerr import imdb_to_tmdb

logger = logging.getLogger(__name__)

def imdb_id_to_title_and_year(imdb_id: str) -> Tuple[str, int]:
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')

    tmdb_id = imdb_to_tmdb(overseerr_url, overseerr_api_key, imdb_id)
    if not tmdb_id:
        return "", 0

    headers = {
        'X-Api-Key': overseerr_api_key,
        'Accept': 'application/json'
    }

    # Try movie endpoint first
    movie_url = f"{overseerr_url}/api/v1/movie/{tmdb_id}"
    try:
        response = requests.get(movie_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data['title'], int(data['releaseDate'][:4])
    except requests.RequestException as e:
        logging.error(f"Error fetching movie data: {e}")

    # If movie not found, try TV show endpoint
    tv_url = f"{overseerr_url}/api/v1/tv/{tmdb_id}"
    try:
        response = requests.get(tv_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data['name'], int(data['firstAirDate'][:4])
    except requests.RequestException as e:
        logging.error(f"Error fetching TV show data: {e}")

    return "", 0

def scrape_sync(imdb_id, title, year, movie_or_episode, season, episode, multi):
    season = int(season) if season.strip() else None
    episode = int(episode) if season is not None and episode.strip() else None
    year = int(year) if year.strip() else None
    
    # Unpack all returned values, but only use the first one (results)
    scrape_result = scrape(imdb_id, title, year, movie_or_episode, season, episode, multi)
    logger.debug(f"Type of scrape_result: {type(scrape_result)}")
    logger.debug(f"Content of scrape_result: {scrape_result}")
    
    if isinstance(scrape_result, tuple):
        results = scrape_result[0]
    else:
        results = scrape_result
    
    logger.debug(f"Type of results: {type(results)}")
    logger.debug(f"Content of results: {results}")
    
    if not results:
        logger.error("No results found.")
        return
    
    if not isinstance(results, list):
        logger.error(f"Unexpected results format. Expected list, got {type(results)}")
        return
    
    processed_results = []
    for result in results:
        if isinstance(result, dict):
            magnet_link = result.get('magnet')
            if magnet_link:
                result['hash'] = extract_hash_from_magnet(magnet_link)
                processed_results.append(result)
        elif isinstance(result, str):
            logger.warning(f"Result is a string: {result}")
        else:
            logger.warning(f"Unexpected result format: {type(result)}")
    
    if not processed_results:
        logger.error("No valid results found after processing.")
        return
    
    selected_item = display_results(processed_results)
    if selected_item:
        magnet_link = selected_item.get('magnet')
        if magnet_link:
            add_to_real_debrid(magnet_link)
        else:
            logger.error("No magnet link found for the selected item.")
    else:
        logger.error("No item selected.")

def manual_scrape(imdb_id, title, year, movie_or_episode, season, episode, multi):
    scrape_sync(imdb_id, title, year, movie_or_episode, season, episode, multi)

def run_manual_scrape():
    imdb_id = input("Enter IMDb ID: ")
    title = input("Enter title (optional, press Enter to fetch from IMDb ID): ")
    year = input("Enter year (optional, press Enter to fetch from IMDb ID): ")
    movie_or_episode = input("Enter type (movie or episode): ")
    season = input("Enter season number (if applicable): ")
    episode = input("Enter episode number (if applicable): ")
    multi = input("Enter multi-pack (if applicable - true or false): ").strip().lower() == 'true'

    if not title or not year:
        fetched_title, fetched_year = imdb_id_to_title_and_year(imdb_id)
        if not title:
            title = fetched_title
            print(f"Fetched title: {title}")
        if not year:
            year = str(fetched_year)
            print(f"Fetched year: {year}")

    manual_scrape(imdb_id, title, year, movie_or_episode, season, episode, multi)
