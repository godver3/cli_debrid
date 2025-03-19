import json
import os
import logging
from typing import Dict

# Get db_content directory from environment variable with fallback
DB_CONTENT_DIR = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Update the path to use the environment variable
BLACKLIST_FILE = os.path.join(DB_CONTENT_DIR, 'manual_blacklist.json')

def load_manual_blacklist():
    os.makedirs(os.path.dirname(BLACKLIST_FILE), exist_ok=True)

    if not os.path.exists(BLACKLIST_FILE):
        return {}
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.error(f"Error decoding {BLACKLIST_FILE}. Starting with empty blacklist.")
        return {}

def save_manual_blacklist(blacklist):
    with open(BLACKLIST_FILE, 'w') as f:
        json.dump(blacklist, f)

def add_to_manual_blacklist(imdb_id: str, media_type: str, title: str, year: str, season: int = None):
    blacklist = get_manual_blacklist()
    
    if season is not None and media_type == 'tv':
        # If this is the first season for this show
        if imdb_id not in blacklist:
            blacklist[imdb_id] = {
                'media_type': 'tv',
                'title': title,
                'year': year,
                'seasons': [season]
            }
        else:
            # Add season if not already blacklisted
            if 'seasons' not in blacklist[imdb_id]:
                blacklist[imdb_id]['seasons'] = []
            if season not in blacklist[imdb_id]['seasons']:
                blacklist[imdb_id]['seasons'].append(season)
                blacklist[imdb_id]['seasons'].sort()
    else:
        # Regular blacklisting for movies or entire shows
        blacklist[imdb_id] = {
            'media_type': media_type,
            'title': title,
            'year': year
        }
        if media_type == 'tv':
            blacklist[imdb_id]['seasons'] = []  # Empty list means all seasons

    save_manual_blacklist(blacklist)
    if season is not None:
        logging.info(f"Added {imdb_id}: {title} ({year}) Season {season} to manual blacklist")
    else:
        logging.info(f"Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type}")

def remove_from_manual_blacklist(imdb_id):
    blacklist = get_manual_blacklist()
    if imdb_id in blacklist:
        item = blacklist.pop(imdb_id)
        save_manual_blacklist(blacklist)
        logging.info(f"Removed {imdb_id}: {item['title']} ({item['year']}) from manual blacklist.")
    else:
        logging.warning(f"{imdb_id} not found in manual blacklist.")

def is_blacklisted(imdb_id, season: int = None):
    blacklist = get_manual_blacklist()
    if imdb_id not in blacklist:
        return False
        
    item = blacklist[imdb_id]
    if item['media_type'] != 'tv':
        return True
        
    # If seasons is empty or doesn't exist, the entire show is blacklisted
    if 'seasons' not in item or not item['seasons']:
        return True
        
    # If season is None, we're checking the whole show
    if season is None:
        return False  # Show isn't fully blacklisted if specific seasons are listed
        
    return season in item['seasons']

def get_manual_blacklist() -> Dict[str, Dict[str, str]]:
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}