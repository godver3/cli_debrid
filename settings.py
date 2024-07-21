import configparser
import os
import urwid
import subprocess
import sys

CONFIG_FILE = './config.ini'

def load_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)
    print(f"Configuration saved to {CONFIG_FILE}")

def get_setting(section, key, default=None):
    config = load_config()
    return config.get(section, key, fallback=default)

def set_setting(section, key, value):
    config = load_config()
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)
    save_config(config)

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
            urwid.AttrMap(urwid.Button("Required Settings", on_press=self.show_required_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Additional Settings", on_press=self.show_additional_settings), None, focus_map='reversed'),
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
            ('Trakt', 'client_id', 'Trakt Client ID'),
            ('Trakt', 'client_secret', 'Trakt Client Secret'),
            ('TMDB', 'api_key', 'TMDB API Key'),
        ])

    def show_additional_settings(self, button):
        self.show_settings("Additional Settings", [
            ('Zilean', 'url', 'Zilean URL'),
            ('Knightcrawler', 'url', 'Knightcrawler URL'),
            ('MDBList', 'api_key', 'MDB API Key'),
            ('MDBList', 'urls', 'MDB List URLs')
        ])

    def show_debug_settings(self, button):
        self.show_settings("Debug Settings", [
            ('Logging', 'use_single_log_file', 'Use Single Log File (True/False)'),
            ('Logging', 'logging_level', 'Logging Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)')
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
        print("Saving settings...")
        for (section, key), edit in self.edits.items():
            value = edit.get_edit_text()
            print(f"Setting {section} - {key} to {value}")
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
