import os
import sys
import questionary
from questionary import Choice
from utilities.plex_functions import get_collected_from_plex
import curses
from database import (
    get_all_media_items, search_movies, search_tv_shows,
    purge_database, verify_database, remove_from_media_items,
    add_collected_items, add_wanted_items, get_blacklisted_items, remove_from_blacklist
)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from content_checkers.overseerr import get_wanted_from_overseerr#, map_collected_media_to_wanted
from content_checkers.mdb_list import get_wanted_from_mdblists
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_trakt, ensure_trakt_auth
import logging
from manual_blacklist import add_to_manual_blacklist, remove_from_manual_blacklist, get_manual_blacklist, manage_manual_blacklist
from utilities.manual_scrape import imdb_id_to_title_and_year
import json
from typing import List, Tuple, Dict
from metadata.metadata import process_metadata, refresh_release_dates

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
        logging.info(f"{'ID':<5} {'Title':<50} {'Year':<5} {'State':<10}")
        logging.info("-" * 70)
        for item in results:
            logging.info(f"{item['id']:<5} {item['title']:<50} {item['year']:<5} {item['state']:<10}")
    else:
        logging.info("No movies found matching the search term.")

def log_tv_results(results):
    if results:
        logging.info(f"{'ID':<5} {'Show Title':<50} {'Season':<6} {'Episode':<7} {'Ep Title':<50} {'State':<10}")
        logging.info("-" * 130)
        for item in results:
            logging.info(f"{item['id']:<5} {item['title']:<50} S{item['season_number']:<5}E{item['episode_number']:<6} {item['episode_title']:<50} {item['state']:<10}")
    else:
        logging.info("No TV shows found matching the search term.")

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
                Choice("All", "all"),
                Choice("Working (All queues that are not Wanted, Collected, or Blacklisted", "working")
            ]
        ).ask()

        purge_database(content_type, state)
        verify_database()
        logging.info(f"Database has been purged for type '{content_type}' and state '{state}'. Tables recreated.")
    else:
        logging.info("Database purge cancelled.")

def view_database_content():
    os.system('clear')
    while True:
        queue = questionary.select(
            "Select a queue to view:",
            choices=[
                Choice("Collected", "Collected"),
                Choice("Wanted", "Wanted"),
                Choice("Scraping", "Scraping"),
                Choice("Adding", "Adding"),
                Choice("Checking", "Checking"),
                Choice("Sleeping", "Sleeping"),
                Choice("Blacklisted", "Blacklisted"),
                Choice("Upgrading", "Upgrading"),
                Choice("Back", "back")
            ]
        ).ask()

        if queue == 'back':
            os.system('clear')
            break

        content_type = questionary.select(
            f"Select content type to view in {queue} queue:",
            choices=[
                Choice("Movies", "movie"),
                Choice("TV Shows", "episode")
            ]
        ).ask()

        items = get_all_media_items(state=queue, media_type=content_type)

        if content_type == 'movie':
            headers = ["ID", "IMDb ID", "Title", "Year", "Release Date", "State", "Type", "Version"]
            item_data = [
                [str(item['id']), item['imdb_id'], item['title'], str(item['year']), 
                 str(item['release_date']), item['state'], item['type'], 
                 str(item['version'])] 
                for item in items
            ]
        else:  # TV shows (episodes)
            headers = ["ID", "IMDb ID", "Title", "Season", "Episode", "Year", "Release Date", "State", "Type", "Version"]
            item_data = [
                [str(item['id']), item['imdb_id'], item['title'], 
                 str(item['season_number']), str(item['episode_number']), 
                 str(item['year']), str(item['release_date']), item['state'], 
                 item['type'], str(item['version'])] 
                for item in items
            ]

        display_results_curses(item_data, headers)

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
                if j < len(col_widths) and sum(col_widths[:j + 1]) <= w:
                    stdscr.addstr(0, sum(col_widths[:j]), header.ljust(col_widths[j]))

            # Display rows
            for i, result in enumerate(results[start_index:end_index], start=1):
                for j, col in enumerate(result):
                    if j < len(col_widths) and sum(col_widths[:j + 1]) <= w:
                        value = str(col)[:col_widths[j] - 1]  # Truncate if necessary
                        stdscr.addstr(i, sum(col_widths[:j]), value.ljust(col_widths[j]))

            # Display navigation instructions
            stdscr.addstr(h - 1, 0, "Use LEFT/RIGHT arrows to navigate pages. Press 'q' to quit.")

            stdscr.refresh()

        # Calculate column widths
        col_widths = [max(len(str(row[i])) for row in [headers] + results) + 2 for i in range(len(headers))]
        
        # Ensure col_widths doesn't exceed the number of columns in the data
        col_widths = col_widths[:min(len(col_widths), len(results[0]))]
        
        total_width = sum(col_widths)

        # Adjust widths if total is greater than window width
        if total_width > w:
            scaling_factor = w / total_width
            col_widths = [max(int(width * scaling_factor), 1) for width in col_widths]

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

def delete_database_items():
    os.system('clear')
    while True:
        content_type = questionary.select(
            "Select content type to delete:",
            choices=[
                Choice("Movies", "movie"),
                Choice("TV Shows", "episode"),
                Choice("Back", "back")
            ]
        ).ask()

        if content_type == 'back':
            break

        items = get_all_media_items(media_type=content_type)

        if content_type == 'movie':
            headers = ["ID", "IMDb ID", "Title", "Year", "Release Date", "State", "Type"]
            item_data = [
                [str(item['id']), item['imdb_id'], item['title'], str(item['year']), 
                 str(item['release_date']), item['state'], item['type']] 
                for item in items
            ]
        else:  # TV shows (episodes)
            headers = ["ID", "IMDb ID", "Title", "Season", "Episode", "Year", "Release Date", "State", "Type"]
            item_data = [
                [str(item['id']), item['imdb_id'], item['title'], 
                 str(item['season_number']), str(item['episode_number']), 
                 str(item['year']), str(item['release_date']), item['state'], 
                 item['type']] 
                for item in items
            ]

        selected_items = display_results_curses_with_selection(item_data, headers)

        if selected_items:
            confirm = questionary.confirm(f"Are you sure you want to delete {len(selected_items)} items?").ask()
            if confirm:
                for item_id in selected_items:
                    remove_from_media_items(item_id)
                logging.info(f"Deleted {len(selected_items)} items from the database.")
            else:
                logging.info("Deletion cancelled.")

def display_results_curses_with_selection(results, headers):
    def draw_menu(stdscr):
        stdscr.clear()
        curses.curs_set(0)
        h, w = stdscr.getmaxyx()
        current_page = 0
        items_per_page = h - 3  # Reserve lines for headers, navigation instructions, and selection info
        selected_items = set()
        current_row = 0

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
                        if i - 1 == current_row:
                            stdscr.attron(curses.A_REVERSE)
                        if int(result[0]) in selected_items:
                            stdscr.attron(curses.A_BOLD)
                        stdscr.addstr(i, sum(col_widths[:j]), value.ljust(col_widths[j]))
                        stdscr.attroff(curses.A_REVERSE | curses.A_BOLD)

            # Display navigation instructions and selection info
            stdscr.addstr(h - 2, 0, "Use UP/DOWN arrows to navigate, SPACE to select, ENTER to confirm, 'q' to quit.")
            stdscr.addstr(h - 1, 0, f"Selected: {len(selected_items)} items")

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
            if key == curses.KEY_DOWN and current_row < min(items_per_page - 1, len(results) - 1 - current_page * items_per_page):
                current_row += 1
            elif key == curses.KEY_UP and current_row > 0:
                current_row -= 1
            elif key == curses.KEY_NPAGE:  # Page Down
                if (current_page + 1) * items_per_page < len(results):
                    current_page += 1
                    current_row = 0
            elif key == curses.KEY_PPAGE:  # Page Up
                if current_page > 0:
                    current_page -= 1
                    current_row = 0
            elif key == ord(' '):  # Space bar
                item_id = int(results[current_page * items_per_page + current_row][0])
                if item_id in selected_items:
                    selected_items.remove(item_id)
                else:
                    selected_items.add(item_id)
            elif key == 10:  # Enter key
                return list(selected_items)
            elif key == ord('q'):
                return []

    return curses.wrapper(draw_menu)

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
                Choice("Get and Add All Wanted from Collected", "get_wanted_collected"),
                Choice("Get and Add All Wanted from Trakt", "get_trakt"),
                Choice("View Database Content", "view_db"),
                Choice("Search Database", "search_db"),
                Choice("Delete Database Items", "delete_db"),  # New option
                Choice("Purge Database", "purge_db"),
                Choice("Manage Blacklisted Items", "manage_blacklist"),
                Choice("Manage Manual Blacklist", "manage_manual_blacklist"),
                Choice("Refresh release dates", "refresh_release"),
                Choice("Check and refresh Trakt auth token", "refresh_trakt"),
                Choice("Back to Main Menu", "back")
            ]
        ).ask()

        if action == 'delete_db':
            delete_database_items()
        elif action == 'get_all_collected':
            collected_content = get_collected_from_plex('all')
            if collected_content:
                add_collected_items(collected_content['movies'] + collected_content['episodes'])
        elif action == 'get_recent_collected':
            collected_content = get_collected_from_plex('recent')
            if collected_content:
                add_collected_items(collected_content['movies'] + collected_content['episodes'], recent=True)
        elif action == 'view_db':
            view_database_content()
        elif action == 'search_db':
            search_db()
        elif action == 'pull_mdblist':
            wanted_content = get_wanted_from_mdblists()
            if wanted_content:
                wanted_content_processed = process_metadata(wanted_content)
                if wanted_content_processed:
                    add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
        elif action == 'get_wanted_collected':
            wanted_content = get_wanted_from_collected()
            if wanted_content:
                wanted_content_processed = process_metadata(wanted_content)
                if wanted_content_processed:
                    add_wanted_items(wanted_content_processed['episodes'])
        elif action == 'get_trakt':
            wanted_content = get_wanted_from_trakt()
            if wanted_content:
                wanted_content_processed = process_metadata(wanted_content)
                if wanted_content_processed:
                    add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
        elif action == 'purge_db':
            purge_db()
        elif action == 'get_all_wanted':
            wanted_content = get_wanted_from_overseerr()
            if wanted_content:
                wanted_content_processed = process_metadata(wanted_content)
                if wanted_content_processed:
                    add_wanted_items(wanted_content_processed['movies'] + wanted_content_processed['episodes'])
        elif action == 'manage_blacklist':
            manage_blacklist()
        elif action == 'manage_manual_blacklist':
            manage_manual_blacklist()
        elif action == 'refresh_trakt':
            ensure_trakt_auth()
        elif action == 'refresh_release':
            refresh_release_dates()
        elif action == 'back':
            os.system('clear')
            break

def manage_blacklist():
    while True:
        blacklisted_items = get_blacklisted_items()
        
        if not blacklisted_items:
            logging.info("No blacklisted items found.")
            return

        choices = [
            Choice(f"{item['title']} ({item['year']}) - ID: {item['id']}", item['id'])
            if item['type'] == 'movie'
            else Choice(f"{item['title']} ({item['year']}) S{item['season_number']}E{item['episode_number']} - ID: {item['id']}", item['id'])
            for item in blacklisted_items
        ]
        choices.append(Choice("Back", "back"))

        selected = questionary.checkbox(
            "Select items to remove from blacklist (or choose 'Back' to return):",
            choices=choices
        ).ask()

        if "back" in selected:
            break

        if selected:
            remove_from_blacklist(selected)
            logging.info(f"Removed {len(selected)} items from blacklist.")
        else:
            break

if __name__ == "__main__":
    debug_commands()
