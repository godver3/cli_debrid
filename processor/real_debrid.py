import aiohttp
import asyncio
import json
import re
import logging
from settings import get_setting
from logging_config import get_logger
from types import SimpleNamespace

logger = get_logger()

API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
api_key = get_setting('RealDebrid', 'api_key')

# Rate limiter configuration
MAX_CALLS_PER_MINUTE = 60
rate_limiter = asyncio.Semaphore(MAX_CALLS_PER_MINUTE)

# Rate limiting wrapper
async def rate_limited():
    async with rate_limiter:
        await asyncio.sleep(60 / MAX_CALLS_PER_MINUTE)

async def add_to_real_debrid(magnet_link):
    if not api_key:
        logger.error("Real-Debrid API token not found in settings")
        return

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    async with aiohttp.ClientSession() as session:
        try:
            await rate_limited()
            # Step 1: Add magnet
            magnet_data = {'magnet': magnet_link}
            async with session.post(f"{API_BASE_URL}/torrents/addMagnet", headers=headers, data=magnet_data) as magnet_response:
                magnet_response.raise_for_status()
                magnet_response_data = await magnet_response.json()
                torrent_id = magnet_response_data['id']

            await rate_limited()
            # Step 2: Get torrent info
            async with session.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers) as info_response:
                info_response.raise_for_status()
                torrent_info = await info_response.json()

            await rate_limited()
            # Step 3: Select files (for this example, we'll select all files)
            files_to_select = ','.join([str(file['id']) for file in torrent_info['files']])
            select_data = {'files': files_to_select}
            async with session.post(f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}", headers=headers, data=select_data) as select_response:
                select_response.raise_for_status()

            # Step 4: Wait for the torrent to be processed
            await asyncio.sleep(5)

            await rate_limited()
            # Step 5: Get the download links
            async with session.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers) as links_response:
                links_response.raise_for_status()
                links_info = await links_response.json()

                if links_info['status'] == 'downloaded':
                    logger.info(f"Successfully added magnet to Real-Debrid:")
                    logger.info(f"Torrent ID: {torrent_id}")
                    logger.debug("\nDownload links:")
                    for link in links_info['links']:
                        logger.debug(link)
                else:
                    logger.debug(f"Torrent is still being processed. Current status: {links_info['status']}")

        except aiohttp.ClientResponseError as e:
            logger.error(f"Error adding magnet to Real-Debrid: {str(e)}")
            logger.debug(f"Error details: {await e.response.text()}")

        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")

# Error Log Function
def logerror(response):
    errors = [
        [202, " action already done"],
        [400, " bad Request (see error message)"],
        [403, " permission denied (infringing torrent or account locked or not premium)"],
        [503, " service unavailable (see error message)"],
        [404, " wrong parameter (invalid file id(s)) / unknown resource (invalid id)"],
    ]
    if response.status not in [200, 201, 204]:
        desc = ""
        for error in errors:
            if response.status == error[0]:
                desc = error[1]
        logger.error(f"[realdebrid] error: ({response.status}{desc}) {response.content}")

# Get Function
async def get(url):
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Authorization': f'Bearer {api_key}'
    }
    async with aiohttp.ClientSession() as session:
        try:
            await rate_limited()
            async with session.get(url, headers=headers) as response:
                logerror(response)
                response_data = await response.json()
                return json.loads(json.dumps(response_data), object_hook=lambda d: SimpleNamespace(**d))
        except Exception as e:
            logger.error(f"[realdebrid] error: (json exception): {e}")
            return None

# Function to check if torrents are cached on Real Debrid
async def is_cached_on_rd(hashes):
    # Ensure hashes is a list
    if isinstance(hashes, str):
        hashes = [hashes]

    # Build the URL for the API request
    url = f'https://api.real-debrid.com/rest/1.0/torrents/instantAvailability/{"/".join(hashes)}'

    # Make the API request
    response = await get(url)
    if not response:
        return {}

    # Parse the response to check cache status
    cache_status = {}
    for hash_ in hashes:
        if hasattr(response, hash_.lower()):
            response_attr = getattr(response, hash_.lower())
            if hasattr(response_attr, 'rd'):
                rd_attr = getattr(response_attr, 'rd')
                cache_status[hash_] = len(rd_attr) > 0
            else:
                cache_status[hash_] = False
        else:
            cache_status[hash_] = False

    return cache_status

def extract_hash_from_magnet(magnet_link):
    match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet_link)
    if match:
        return match.group(1).lower()
    else:
        return None

# Example implementation of required functions
def display_results(results):
    for idx, result in enumerate(results):
        cached_status = "Cached" if result.get('cached') else "Not Cached"
        logging.debug(f"{idx + 1}. {result.get('title')} - {cached_status}")
    selected_index = int(input("Select an item: ")) - 1
    return results[selected_index] if 0 <= selected_index < len(results) else None

async def manual_scrape(imdb_id, movie_or_episode, season, episode, multi):
    results = await scrape(imdb_id, movie_or_episode, season, episode, multi)

    if not results:
        logging.debug("No results found.")
        return

    # Extract magnet links and hashes from results
    for result in results:
        magnet_link = result.get('magnet')
        if magnet_link:
            result['hash'] = extract_hash_from_magnet(magnet_link)

    # Filter out results without a valid hash
    results = [result for result in results if result.get('hash')]

    # Check cache status for each hash
    hashes = [result['hash'] for result in results]
    cache_status = await is_cached_on_rd(hashes)

    # Add cache status to each result
    for result in results:
        result['cached'] = cache_status.get(result['hash'], False)

    # Display results with cache status
    selected_item = display_results(results)

    if selected_item:
        magnet_link = selected_item.get('magnet')
        if magnet_link:
            if selected_item.get('cached'):
                await add_to_real_debrid(magnet_link)
            else:
                logging.debug("The selected item is not cached on Real Debrid.")
        else:
            logging.debug("No magnet link found for the selected item.")
    else:
        logging.debug("No item selected.")

async def add_to_real_debrid_async(magnet_link):
    api_token = get_setting('RealDebrid', 'api_key')
    if not api_token:
        logger.error("Real-Debrid API token not found in settings")
        return

    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    async with aiohttp.ClientSession() as session:
        try:
            # Step 1: Add magnet
            magnet_data = {'magnet': magnet_link}
            async with session.post(f"{API_BASE_URL}/torrents/addMagnet", headers=headers, data=magnet_data) as magnet_response:
                magnet_response.raise_for_status()
                torrent_id = (await magnet_response.json())['id']

            # Step 2: Get torrent info
            async with session.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers) as info_response:
                info_response.raise_for_status()
                torrent_info = await info_response.json()

            # Step 3: Select files (for this example, we'll select all files)
            files_to_select = ','.join([str(file['id']) for file in torrent_info['files']])
            select_data = {'files': files_to_select}
            async with session.post(f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}", headers=headers, data=select_data) as select_response:
                select_response.raise_for_status()

            # Step 4: Wait for the torrent to be processed (you might want to implement a proper waiting mechanism)
            await asyncio.sleep(5)

            # Step 5: Get the download links
            async with session.get(f"{API_BASE_URL}/torrents/info/{torrent_id}", headers=headers) as links_response:
                links_response.raise_for_status()
                links_info = await links_response.json()

            if links_info['status'] == 'downloaded':
                logger.info(f"Successfully added magnet to Real-Debrid:")
                logger.info(f"Torrent ID: {torrent_id}")
                logger.debug("\nDownload links:")
                for link in links_info['links']:
                    logger.debug(link)
            else:
                logger.debug(f"Torrent is still being processed. Current status: {links_info['status']}")

        except aiohttp.ClientResponseError as e:
            logger.error(f"Error adding magnet to Real-Debrid: {str(e)}")
            if e.response:
                logger.debug(f"Error details: {await e.response.text()}")

        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
