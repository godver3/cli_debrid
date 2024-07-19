import sys, os
import asyncio
import logging
from scraper.scraper import scrape
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from utilities.result_viewer import display_results
from debrid.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet

logger = logging.getLogger(__name__)

async def manual_scrape(imdb_id, movie_or_episode, season, episode, multi):
    season = int(season) if season.strip() else None
    episode = int(episode) if episode.strip() else None

    results = await scrape(imdb_id, movie_or_episode, season, episode, multi)

    if not results:
        logger.error("No results found.")
        return

    for result in results:
        magnet_link = result.get('magnet')
        if magnet_link:
            result['hash'] = extract_hash_from_magnet(magnet_link)

    results = [result for result in results if result.get('hash')]
    hashes = [result['hash'] for result in results]
    cache_status = await is_cached_on_rd(hashes)

    for result in results:
        result['cached'] = cache_status.get(result['hash'], False)

    selected_item = display_results(results)

    if selected_item:
        magnet_link = selected_item.get('magnet')
        if magnet_link:
            if selected_item.get('cached'):
                await add_to_real_debrid(magnet_link)
            else:
                logger.error("The selected item is not cached on Real Debrid.")
        else:
            logger.error("No magnet link found for the selected item.")
    else:
        logger.error("No item selected.")

async def run_manual_scrape():
    imdb_id = input("Enter IMDb ID: ")
    movie_or_episode = input("Enter type (movie or episode): ")
    season = input("Enter season number (if applicable): ")
    episode = input("Enter episode number (if applicable): ")
    multi = input("Enter multi-pack (if applicable - true or false): ").strip().lower() == 'true'

    await manual_scrape(imdb_id, movie_or_episode, season, episode, multi)
