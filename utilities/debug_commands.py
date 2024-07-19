import os
import questionary
from questionary import Choice
from settings import get_setting
from plex_integration import populate_db_from_plex
from content_checkers.overseer_checker import get_unavailable_content
from content_checkers.mdb_list import sync_mdblist_with_overseerr
from database import (
    get_all_collected_movies, get_all_collected_episodes, get_all_wanted_movies,
    get_all_wanted_episodes, search_collected_movies, search_collected_episodes,
    purge_database, purge_wanted_database, verify_database, load_collected_cache,
    get_working_movies_by_state, get_working_episodes_by_state,
    purge_working_database
)

def search_db():
    search_term = input("Enter search term (use % for wildcards): ")
    content_type = questionary.select(
        "Select content type to search:",
        choices=[
            Choice("Collected Movies", "collected_movies"),
            Choice("Collected TV Shows", "collected_tv_shows")
        ]
    ).ask()

    if content_type == 'collected_movies':
        results = search_collected_movies(search_term)
        print_movie_results(results)
    elif content_type == 'collected_tv_shows':
        results = search_collected_episodes(search_term)
        print_tv_results(results)

def print_movie_results(results):
    if results:
        print("\nMatching Movies:")
        for movie in results:
            print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, Title: {movie['title']}, Year: {movie['year']}")
    else:
        print("No matching movies found.")

def print_tv_results(results):
    if results:
        print("\nMatching TV Shows:")
        for episode in results:
            print(f"Show: {episode['show_title']}, Episode: {episode['episode_title']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, Year: {episode['year']}")
    else:
        print("No matching TV shows found.")

def view_database_content():
    os.system('clear')
    while True:
        content_type = questionary.select(
            "Select content type to view:",
            choices=[
                Choice("Collected Movies", "collected_movies"),
                Choice("Collected Episodes", "collected_episodes"),
                Choice("Wanted Movies", "wanted_movies"),
                Choice("Wanted Episodes", "wanted_episodes"),
                Choice("Working Movies", "working_movies"),
                Choice("Working Episodes", "working_episodes"),
                Choice("Back", "back")
            ]
        ).ask()

        if content_type == 'collected_movies':
            movies = get_all_collected_movies()
            print("\nCollected Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}")
        elif content_type == 'collected_episodes':
            episodes = get_all_collected_episodes()
            print("\nCollected Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}")
        elif content_type == 'wanted_movies':
            movies = get_all_wanted_movies()
            print("\nWanted Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}, Release Date: {movie['release_date']}")
        elif content_type == 'wanted_episodes':
            episodes = get_all_wanted_episodes()
            print("\nWanted Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, Release Date: {episode['release_date']}")
        elif content_type == 'working_movies':
            movies = get_working_movies_by_state('Wanted')
            print("\nWorking Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}, State: {movie['state']}, Filled By: {movie['filled_by_title']}")
        elif content_type == 'working_episodes':
            episodes = get_working_episodes_by_state('Wanted')
            print("\nWorking Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, State: {episode['state']}, Filled By: {episode['filled_by_title']}")
        elif content_type == 'back':
            os.system('clear')
            break

        input("\nPress Enter to continue...")

def populate_wanted_database():
    print("Fetching unavailable content from Overseerr...")
    unavailable_content = get_unavailable_content()

def debug_commands():
    os.system('clear')
    while True:
        action = questionary.select(
            "Select a debug action:",
            choices=[
                Choice("Populate Database from Plex", "populate_db"),
                Choice("View Database Content", "view_db"),
                Choice("Search Database", "search_db"),
                Choice("Populate Wanted Database", "populate_wanted"),
                Choice("Pull MDBList Items", "pull_mdblist"),
                Choice("Print Release Date Cache", "print_cache"),
                Choice("Print Collected Content Cache", "print_collected_cache"),
                Choice("Purge Working Database", "purge_working_db"),
                Choice("Purge Collected Database", "purge_db"),
                Choice("Purge Wanted Database", "purge_wanted_db"),
                Choice("Back to Main Menu", "back")
            ]
        ).ask()

        if action == 'populate_db':
            plex_url = get_setting('Plex', 'url')
            plex_token = get_setting('Plex', 'token')
            if plex_url and plex_token:
                populate_db_from_plex(plex_url, plex_token)
            else:
                print("Plex settings are not configured. Please set them in the settings menu.")
        elif action == 'view_db':
            view_database_content()
        elif action == 'search_db':
            search_db()
        elif action == 'pull_mdblist':
            sync_mdblist_with_overseerr()
        elif action == 'purge_db':
            confirm = questionary.confirm("Are you sure you want to purge the collected database? This action cannot be undone.", default=False).ask()
            if confirm:
                purge_database()
                verify_database()
                print("Database has been purged and tables recreated.")
            else:
                print("Database purge cancelled.")
        elif action == 'purge_wanted_db':
            confirm = questionary.confirm("Are you sure you want to purge the wanted database? This action cannot be undone.", default=False).ask()
            if confirm:
                purge_wanted_database()
                verify_database()
                print("Database has been purged and tables recreated.")
            else:
                print("Database purge cancelled.")
        elif action == 'populate_wanted':
            populate_wanted_database()
        elif action == 'print_cache':
            debug_print_cache()
        elif action == 'print_collected_cache':
            debug_print_collected_cache()
        elif action == 'purge_working_db':
            confirm = questionary.confirm("Are you sure you want to purge the working database? This action cannot be undone.", default=False).ask()
            if confirm:
                purge_working_database()
                verify_database()
                print("Working database has been purged and tables recreated.")
            else:
                print("Working database purge cancelled.")
        elif action == 'back':
            os.system('clear')
            break

def debug_print_collected_cache():
    cache = load_collected_cache()
    print("\nCollected Content Cache:")
    print("========================")
    print("\nMovies:")
    for movie in sorted(cache['movies']):
        print(f"  - {movie[0]} ({movie[1]})")
    print("\nEpisodes:")
    for episode in sorted(cache['episodes']):
        print(f"  - {episode[0]} S{episode[1]}E{episode[2]}")
    print("\nTotal cached items:")
    print(f"  Movies: {len(cache['movies'])}")
    print(f"  Episodes: {len(cache['episodes'])}")
