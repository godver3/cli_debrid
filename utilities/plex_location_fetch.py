import sys
from plexapi.server import PlexServer

# Plex server details
PLEX_URL = 'http://192.168.1.51:32400'
PLEX_TOKEN = 'sTr9jgcH7-H2YUyu2hr7'

def fetch_library_items(library_name):
    plex = PlexServer(PLEX_URL, PLEX_TOKEN)
    library = plex.library.section(library_name)
    return library.all()

def get_imdb_id(item):
    for guid in item.guids:
        if guid.id.startswith('imdb://'):
            return guid.id.split('://')[1]
    return "N/A"

def print_movies(movies):
    for movie in movies:
        imdb_id = get_imdb_id(movie)
        for location in movie.locations:
            print(f"{movie.title}|{movie.year}|{imdb_id}|{location}")

def print_shows(shows):
    for show in shows:
        show_imdb_id = get_imdb_id(show)
        for episode in show.episodes():
            for location in episode.locations:
                print(f"{show.title}|{show.year}|{show_imdb_id}|{episode.title}|{episode.seasonNumber}|{episode.index}|{location}")

def main(library_name, library_type):
    items = fetch_library_items(library_name)
    
    if library_type.lower() == 'movie':
        print_movies(items)
    elif library_type.lower() == 'show':
        print_shows(items)
    else:
        print(f"Unsupported library type: {library_type}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python script_name.py <Library Name> <Library Type>")
        print("Library Type should be either 'movie' or 'show'")
        sys.exit(1)
    
    library_name = sys.argv[1]
    library_type = sys.argv[2]
    
    main(library_name, library_type)
