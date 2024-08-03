import requests
import os
import sys
from plex_location_fetch import fetch_library_items, get_imdb_id

API_BASE_URL = "http://192.168.1.51:6000"

def get_current_translations():
    response = requests.get(f"{API_BASE_URL}/list_translations")
    if response.status_code == 200:
        return {item[0]: item[1] for item in response.json()["translations"]}
    else:
        print(f"Error fetching translations: {response.text}")
        return {}

def add_translation(original, translated):
    response = requests.post(f"{API_BASE_URL}/add_translation", json={
        "original": original,
        "translated": translated
    })
    if response.status_code == 200:
        print(f"Translation added: {original} -> {translated}")
    else:
        print(f"Error adding translation: {response.text}")

def sanitize_path(path):
    return path.replace('/', '-')

def process_locations(items, current_translations, library_type):
    processed_count = 0
    added_count = 0
    for item in items:
        if library_type == 'movie':
            locations = item.locations
        elif library_type == 'show':
            locations = [episode.locations[0] for episode in item.episodes() if episode.locations]
        else:
            continue

        for location in locations:
            if "zurg-symlinked" in location:
                continue

            # Trim leading /mnt/zurg
            trimmed_location = location.replace("/mnt/zurg", "", 1)
            
            if trimmed_location not in current_translations:
                # Extract filename with extension
                original_filename = os.path.basename(trimmed_location)
                
                # Get IMDB ID
                imdb_id = get_imdb_id(item)
                
                # Sanitize the title
                sanitized_title = sanitize_path(item.title)
                
                if library_type == 'movie':
                    translated_path = f"/Films/{sanitized_title} ({item.year}) - {{{imdb_id}}}/{sanitized_title} ({item.year}) - {{{imdb_id}}} - [{os.path.splitext(original_filename)[0]}]{os.path.splitext(original_filename)[1]}"
                else:  # show
                    episode = next(ep for ep in item.episodes() if ep.locations and ep.locations[0] == location)
                    translated_path = f"/Shows/{sanitized_title} ({item.year}) {{{imdb_id}}}/Season {episode.seasonNumber}/{sanitized_title} ({item.year}) - S{episode.seasonNumber:02d}E{episode.index:02d} - {sanitize_path(episode.title)} [{os.path.splitext(original_filename)[0]}]{os.path.splitext(original_filename)[1]}"
                
                # Add translation
                add_translation(trimmed_location, translated_path)
                added_count += 1
            else:
                print(f"Translation already exists for: {trimmed_location}")
            
            processed_count += 1

    return processed_count, added_count

def main(library_name, library_type):
    current_translations = get_current_translations()
    print(f"Fetched {len(current_translations)} existing translations.")
    
    items = fetch_library_items(library_name)
    processed, added = process_locations(items, current_translations, library_type)
    print(f"Processed {processed} locations.")
    print(f"Added {added} new translations.")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python script_name.py <Library Name> <Library Type>")
        print("Library Type should be either 'movie' or 'show'")
        sys.exit(1)
    
    library_name = sys.argv[1]
    library_type = sys.argv[2]
    
    main(library_name, library_type)
