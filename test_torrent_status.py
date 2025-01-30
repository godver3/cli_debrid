#!/usr/bin/env python3
import logging
from debrid.common.utils import format_torrent_status
from debrid.real_debrid.client import RealDebridProvider
from api_tracker import setup_api_logging, api

setup_api_logging()

def main():
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    print("Testing Torrent Status...")
    print("-" * 50)
    
    try:
        # Initialize api_tracker before using RealDebridProvider
        api.rate_limiter.reset_limits()
        
        provider = RealDebridProvider()
        torrents, stats = provider.get_torrent_status()
        status_text = format_torrent_status(torrents, stats)
        print(status_text)
    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
