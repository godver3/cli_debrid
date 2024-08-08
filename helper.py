import logging
from debrid.real_debrid import add_to_real_debrid, extract_hash_from_magnet, is_cached_on_rd, get_magnet_files, get, API_BASE_URL
from settings import get_setting
import json
import requests
import time

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Get the API key
api_key = get_setting("RealDebrid", "api_key")
if not api_key:
    raise ValueError("Real-Debrid API key not found in settings")

def print_json(obj):
    print(json.dumps(obj, indent=2, default=str))

def get_with_auth(url):
    headers = {
        'Authorization': f'Bearer {api_key}'
    }
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def analyze_magnet(magnet_link):
    logging.info(f"Analyzing magnet link: {magnet_link}")

    hash_ = extract_hash_from_magnet(magnet_link)
    if not hash_:
        logging.error("Failed to extract hash from magnet link")
        return

    logging.info(f"Extracted hash: {hash_}")

    cache_status = is_cached_on_rd(hash_)
    logging.info(f"Cache status: {cache_status}")

    try:
        result = add_to_real_debrid(magnet_link)
        logging.info("Add to Real-Debrid result:")
        print_json(result)

        if isinstance(result, list):
            logging.info("Torrent is cached. Direct links:")
            for link in result:
                logging.info(f"- {link}")
        elif isinstance(result, str):
            if result in ['downloading', 'queued']:
                logging.info(f"Torrent is uncached. Status: {result}")
                logging.info("Waiting for torrent to be processed...")
                
                # Poll for torrent status
                torrent_id = None
                for _ in range(10):  # Try for about 100 seconds
                    time.sleep(10)
                    torrents = get_with_auth(f"{API_BASE_URL}/torrents")
                    for torrent in torrents:
                        if torrent['hash'].lower() == hash_.lower():
                            torrent_id = torrent['id']
                            break
                    if torrent_id:
                        break
                
                if torrent_id:
                    logging.info(f"Found torrent ID: {torrent_id}")
                    info_url = f"{API_BASE_URL}/torrents/info/{torrent_id}"
                    torrent_info = get_with_auth(info_url)

                    logging.info("Torrent info:")
                    print_json(torrent_info)

                    if 'files' in torrent_info:
                        logging.info("Torrent files:")
                        for file in torrent_info['files']:
                            logging.info(f"- {file['path']} (Size: {file['bytes']} bytes)")
                    else:
                        logging.warning("No 'files' key in torrent_info")
                        logging.info("Available keys:")
                        for key in torrent_info.keys():
                            logging.info(f"- {key}: {torrent_info[key]}")
                else:
                    logging.warning("Could not find torrent ID after waiting")
            else:
                logging.warning(f"Unexpected result from add_to_real_debrid: {result}")
        else:
            logging.warning("Failed to add torrent to Real-Debrid")

    except Exception as e:
        logging.error(f"Error occurred while processing the magnet link: {str(e)}", exc_info=True)

    magnet_files = get_magnet_files(magnet_link)
    logging.info("Magnet files:")
    print_json(magnet_files)

if __name__ == "__main__":
    magnet_link = "magnet:?xt=urn:btih:82e9c42126adff0d43ce00d77bfa5b346df4c533&dn=Ghostwriter.S01.2160p.ATVP.WEB-DL.DD5.1.DV.MKV.x265-FLUX%5Brartv%5D&so=8"
    analyze_magnet(magnet_link)
