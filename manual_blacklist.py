import json
import os
import logging
import questionary
from questionary import Choice
from typing import List, Tuple, Dict

# Update the path to include the db_content folder
BLACKLIST_FILE = os.path.join('db_content', 'manual_blacklist.json')

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

def add_to_manual_blacklist(imdb_id: str, media_type: str, title: str, year: str):
    blacklist = get_manual_blacklist()
    blacklist[imdb_id] = {
        'media_type': media_type,
        'title': title,
        'year': year
    }
    if blacklist[imdb_id]['media_type'] == 'tv':   
        blacklist[imdb_id]['media_type'] = 'episode'

    save_manual_blacklist(blacklist)
    logging.info(f"Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type}")

def remove_from_manual_blacklist(imdb_id):
    blacklist = get_manual_blacklist()
    if imdb_id in blacklist:
        item = blacklist.pop(imdb_id)
        save_manual_blacklist(blacklist)
        logging.info(f"Removed {imdb_id}: {item['title']} ({item['year']}) from manual blacklist.")
    else:
        logging.warning(f"{imdb_id} not found in manual blacklist.")

def is_blacklisted(imdb_id):
    return imdb_id in get_manual_blacklist()

def get_manual_blacklist() -> Dict[str, Dict[str, str]]:
    try:
        with open(BLACKLIST_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def manage_manual_blacklist():
    from utilities.manual_scrape import imdb_id_to_title_and_year

    os.system('clear')

    while True:
        action = questionary.select(
            "Select an action:",
            choices=[
                Choice("View Manual Blacklist", "view"),
                Choice("Add to Manual Blacklist", "add"),
                Choice("Remove from Manual Blacklist", "remove"),
                Choice("Back", "back")
            ]
        ).ask()
        os.system('clear')

        if action == 'view':
            blacklist = get_manual_blacklist()
            if blacklist:
                for imdb_id, item in blacklist.items():
                    print(f"{imdb_id}: {item['title']} ({item['year']}) [{item['media_type'].capitalize()}]")
            else:
                print("Manual blacklist is empty.")

        elif action == 'add':
            imdb_id = questionary.text("Enter IMDb ID to blacklist:").ask()
            media_type = questionary.select(
                "Is this a movie or TV show?",
                choices=[Choice("Movie", "movie"), Choice("TV Show", "episode")]
            ).ask()
            title, year = imdb_id_to_title_and_year(imdb_id, media_type)
            if title and year:
                confirm = questionary.confirm(f"Add '{title} ({year})' to the blacklist?").ask()
                if confirm:
                    add_to_manual_blacklist(imdb_id, media_type, title, year)
                    print(f"Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type}.")
                else:
                    print("Operation cancelled.")
            else:
                print(f"Unable to fetch title and year for {imdb_id}. Do you still want to add it to the blacklist?")
                if questionary.confirm("Add to blacklist anyway?").ask():
                    title = questionary.text("Enter title:").ask()
                    year = questionary.text("Enter year:").ask()
                    add_to_manual_blacklist(imdb_id, media_type, title, year)
                    print(f"Added {imdb_id}: {title} ({year}) to manual blacklist as {media_type}.")
                else:
                    print("Operation cancelled.")

        elif action == 'remove':
            blacklist = get_manual_blacklist()
            if not blacklist:
                print("Manual blacklist is empty.")
                continue

            choices = [Choice(f"{imdb_id}: {item['title']} ({item['year']}) [{item['media_type'].capitalize()}]", imdb_id) for imdb_id, item in blacklist.items()]
            choices.append(Choice("Back", "back"))

            selected = questionary.select(
                "Select item to remove from manual blacklist:",
                choices=choices
            ).ask()

            if selected != "back":
                remove_from_manual_blacklist(selected)

        elif action == 'back':
            break
