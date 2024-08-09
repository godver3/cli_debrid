import sys
import logging
from typing import Dict, Any
import requests
import hashlib
import bencodepy
from queues.adding_queue import AddingQueue, is_cached_on_rd, extract_hash_from_magnet, get_magnet_files
from settings import get_setting

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_torrent_files(link: str) -> Dict[str, Any]:
    adding_queue = AddingQueue()
    
    if link.startswith('magnet:'):
        hash_value = extract_hash_from_magnet(link)
        files = get_magnet_files(link)
    else:
        hash_value = adding_queue.download_and_extract_hash(link)
        files = adding_queue.get_torrent_files(hash_value)
    
    cache_status = is_cached_on_rd(hash_value)
    
    return {
        "hash": hash_value,
        "is_cached": cache_status[hash_value] if hash_value in cache_status else False,
        "files": files.get('cached_files', []) if files else []
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: python torrent_file_helper.py <magnet_link_or_torrent_url>")
        sys.exit(1)
    
    link = sys.argv[1]
    result = get_torrent_files(link)
    
    print(f"Hash: {result['hash']}")
    print(f"Is Cached: {result['is_cached']}")
    print("Files:")
    for file in result['files']:
        print(f"  - {file}")

if __name__ == "__main__":
    main()
