import inquirer
from state_machine import StateMachine
from queue_manager import QueueManager
from launch_check import perform_launch_check
from scraper.scraper import scrape
from settings import edit_settings, get_setting
from database import get_all_collected_movies, get_all_collected_episodes, get_all_wanted_movies, get_all_wanted_episodes, search_collected_movies, search_collected_episodes, purge_database, purge_wanted_database, verify_database, load_collected_cache
from plex_integration import populate_db_from_plex
from content_checkers.overseer_checker import check_overseer_requests, get_unavailable_content, get_mdblists
from content_checkers.mdb_list import sync_mdblist_with_overseerr
import json
from trakt_config import debug_print_cache
import asyncio
import logging
from result_viewer import display_results
from processor.real_debrid import add_to_real_debrid, is_cached_on_rd, extract_hash_from_magnet
from database import (get_working_movies_by_state, get_working_episodes_by_state, 
                      purge_working_database)
from run_program import run_program_from_menu
from collections import deque
from questionary import Choice, select
from logging_config import get_logger
import os

logger = get_logger()
os.system('clear')

async def manual_scrape(imdb_id, movie_or_episode, season, episode, multi):
    # Convert season and episode to integers if they are not empty strings
    season = int(season) if season.strip() else None
    episode = int(episode) if episode.strip() else None
    #multi = multi.lower() == 'true'

    results = await scrape(imdb_id, movie_or_episode, season, episode, multi)

    if not results:
        logger.error("No results found.")
        return

    # Extract magnet links and hashes from results
    for result in results:
        magnet_link = result.get('magnet')
        if magnet_link:
            result['hash'] = extract_hash_from_magnet(magnet_link)

    # Filter out results without a valid hash
    results = [result for result in results if result.get('hash')]

    # Check cache status for each hash
    hashes = [result['hash'] for result in results]
    cache_status = await is_cached_on_rd(hashes)

    # Add cache status to each result
    for result in results:
        result['cached'] = cache_status.get(result['hash'], False)

    # Display results with cache status
    selected_item = display_results(results)

    if selected_item:
        magnet_link = selected_item.get('magnet')
        if magnet_link:
            if selected_item.get('cached'):
                await add_to_real_debrid(magnet_link)
            else:
                logger.error("The selected item is not cached on Real Debrid.")
        else:
            logger.error("No magnet link found for the selected item.")
    else:
        logger.error("No item selected.")

async def run_manual_scrape():
    imdb_id = input("Enter IMDb ID: ")
    movie_or_episode = input("Enter type (movie or episode): ")
    season = input("Enter season number (if applicable): ")
    episode = input("Enter episode number (if applicable): ")
    multi = input("Enter multi-pack (if applicable - true or false): ").strip().lower() == 'true'

    # Convert season and episode to integers if they are not None
    #season = int(season) if season is not None else None
    #episode = int(episode) if episode is not None else None

    await manual_scrape(imdb_id, movie_or_episode, season, episode, multi)

def search_db():
    search_term = input("Enter search term (use % for wildcards): ")
    content_type = inquirer.prompt([
        inquirer.List('type',
                      message="Select content type to search:",
                      choices=[
                          ('Collected Movies', 'collected_movies'),
                          ('Collected TV Shows', 'collected_tv_shows'),
                      ],
                      ),
    ])['type']

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
        questions = [
            inquirer.List('content_type',
                          message="Select content type to view:",
                          choices=[
                              ('Collected Movies', 'collected_movies'),
                              ('Collected Episodes', 'collected_episodes'),
                              ('Wanted Movies', 'wanted_movies'),
                              ('Wanted Episodes', 'wanted_episodes'),
                              ('Working Movies', 'working_movies'),
                              ('Working Episodes', 'working_episodes'),
                              ('Back', 'back')
                          ],
                          ),
        ]
        answers = inquirer.prompt(questions)

        if answers['content_type'] == 'collected_movies':
            movies = get_all_collected_movies()
            print("\nCollected Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}")
        elif answers['content_type'] == 'collected_episodes':
            episodes = get_all_collected_episodes()
            print("\nCollected Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}")
        elif answers['content_type'] == 'wanted_movies':
            movies = get_all_wanted_movies()
            print("\nWanted Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}, Release Date: {movie['release_date']}")
        elif answers['content_type'] == 'wanted_episodes':
            episodes = get_all_wanted_episodes()
            print("\nWanted Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, Release Date: {episode['release_date']}")
        elif answers['content_type'] == 'working_movies':
            movies = get_working_movies_by_state('Wanted')
            print("\nWorking Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}, State: {movie['state']}, Filled By: {movie['filled_by_title']}")
        elif answers['content_type'] == 'working_episodes':
            episodes = get_working_episodes_by_state('Wanted')
            print("\nWorking Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, State: {episode['state']}, Filled By: {episode['filled_by_title']}")
        elif answers['content_type'] == 'back':
            os.system('clear')
            break

        input("\nPress Enter to continue...")

def populate_wanted_database():
    print("Fetching unavailable content from Overseerr...")
    unavailable_content = get_unavailable_content()

import inquirer

def debug_commands():
    os.system('clear')
    while True:
        questions = [
            inquirer.List('action',
                          message="Select a debug action:",
                          choices=[
                              ('Populate Database from Plex', 'populate_db'),
                              ('View Database Content', 'view_db'),
                              ('Search Database', 'search_db'),
                              ('Populate Wanted Database', 'populate_wanted'),
                              ('Pull MDBList Items', 'pull_mdblist'),
                              ('Print Release Date Cache', 'print_cache'),
                              ('Print Collected Content Cache', 'print_collected_cache'),
                              ('Purge Working Database', 'purge_working_db'),
                              ('Purge Collected Database', 'purge_db'),
                              ('Purge Wanted Database', 'purge_wanted_db'),
                              ('Back to Main Menu', 'back')
                          ],
                          ),
        ]
        answers = inquirer.prompt(questions)

        if answers['action'] == 'populate_db':
            plex_url = get_setting('Plex', 'url')
            plex_token = get_setting('Plex', 'token')
            if plex_url and plex_token:
                populate_db_from_plex(plex_url, plex_token)
            else:
                print("Plex settings are not configured. Please set them in the settings menu.")
        elif answers['action'] == 'view_db':
            view_database_content()
        elif answers['action'] == 'search_db':
            search_db()
        elif answers['action'] == 'pull_mdblist':
            sync_mdblist_with_overseerr()
        elif answers['action'] == 'purge_db':
            confirm = inquirer.confirm("Are you sure you want to purge the collected database? This action cannot be undone.", default=False)
            if confirm:
                purge_database()
                verify_database()
                print("Database has been purged and tables recreated.")
            else:
                print("Database purge cancelled.")
        elif answers['action'] == 'purge_wanted_db':
            confirm = inquirer.confirm("Are you sure you want to purge the wanted database? This action cannot be undone.", default=False)
            if confirm:
                purge_wanted_database()
                verify_database()
                print("Database has been purged and tables recreated.")
            else:
                print("Database purge cancelled.")
        elif answers['action'] == 'populate_wanted':
            populate_wanted_database()
        elif answers['action'] == 'print_cache':
            debug_print_cache()
        elif answers['action'] == 'print_collected_cache':
            debug_print_collected_cache()
        elif answers['action'] == 'purge_working_db':
            confirm = inquirer.confirm("Are you sure you want to purge the working database? This action cannot be undone.", default=False)
            if confirm:
                purge_working_database()
                verify_database()
                print("Working database has been purged and tables recreated.")
            else:
                print("Working database purge cancelled.")
        elif answers['action'] == 'back':
            os.system('clear')
            break

def view_working_database_content():
    os.system('clear')
    while True:
        questions = [
            inquirer.List('content_type',
                          message="Select content type to view:",
                          choices=[
                              ('Working Movies', 'working_movies'),
                              ('Working Episodes', 'working_episodes'),
                              ('Back', 'back')
                          ],
                          ),
        ]
        answers = inquirer.prompt(questions)

        if answers['content_type'] == 'working_movies':
            movies = get_working_movies_by_state('Wanted')
            print("\nWorking Movies:")
            for movie in movies:
                print(f"ID: {movie['id']}, IMDb ID: {movie['imdb_id']}, TMDb ID: {movie['tmdb_id']}, Title: {movie['title']}, Year: {movie['year']}, State: {movie['state']}, Filled By: {movie['filled_by_title']}")
        elif answers['content_type'] == 'working_episodes':
            episodes = get_working_episodes_by_state('Wanted')
            print("\nWorking Episodes:")
            for episode in episodes:
                print(f"ID: {episode['id']}, Show IMDb ID: {episode['show_imdb_id']}, Show TMDb ID: {episode['show_tmdb_id']}, Show Title: {episode['show_title']}, Episode Title: {episode['episode_title']}, Year: {episode['year']}, Season: {episode['season_number']}, Episode: {episode['episode_number']}, State: {episode['state']}, Filled By: {episode['filled_by_title']}")
        elif answers['content_type'] == 'back':
            os.system('clear')
            break

        input("\nPress Enter to continue...")

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

async def execute_run_program():
    logger.info("Starting program execution...")
    program_task = asyncio.create_task(run_program())
    try:
        await program_task
    except asyncio.CancelledError:
        logger.info("Program execution cancelled")
    finally:
        logger.info("Program execution finished. Returning to main menu.")

async def main_menu():
    os.system('clear')
    while True:
        action = await select(
            "Select an action:",
            choices=[
                Choice("Run Program", "run"),
                Choice("Edit Settings", "settings"),
                Choice("Manual Scrape", "scrape"),
                Choice("Debug Commands", "debug"),
                Choice("Exit", "exit")
            ]
        ).ask_async()

        if action == "run":
            await run_program_from_menu()
        elif action == "settings":
            edit_settings()
        elif action == "scrape":
            await run_manual_scrape()
        elif action == "debug":
            debug_commands()
        elif action == "exit":
            print("Exiting program.")
            break

        logger.info("Returned to main menu.")
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main_menu())
