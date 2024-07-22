import os
import sys
import questionary
from questionary import Choice
from utilities.plex_functions import get_collected_from_plex
import curses
from database import (
    get_all_media_items, search_movies, search_tv_shows,
    purge_database, verify_database,
    add_collected_items, add_wanted_items
)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from settings import get_setting
from logging_config import get_logger, get_log_messages
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.mdb_list import get_wanted_from_mdblists

logger = get_logger()

def search_db():
    search_term = input("Enter search term (use % for wildcards): ")
    content_type = questionary.select(
        "Select content type to search:",
        choices=[
            Choice("Movies", "movies"),
            Choice("TV Shows", "tv_shows")
        ]
    ).ask()

    if content_type == 'movies':
        results = search_movies(search_term)
        log_movie_results(results)
    elif content_type == 'tv_shows':
        results = search_tv_shows(search_term)
        log_tv_results(results)
        
def log_movie_results(results):
    if results:
        logger.info(f"{'ID':<5} {'Title':<50} {'Year':<5} {'State':<10}")
        logger.info("-" * 70)
        for item in results:
            logger.info(f"{item['id']:<5} {item['title']:<50} {item['year']:<5} {item['state']:<10}")
    else:
        logger.info("No movies found matching the search term.")

def log_tv_results(results):
    if results:
        logger.info(f"{'ID':<5} {'Show Title':<50} {'Season':<6} {'Episode':<7} {'Ep Title':<50} {'State':<10}")
        logger.info("-" * 130)
        for item in results:
            logger.info(f"{item['id']:<5} {item['title']:<50} S{item['season_number']:<5}E{item['episode_number']:<6} {item['episode_title']:<50} {item['state']:<10}")
    else:
        logger.info("No TV shows found matching the search term.")


def purge_db():
    confirm = questionary.confirm("Are you sure you want to purge the database? This action cannot be undone.", default=False).ask()
    if confirm:
        content_type = questionary.select(
            "Select content type to purge:",
            choices=[
                Choice("Movies", "movie"),
                Choice("Episodes", "episode"),
                Choice("All", "all")
            ]
        ).ask()

        state = questionary.select(
            "Select state to purge:",
            choices=[
                Choice("Wanted", "Wanted"),
                Choice("Collected", "Collected"),
                Choice("All", "all")
            ]
        ).ask()

        purge_database(content_type, state)
        verify_database()
        logger.info(f"Database has been purged for type '{content_type}' and state '{state}'. Tables recreated.")
    else:
        logger.info("Database purge cancelled.")

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
                Choice("Back", "back")
            ]
        ).ask()

        if content_type == 'collected_movies':
            movies = get_all_media_items(state='Collected', media_type='movie')
            headers = ["ID", "IMDb ID", "TMDb ID", "Title", "Year", "Release Date", "State", "Type", "Last Updated"]
            movie_data = [[str(movie['id']), movie['imdb_id'], movie['tmdb_id'], movie['title'], str(movie['year']), str(movie['release_date']), movie['state'], movie['type'], str(movie['last_updated'])] for movie in movies]
            display_results_curses(movie_data, headers)
        elif content_type == 'collected_episodes':
            episodes = get_all_media_items(state='Collected', media_type='episode')
            headers = ["ID", "IMDb ID", "TMDb ID", "Title", "Episode Title", "Year", "Season Number", "Episode Number", "Release Date", "State", "Type", "Last Updated"]
            episode_data = [[str(episode['id']), episode['imdb_id'], episode['tmdb_id'], episode['title'], episode['episode_title'], str(episode['year']), str(episode['season_number']), str(episode['episode_number']), str(episode['release_date']), episode['state'], episode['type'], str(episode['last_updated'])] for episode in episodes]
            display_results_curses(episode_data, headers)
        elif content_type == 'wanted_movies':
            movies = get_all_media_items(state='Wanted', media_type='movie')
            headers = ["ID", "IMDb ID", "TMDb ID", "Title", "Year", "Release Date", "State", "Type", "Last Updated"]
            movie_data = [[str(movie['id']), movie['imdb_id'], movie['tmdb_id'], movie['title'], str(movie['year']), str(movie['release_date']), movie['state'], movie['type'], str(movie['last_updated'])] for movie in movies]
            display_results_curses(movie_data, headers)
        elif content_type == 'wanted_episodes':
            episodes = get_all_media_items(state='Wanted', media_type='episode')
            headers = ["ID", "IMDb ID", "TMDb ID", "Title", "Episode Title", "Year", "Season Number", "Episode Number", "Release Date", "State", "Type", "Last Updated"]
            episode_data = [[str(episode['id']), episode['imdb_id'], episode['tmdb_id'], episode['title'], episode['episode_title'], str(episode['year']), str(episode['season_number']), str(episode['episode_number']), str(episode['release_date']), episode['state'], episode['type'], str(episode['last_updated'])] for episode in episodes]
            display_results_curses(episode_data, headers)
        elif content_type == 'back':
            os.system('clear')
            break

def display_results_curses(results, headers):
    def draw_menu(stdscr):
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        current_page = 0
        items_per_page = h - 2  # Reserve one line for the headers and one for navigation instructions

        def display_page():
            stdscr.clear()
            start_index = current_page * items_per_page
            end_index = start_index + items_per_page

            # Display headers
            for j, header in enumerate(headers):
                if sum(col_widths[:j + 1]) <= w:
                    stdscr.addstr(0, sum(col_widths[:j]), header.ljust(col_widths[j]))

            # Display rows
            for i, result in enumerate(results[start_index:end_index], start=1):
                for j, col in enumerate(result):
                    if sum(col_widths[:j + 1]) <= w:
                        value = str(col)[:col_widths[j] - 1]  # Truncate if necessary
                        stdscr.addstr(i, sum(col_widths[:j]), value.ljust(col_widths[j]))

            # Display navigation instructions
            stdscr.addstr(h - 1, 0, "Use LEFT/RIGHT arrows to navigate pages. Press 'q' to quit.")

            stdscr.refresh()

        # Calculate column widths
        col_widths = [max(len(str(row[i])) for row in [headers] + results) + 2 for i in range(len(headers))]
        total_width = sum(col_widths)
        
        # Adjust widths if total is greater than window width
        if total_width > w:
            for i in range(len(col_widths)):
                col_widths[i] = int(col_widths[i] * (w / total_width))

        # Main loop
        while True:
            display_page()
            key = stdscr.getch()
            if key == curses.KEY_RIGHT and (current_page + 1) * items_per_page < len(results):
                current_page += 1
            elif key == curses.KEY_LEFT and current_page > 0:
                current_page -= 1
            elif key == ord('q'):
                break

    curses.wrapper(draw_menu)

def debug_commands():
    os.system('clear')
    while True:
        action = questionary.select(
            "Select a debug action:",
            choices=[
                Choice("Get and Add All Collected from Plex", "get_all_collected"),
                Choice("Get and Add All Recent from Plex", "get_recent_collected"),
                Choice("Get and Add All Wanted from Overseerr", "get_all_wanted"),
                Choice("Get and Add All Wanted from MDB List", "pull_mdblist"),
                Choice("View Database Content", "view_db"),
                Choice("Search Database", "search_db"),
                Choice("Sync all collected content to Overseerr", "sync_all"),
                Choice("Purge Database", "purge_db"),
                Choice("Back to Main Menu", "back")
            ]
        ).ask()

        if action == 'get_all_collected':
            collected_content = get_collected_from_plex('all')
            if collected_content:
                add_collected_items(collected_content['movies'] + collected_content['episodes'])
        elif action == 'get_recent_collected':
            collected_content = get_collected_from_plex('recent')
            if collected_content:
                add_collected_items(collected_content['movies'] + collected_content['episodes'])
        elif action == 'view_db':
            view_database_content()
        elif action == 'search_db':
            search_db()
        elif action == 'pull_mdblist':
            wanted_content = get_wanted_from_mdblists()
            if wanted_content:
                add_wanted_items(wanted_content['movies'] + wanted_content['episodes'])
        elif action == 'purge_db':
            purge_db()
        elif action == 'get_all_wanted':
            wanted_content = get_wanted_from_overseerr()
            if wanted_content:
                add_wanted_items(wanted_content['movies'] + wanted_content['episodes'])
        elif action == 'sync_all':
            sync_collected_to_overseerr()
        elif action == 'back':
            os.system('clear')
            break

if __name__ == "__main__":
    debug_commands()
