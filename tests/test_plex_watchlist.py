import sys
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist

def test_plex_watchlist():
    # Test with empty versions dict
    versions = {}
    results = get_wanted_from_plex_watchlist(versions)
    
    # Print results
    for items, _ in results:
        if not items:
            logging.info("No items found in watchlist")
        for item in items:
            logging.info(f"Found item: {item}")

if __name__ == '__main__':
    test_plex_watchlist()
