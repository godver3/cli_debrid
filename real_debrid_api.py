import aiohttp
import logging
from settings import get_setting

API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
API_KEY = get_setting('RealDebrid', 'api_key')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

async def add_magnet(magnet_link: str):
    url = f"{API_BASE_URL}/torrents/addMagnet"
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'magnet': magnet_link
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 201:
                    result = await response.json()
                    torrent_id = result['id']
                    await select_files(torrent_id)
                    return torrent_id
                else:
                    logger.error(f"Failed to add magnet. Status code: {response.status}")
                    return None
    except Exception as e:
        #logger.error(f"Error adding magnet to Real-Debrid: {str(e)}")
        return None

async def select_files(torrent_id: str):
    url = f"{API_BASE_URL}/torrents/selectFiles/{torrent_id}"
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'files': 'all'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data) as response:
                if response.status == 204:
                    #logging.info(f"Successfully selected all files for torrent ID: {torrent_id}")
                    return True
                else:
                    #logging.error(f"Failed to select files. Status code: {response.status}")
                    return False
    except Exception as e:
        #logging.error(f"Error selecting files in Real-Debrid: {str(e)}")
        return False

async def get_torrent_info(torrent_id: str):
    url = f"{API_BASE_URL}/torrents/info/{torrent_id}"
    headers = {
        'Authorization': f'Bearer {API_KEY}'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    #logging.error(f"Failed to get torrent info. Status code: {response.status}")
                    return None
    except Exception as e:
        #logging.error(f"Error getting torrent info from Real-Debrid: {str(e)}")
        return None

async def is_torrent_ready(torrent_id: str):
    info = await get_torrent_info(torrent_id)
    if info and info['status'] == 'downloaded':
        return True
    return False
