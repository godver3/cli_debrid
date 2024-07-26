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

logger = logging.getLogger(__name__)

TMDB_API_URL = "https://api.themoviedb.org/3"
TMDB_API_KEY = get_setting('TMDB', 'api_key')

def imdb_id_to_title_and_year(imdb_id: str) -> Tuple[str, int]:
    search_url = f"{TMDB_API_URL}/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"
    response = requests.get(search_url)
    if response.status_code == 200:
        data = response.json()
        if 'movie_results' in data and data['movie_results']:
            return data['movie_results'][0]['title'], int(data['movie_results'][0]['release_date'][:4])
        elif 'tv_results' in data and data['tv_results']:
            return data['tv_results'][0]['name'], int(data['tv_results'][0]['first_air_date'][:4])
    return "", None

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
