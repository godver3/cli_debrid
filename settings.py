import urwid
import configparser
import os
import subprocess
import sys
import logging

CONFIG_FILE = './config.ini'

def load_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)

def get_setting(section, option, default=None):
    config = configparser.ConfigParser()
    config.read('config.ini')  # Ensure this path is correct
    if config.has_option(section, option):
        raw_value = config.get(section, option)
        logging.debug(f"Setting found - Section: {section}, Option: {option}, Raw Value: {raw_value}")

        if raw_value.strip() == '':
            logging.debug(f"Empty value found for {section}.{option}, using default: {default}")
            return default

        if raw_value.lower() in ['true', 'yes', '1']:
            value = True
        elif raw_value.lower() in ['false', 'no', '0']:
            value = False
        else:
            value = raw_value

        logging.debug(f"Parsed value - Section: {section}, Option: {option}, Value: {value}, Type: {type(value)}")
        return value
    else:
        logging.debug(f"Setting not found - Section: {section}, Option: {option}, using default: {default}")
        return default

def set_setting(section, key, value):
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)
    save_config(config)

def get_all_settings(section):
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    return dict(config[section])

class SettingsEditor:
    def __init__(self):
        self.config = load_config()
        self.edits = {}
        self.palette = [
            ('reversed', 'standout', '')
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
            ('Plex', 'url', 'Plex URL'),
            ('Plex', 'token', 'Plex Token'),
            ('Overseerr', 'url', 'Overseerr URL'),
            ('Overseerr', 'api_key', 'Overseerr API Key'),
            ('RealDebrid', 'api_key', 'Real-Debrid API Key'),
            ('Torrentio', 'enabled', 'Torrentio enabled? True/False')
        ])

    def show_additional_settings(self, button):
        self.show_settings("Additional Settings", [
            ('Zilean', 'url', 'Zilean URL'),
            ('Zilean', 'enabled', 'Zilean enabled? True/False'),
            ('Knightcrawler', 'url', 'Knightcrawler URL'),
            ('Knightcrawler', 'enabled', 'Knightcrawler enabled? (True/False)'),
            ('Comet', 'url', 'Comet URL'),
            ('Comet', 'enabled', 'Comet enabled? True/False'),
            ('MDBList', 'api_key', 'MDB API Key'),
            ('MDBList', 'urls', 'MDB List URLs'),
            ('Trakt', 'client_id', 'Trakt Client ID'),
            ('Trakt', 'client_secret', 'Trakt Client Secret'),
            ('TMDB', 'api_key', 'TMDB API Key'),
            ('Queue', 'wake_limit', 'Enter number of times to wake items before blacklisting')
        ])

    def show_scraping_settings(self, button):
        self.show_settings("Scraping Settings", [
            ('Scraping', 'enable_4k', '4k enabled? True/False'),
            ('Scraping', 'enable_hdr', 'HDR enabled? True/False'),
            ('Scraping', 'resolution_bonus', 'Resolution bonus (1-5)'),
            ('Scraping', 'hdr_bonus', 'HDR bonus (1-5)'),
            ('Scraping', 'similarity_threshold_bonus', 'Title similarity threshold bonus (1-5)'),
            ('Scraping', 'file_size_bonus', 'File size bonus (1-5)'),
            ('Scraping', 'bitrate_bonus', 'Bitrate bonus (1-5)'),
            ('Scraping', 'preferred_filter_in', 'Preferred filter-in terms (comma-separated)'),
            ('Scraping', 'preferred_filter_out', 'Preferred filter-out terms (comma-separated)'),
            ('Scraping', 'filter_in', 'Required filter-in terms (comma-separated)'),
            ('Scraping', 'filter_out', 'Required filter-out terms (comma-separated)'),
            ('Scraping', 'min_size_gb', 'Minimum file size in GB (e.g., 0.01)')
        ])
        
    def show_debug_settings(self, button):
        self.show_settings("Debug Settings", [
            ('Logging', 'use_single_log_file', 'Use Single Log File (True/False)'),
            ('Logging', 'logging_level', 'Logging Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)'),
            ('Plex', 'skip_initial_plex_update', 'Skip Plex initial collection scan (True/False)'),
            ('Logging', 'skip_menu', 'Skip menu? (True/False)')
        ])

    def show_settings(self, title, settings):
        self.edits = {}
        widgets = [urwid.Text(title), urwid.Divider()]

        for section, key, label_text in settings:
            value = self.config.get(section, key, fallback='')
            edit = urwid.Edit(f"{label_text}: ", value)
            self.edits[(section, key)] = edit
            widgets.append(urwid.AttrMap(edit, None, focus_map='reversed'))

        widgets.append(urwid.Divider())
        widgets.append(urwid.AttrMap(urwid.Button("Back to Main Menu", on_press=self.back_to_main_menu), None, focus_map='reversed'))

        self.main_loop.widget = urwid.ListBox(urwid.SimpleFocusListWalker(widgets))

    def save_settings(self):
        for (section, key), edit in self.edits.items():
            value = edit.get_edit_text()
            set_setting(section, key, value)
        # Reload config to ensure updates are applied
        self.config = load_config()

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
    subprocess.run([sys.executable, 'main.py'])
