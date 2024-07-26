import json
import os
import logging

BLACKLIST_FILE = 'db_content/manual_blacklist.json'

def load_blacklist():
    if not os.path.exists(BLACKLIST_FILE):
        return set()
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            return set(json.load(f))
    except json.JSONDecodeError:
        logging.error(f"Error decoding {BLACKLIST_FILE}. Starting with empty blacklist.")
        return set()

def save_blacklist(blacklist):
    with open(BLACKLIST_FILE, 'w') as f:
        json.dump(list(blacklist), f)

def add_to_blacklist(imdb_id):
    blacklist = load_blacklist()
    blacklist.add(imdb_id)
    save_blacklist(blacklist)
    logging.info(f"Added {imdb_id} to manual blacklist.")

def remove_from_blacklist(imdb_id):
    blacklist = load_blacklist()
    if imdb_id in blacklist:
        blacklist.remove(imdb_id)
        save_blacklist(blacklist)
        logging.info(f"Removed {imdb_id} from manual blacklist.")
    else:
        logging.warning(f"{imdb_id} not found in manual blacklist.")

def is_blacklisted(imdb_id):
    return imdb_id in load_blacklist()

def get_blacklist():
    return load_blacklist()
