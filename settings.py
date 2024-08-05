import urwid
import configparser
import os
import subprocess
import sys
import logging
import inspect
import time
from urllib.parse import urlparse
from trakt import init
import trakt.core
import io

CONFIG_FILE = './config/config.ini'

def load_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)

def get_setting(section, option, default=''):
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    try:
        value = config.get(section, option)
        if value is None or value.strip() == '':
            logging.debug(f"Empty or None value found for {section}.{option}, using default: {default}")
            return default
        if value.lower() in ['true', 'yes', '1']:
            return True
        elif value.lower() in ['false', 'no', '0']:
            return False
        return value
    except (configparser.NoSectionError, configparser.NoOptionError):
        logging.debug(f"Setting not found - Section: {section}, Option: {option}, using default: {default}")
        return default

def validate_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    
    # Remove trailing slash
    url = url.rstrip('/')
    
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Invalid URL")
    return url

def set_setting(section, key, value):
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    
    # List of keys that should be treated as URLs
    url_keys = ['url']  # Add more keys here if needed, e.g., ['url', 'api_url', 'webhook_url']
    
    if key in url_keys:
        try:
            value = validate_url(value)
        except ValueError:
            logging.error(f"Invalid URL provided for {section}.{key}: {value}")
            return False

    config.set(section, key, value)
    save_config(config)
    return True

def get_all_settings():
    from settings import SettingsEditor

    all_settings = []
    methods = [
        'show_required_settings',
        'show_additional_settings',
        'show_scraping_settings',
        'show_debug_settings'
    ]

    for method_name in methods:
        method = getattr(SettingsEditor, method_name)
        source = inspect.getsource(method)
        start = source.index('[')
        end = source.rindex(']') + 1
        settings_list = eval(source[start:end])
        all_settings.extend(settings_list)

    return all_settings
    
def get_scraping_settings():
    all_settings = get_all_settings()
    scraping_settings = {}
    
    for section, key, label in all_settings:
        if section == 'Scraping':
            # Get the current value from the config file
            value = get_setting('Scraping', key)
            scraping_settings[key] = (label, value)
    
    return scraping_settings

def ensure_settings_file():
    config = load_config()
    all_settings = get_all_settings()

    for section, key, _ in all_settings:
        if not config.has_section(section):
            config.add_section(section)
        if not config.has_option(section, key) or not config.get(section, key).strip():
            # Set a default value
            default_value = ''
            if key == 'enabled':
                default_value = 'False'
            elif key == 'logging_level':
                default_value = 'INFO'
            elif key == 'wake_limit':
                default_value = '3'
            config.set(section, key, default_value)

    save_config(config)

class SettingsEditor:
    def __init__(self):
        self.config = load_config()
        self.edits = {}
        self.palette = [
            ('reversed', 'standout', ''),
            ('edit', 'light gray', 'black'),
            ('edit_focus', 'white', 'dark blue'),
            ('error', 'light red', 'black'),
        ]
        self.main_loop = urwid.MainLoop(self.build_main_menu(), self.palette, unhandled_input=self.exit_on_q)
        self.main_loop.run()

    def build_main_menu(self):
        menu = urwid.Pile([
            urwid.Text("Settings Editor (press 'q' to quit)"),
            urwid.Text("Use Shift-Insert to paste values."),
            urwid.AttrMap(urwid.Button("Required Settings", on_press=self.show_required_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Additional Settings", on_press=self.show_additional_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Scraping Settings", on_press=self.show_scraping_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Debug Settings", on_press=self.show_debug_settings), None, focus_map='reversed')
        ])
        return urwid.Filler(menu, valign='top')

    def show_required_settings(self, button):
        self.show_settings("Required Settings", [
            ('Plex', 'url', 'Plex - Plex URL'),
            ('Plex', 'token', 'Plex - Plex Token'),
            ('Plex', 'movie_libraries', 'Plex - List of movie libraries, separated by commas'),
            ('Plex', 'shows_libraries', 'Plex - List of shows libraries, separated by commas'),
            ('Overseerr', 'url', 'Overseerr - Overseerr URL'),
            ('Overseerr', 'api_key', 'Overseerr - Overseerr API Key'),
            ('RealDebrid', 'api_key', 'RealDebrid - Real-Debrid API Key'),
            ('Torrentio', 'enabled', 'Torrentio - Torrentio enabled? (Must configure at least one scraper in Additional Settings if not Torrentio) True/False')
        ])

    def show_additional_settings(self, button):
        self.show_settings("Additional Settings", [
            ('Zilean', 'url', 'Zilean - Zilean URL'),
            ('Zilean', 'enabled', 'Zilean - Zilean enabled? True/False'),
            #('Knightcrawler', 'url', 'Knightcrawler URL'),
            #('Knightcrawler', 'enabled', 'Knightcrawler enabled? (True/False)'),
            ('Comet', 'url', 'Comet - Comet URL'),
            ('Comet', 'enabled', 'Comet - Comet enabled? True/False'),
            ('MDBList', 'urls', 'MDBList - MDB List URLs'),
            ('Collected Content Source', 'enabled', 'Collected - Enable collected content source? True/False'),
            ('TMDB', 'api_key', 'TMDB - TMDB API Key'),
            ('Queue', 'wake_limit', 'Queue - Enter number of times to wake items before blacklisting'),
            ('Scraping', 'uncached_content_handling', 'Scraping - Uncached content handling (None/Hybrid/Full)'),
            ('Trakt', 'client_id', 'Trakt - Enter Trakt client ID'),
            ('Trakt', 'client_secret', 'Trakt - Enter Trakt client secret'),
            ('Trakt', 'user_watchlist_enabled', 'Trakt - Enable your watchlist as a content source? True/False (Requires auth. below)'),
            ('Trakt', 'trakt_lists', 'Trakt - Add any other Trakt lists as content sources (comma-separated)')
            #('Trakt', 'oauth_token', 'Trakt - oauth_token - Do not change - set by authorization below'),
            #('Trakt', 'refresh_token', 'Trakt - refresh_token - Do not change - set by authorization below')
        ])
        # Add Trakt OAuth button
        self.main_loop.widget.body.body.insert(-2, urwid.AttrMap(
            urwid.Button("Authorize Trakt", on_press=self.start_trakt_oauth),
            None, focus_map='reversed'))

    def show_scraping_settings(self, button):
        self.show_settings("Scraping Settings", [
            ('Scraping', 'enable_4k', '4k enabled? True/False'),
            ('Scraping', 'enable_hdr', 'HDR enabled? True/False'),
            ('Scraping', 'resolution_weight', 'Resolution weight (1-5)'),
            ('Scraping', 'hdr_weight', 'HDR weight (1-5)'),
            ('Scraping', 'similarity_weight', 'Title similarity weight (1-5)'),
            ('Scraping', 'size_weight', 'File size weight (1-5)'),
            ('Scraping', 'bitrate_weight', 'Bitrate weight (1-5)'),
            ('Scraping', 'preferred_filter_in', 'Preferred filter-in terms (comma-separated)'),
            ('Scraping', 'preferred_filter_out', 'Preferred filter-out terms (comma-separated)'),
            ('Scraping', 'filter_in', 'Required filter-in terms (comma-separated)'),
            ('Scraping', 'filter_out', 'Required filter-out terms (comma-separated)'),
            ('Scraping', 'min_size_gb', 'Minimum file size in GB (e.g., 0.01)')
        ])
        
    def show_debug_settings(self, button):
        self.show_settings("Debug Settings", [
            ('Debug', 'logging_level', 'Logging - Logging Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)'),
            ('Debug', 'skip_initial_plex_update', 'Menu - Skip Plex initial collection scan (True/False)'),
            ('Debug', 'skip_menu', 'Menu - Skip menu? (True/False)'),
            ('Debug', 'disable_initialization', 'Menu - Disable initialization tasks? (True/False)')
        ])

    def start_trakt_oauth(self, button):
        # Save the current urwid screen
        saved_screen = self.main_loop.screen

        # Temporarily suspend urwid
        self.main_loop.screen.stop()

        print("\nStarting Trakt Authorization Process")
        print("====================================")

        # Get existing client ID and secret
        client_id = get_setting('Trakt', 'client_id')
        client_secret = get_setting('Trakt', 'client_secret')

        trakt.core.AUTH_METHOD = trakt.core.DEVICE_AUTH
        trakt.APPLICATION_ID = client_id

        # Set the CONFIG_PATH to ./config/.pytrakt.json
        trakt.core.CONFIG_PATH = './config/.pytrakt.json'

        try:
            # Initialize Trakt
            trakt.core.OAUTH_CLIENT_ID = client_id
            trakt.core.OAUTH_CLIENT_SECRET = client_secret
            logging.debug("Initializing Trakt...")
            auth = trakt.init(store=True, client_id=client_id, client_secret=client_secret)
            logging.debug("Trakt initialized successfully.")
            success_message = "Trakt authorization completed successfully."

        except Exception as e:
            logging.exception("Error during Trakt authorization")
            success_message = f"Error during Trakt authorization: {str(e)}"

        print(success_message)
        print("\nPress Enter to return to the settings menu...")
        input()

        # Resume urwid
        self.main_loop.screen = saved_screen
        self.main_loop.screen.start()

    def show_settings(self, title, settings):
        self.edits = {}
        widgets = [urwid.Text(title), urwid.Divider()]

        for section, key, label_text in settings:
            value = self.config.get(section, key, fallback='')
            edit = urwid.Edit(('edit', f"{label_text}: "), value)
            edit = urwid.AttrMap(edit, 'edit', 'edit_focus')
            self.edits[(section, key)] = edit
            widgets.append(edit)

        widgets.append(urwid.Divider())
        widgets.append(urwid.AttrMap(urwid.Button("Back to Main Menu", on_press=self.back_to_main_menu), None, focus_map='reversed'))

        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(widgets))
        self.main_loop.widget = urwid.Frame(list_box, footer=urwid.Text("Use arrow keys to navigate, Enter to select, q to quit"))

    def save_settings(self):
        for (section, key), edit in self.edits.items():
            value = edit.original_widget.get_edit_text()
            if not set_setting(section, key, value):
                self.show_error(f"Invalid URL for {section}.{key}: {value}")
        # Reload config to ensure updates are applied
        self.config = load_config()

    def show_error(self, message):
        self.main_loop.widget = urwid.Overlay(
            urwid.LineBox(urwid.Pile([
                urwid.Text(('error', message)),
                urwid.Button('OK', on_press=self.close_error)
            ])),
            self.main_loop.widget,
            'center', ('relative', 50),
            'middle', ('relative', 20)
        )

    def close_error(self, button):
        self.main_loop.widget = self.main_loop.widget.bottom_w

    def back_to_main_menu(self, button):
        self.save_settings()
        self.main_loop.widget = self.build_main_menu()

    def exit_on_q(self, key):
        if key in ('q', 'Q'):
            self.save_settings()
            raise urwid.ExitMainLoop()

if __name__ == "__main__":
    SettingsEditor()
    # Relaunch main script
    # subprocess.run([sys.executable, 'main.py'])
