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
import json
import copy

CONFIG_FILE = './config/config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as config_file:
            try:
                config = json.load(config_file)
                return deserialize_config(config)
            except json.JSONDecodeError:
                logging.error(f"Error decoding JSON from {CONFIG_FILE}. Using empty config.")
    return {}

def deserialize_config(config):
    if isinstance(config, dict):
        return {k: deserialize_config(v) for k, v in config.items() if not k.isdigit()}
    elif isinstance(config, list):
        if config and isinstance(config[0], list) and len(config[0]) == 2:
            # This is likely a preferred filter list
            return [tuple(item) for item in config]
        return [deserialize_config(item) for item in config]
    else:
        return config

def save_config(config):
    def serialize(obj):
        if isinstance(obj, dict):
            return {k: serialize(v) for k, v in obj.items() if not k.isdigit()}
        elif isinstance(obj, list):
            return [serialize(item) for item in obj]
        elif isinstance(obj, tuple):
            # Handle tuples (used for preferred filters)
            return list(obj)
        else:
            return obj

    serialized_config = serialize(config)

    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as config_file:
        json.dump(serialized_config, config_file, indent=2)

def validate_url(url):
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        url = f'http://{url}'
    try:
        result = urlparse(url)
        return url if all([result.scheme, result.netloc]) else ''
    except:
        return ''

def get_setting(section, key, default=''):
    config = load_config()
    value = config.get(section, {}).get(key, default)
    
    if key.lower() == 'enabled':
        if isinstance(value, str):
            return value.lower() == 'true'
        return bool(value)
    
    if key.lower().endswith('url'):
        validated_url = validate_url(value)
        if validated_url != value:
            logging.debug(f"URL validation changed value for {section}.{key}: '{value}' -> '{validated_url}'")
        return validated_url
    
    return value

def set_setting(section, key, value):
    config = load_config()
    if section not in config:
        config[section] = {}
    if key.lower().endswith('url'):  # Check if the key ends with 'url' (case-insensitive)
        value = validate_url(value)
    config[section][key] = value
    save_config(config)

def get_all_settings():
    return load_config()

def validate_url(url):
    if not url:
        logging.debug(f"Empty URL provided")
        return ''
    if not url.startswith(('http://', 'https://')):
        url = f'http://{url}'
    try:
        result = urlparse(url)
        if all([result.scheme, result.netloc]):
            return url
        else:
            logging.warning(f"Invalid URL structure: {url}")
            return ''
    except Exception as e:
        logging.error(f"Error parsing URL {url}: {str(e)}")
        return ''

def get_scraping_settings():
    config = load_config()
    scraping_settings = {}

    versions = config.get('Scraping', {}).get('versions', {})
    for version, settings in versions.items():
        for key, value in settings.items():
            label = f"{version.capitalize()} - {key.replace('_', ' ').title()}"
            scraping_settings[f"{version}_{key}"] = (label, value)

    return scraping_settings

def ensure_settings_file():
    config = load_config()
    default_settings = {
        'Plex': {
            'url': '',
            'token': '',
            'movie_libraries': '',
            'shows_libraries': ''
        },
        'Overseerr': {
            'url': '',
            'api_key': ''
        },
        'RealDebrid': {
            'api_key': ''
        },
        'Torrentio': {
            'enabled': 'False'
        },
        'Debug': {
            'skip_menu': 'False',
            'logging_level': 'INFO'
        }
    }

    for section, settings in default_settings.items():
        if section not in config:
            config[section] = {}
        for key, default_value in settings.items():
            if key not in config[section] or not config[section][key]:
                config[section][key] = default_value

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
        self.main_loop.original_widget = self.main_loop.widget
        self.main_loop.run()

    def build_main_menu(self):
        menu = urwid.Pile([
            urwid.Text("Settings Editor (press 'q' to quit)"),
            urwid.Text("Use Shift-Insert to paste values."),
            urwid.AttrMap(urwid.Button("Required Settings", on_press=self.show_required_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Scrapers", on_press=self.show_scrapers), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Scraping Settings", on_press=self.show_scraping_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Content Settings", on_press=self.show_content_settings), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Additional Settings", on_press=self.show_additional_settings), None, focus_map='reversed'),
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


    def show_scrapers(self, button):
        self.show_settings("Scrapers", [
            ('Zilean', 'url', 'Zilean - Zilean URL'),
            ('Comet', 'url', 'Comet - Comet URL'),
            ('Jackett', 'url', 'Jackett - Jackett URL'),
            ('Jackett', 'api', 'Jackett - Jackett API'),
            ('Prowlarr', 'url', 'Jackett - Jackett URL'),
            ('Prowlarr', 'api', 'Jackett - Jackett API')
        ])

    def show_content_settings(self, button):
        self.show_settings("Additional Settings", [
            ('MDBList Content Source', 'urls', 'MDBList - MDB List URLs'),
            ('Collected Content Source', 'enabled', 'Collected - Enable collected content source? True/False'),
            ('Trakt', 'user_watchlist_enabled', 'Trakt - Enable your watchlist as a content source? True/False (Requires auth. below)'),
            ('Trakt', 'trakt_lists', 'Trakt - Add any other Trakt lists as content sources (comma-separated)')
        ])

    def show_additional_settings(self, button):
        self.show_settings("Additional Settings", [
            ('TMDB', 'api_key', 'TMDB - TMDB API Key'),
            ('Queue', 'wake_limit', 'Queue - Enter number of times to wake items before blacklisting'),
            ('Scraping', 'uncached_content_handling', 'Scraping - Uncached content handling (None/Hybrid/Full)'),
            ('Trakt', 'client_id', 'Trakt - Enter Trakt client ID'),
            ('Trakt', 'client_secret', 'Trakt - Enter Trakt client secret')
        ])
        # Add Trakt OAuth button
        self.main_loop.widget.body.body.insert(-2, urwid.AttrMap(
            urwid.Button("Authorize Trakt (must add client_id/secret first)", on_press=self.start_trakt_oauth),
            None, focus_map='reversed'))


    def show_scraping_settings(self, button):
        ScrapingSettingsEditor(self.main_loop).show_versions_menu()
        
    def show_debug_settings(self, button):
        self.show_settings("Debug Settings", [
            ('Debug', 'logging_level', 'Logging - Logging Level (DEBUG, INFO, WARNING, ERROR, CRITICAL)'),
            ('Debug', 'skip_initial_plex_update', 'Menu - Skip Plex initial collection scan (True/False)'),
            ('Debug', 'skip_menu', 'Menu - Skip menu? (True/False)'),
            ('Debug', 'disable_initialization', 'Menu - Disable initialization tasks? (True/False)'),
            ('Debug', 'api_key', 'TMDB - TMDB API Key')
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
            value = get_setting(section, key, '')
            edit = urwid.Edit(('edit', f"{label_text}: "), str(value))
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
            set_setting(section, key, value)
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

class ScrapingSettingsEditor:
    def __init__(self, main_loop):
        self.main_loop = main_loop
        self.current_version = None
        self.versions = self.load_versions()
        self.resolution_options = ['2160p', '1080p', '720p', 'SD']
        self.resolution_wanted_options = ['<=', '==', '>=']
        self.previous_widget = None

    def load_versions(self):
        versions = get_setting('Scraping', 'versions', {})
        if not versions:
            versions = {'Default': self.get_default_settings()}
            self.save_versions(versions)
        return versions

    def save_versions(self, versions=None):
        if versions is None:
            versions = self.versions
        clean_versions = {}
        for version_name, settings in versions.items():
            clean_settings = {}
            for key, value in settings.items():
                if isinstance(value, urwid.Widget):
                    if isinstance(value, urwid.Edit):
                        clean_settings[key] = value.edit_text
                    elif isinstance(value, urwid.IntEdit):
                        try:
                            clean_settings[key] = int(value.edit_text)
                        except ValueError:
                            clean_settings[key] = 0  # Default to 0 if invalid
                    elif isinstance(value, urwid.CheckBox):
                        clean_settings[key] = value.state
                elif isinstance(value, list):
                    clean_settings[key] = [
                        (item[0], int(item[1])) if isinstance(item, tuple) else item
                        for item in value
                    ]
                else:
                    clean_settings[key] = value
            clean_versions[version_name] = clean_settings
        set_setting('Scraping', 'versions', clean_versions)

    def get_default_settings(self):
        return {
            'enable_hdr': False,
            'max_resolution': '1080p',
            'resolution_wanted': '<=',
            'resolution_weight': 3,
            'hdr_weight': 3,
            'similarity_weight': 3,
            'size_weight': 3,
            'bitrate_weight': 3,
            'preferred_filter_in': [],
            'preferred_filter_out': [],
            'filter_in': [],
            'filter_out': [],
            'min_size_gb': 0.01
        }

    def show_versions_menu(self, button=None):
        menu_items = [
            urwid.Text("Scraping Settings Versions"),
            urwid.Divider(),
            urwid.AttrMap(urwid.Button("Add New Version", on_press=self.add_new_version), None, focus_map='reversed'),
        ]

        for version_name in self.versions.keys():
            menu_items.append(urwid.Columns([
                ('weight', 6, urwid.AttrMap(urwid.Button(version_name, on_press=self.edit_version, user_data=version_name), None, focus_map='reversed')),
                ('weight', 2, urwid.AttrMap(urwid.Button("Duplicate", on_press=self.duplicate_version, user_data=version_name), None, focus_map='reversed')),
                ('weight', 2, urwid.AttrMap(urwid.Button("Rename", on_press=self.rename_version, user_data=version_name), None, focus_map='reversed')),
                ('weight', 2, urwid.AttrMap(urwid.Button("Delete", on_press=self.delete_version, user_data=version_name), None, focus_map='reversed')),
            ]))

        menu_items.append(urwid.Divider())
        menu_items.append(urwid.AttrMap(urwid.Button("Back to Main Menu", on_press=self.back_to_main_menu), None, focus_map='reversed'))

        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(menu_items))
        self.main_loop.widget = urwid.Frame(list_box, footer=urwid.Text("Use arrow keys to navigate, Enter to select"))

    def add_new_version(self, button):
        self.edit_version_name(None)

    def duplicate_version(self, button, version_name):
        new_name = f"{version_name}_copy"
        counter = 1
        while new_name in self.versions:
            new_name = f"{version_name}_copy_{counter}"
            counter += 1
        self.versions[new_name] = copy.deepcopy(self.versions[version_name])
        self.save_versions()
        self.show_versions_menu()

    def delete_version(self, button, version_name):
        if len(self.versions) > 1:
            del self.versions[version_name]
            self.save_versions()
            self.show_versions_menu()
        else:
            self.show_error("Cannot delete the last version")

    def edit_version(self, button, version_name):
        self.current_version = version_name
        settings = self.versions[version_name]

        widgets = [
            urwid.Text(f"Editing Version: {version_name}"),
            urwid.Divider(),
        ]

        for key, value in settings.items():
            if key in ['resolution_weight', 'hdr_weight', 'similarity_weight', 'size_weight', 'bitrate_weight']:
                caption = f"{key.replace('_', ' ').title()}: "
                edit = urwid.IntEdit(caption, value)
                urwid.connect_signal(edit, 'change', lambda widget, new_text, user_data=key: self.on_weight_change(new_text, user_data))
                widgets.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))
            elif key == 'max_resolution':
                widgets.append(urwid.Text("Resolution:"))
                radio_buttons = []
                for option in self.resolution_options:
                    radio = urwid.RadioButton(radio_buttons, option, state=(option == value),
                                              on_state_change=self.on_resolution_change)
                    widgets.append(urwid.AttrMap(radio, None, focus_map='reversed'))
            elif key == 'resolution_wanted':                  
                widgets.append(urwid.Text("Resolution Symbol:"))
                radio_buttons = []
                for option in self.resolution_wanted_options:
                    radio = urwid.RadioButton(radio_buttons, option, state=(option == value),
                                              on_state_change=self.on_resolution_wanted_change)
                    widgets.append(urwid.AttrMap(radio, None, focus_map='reversed'))
            elif isinstance(value, bool):
                checkbox = urwid.CheckBox(key.replace('_', ' ').title(), state=value,
                                          on_state_change=self.on_checkbox_change)
                widgets.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
            elif isinstance(value, (int, float)):
                caption = f"{key.replace('_', ' ').title()}: "
                if isinstance(value, int):
                    edit = urwid.IntEdit(caption, str(value))
                else:
                    edit = urwid.Edit(caption, str(value))
                urwid.connect_signal(edit, 'change', self.on_edit_change, user_args=[key])
                widgets.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))
            elif isinstance(value, list):
                widgets.append(urwid.Text(f"{key.replace('_', ' ').title()}:"))
                if value:
                    for item in value:
                        if isinstance(item, tuple):
                            widgets.append(urwid.Text(f"  - {item[0]} (weight: {item[1]})"))
                        else:
                            widgets.append(urwid.Text(f"  - {item}"))
                else:
                    widgets.append(urwid.Text("  (empty)"))
                widgets.append(urwid.AttrMap(
                    urwid.Button("Edit List", on_press=self.edit_filter_list, user_data=(key, value)),
                    None, focus_map='reversed'
                ))
            else:
                edit = urwid.Edit(f"{key.replace('_', ' ').title()}: ", str(value))
                urwid.connect_signal(edit, 'change', self.on_edit_change, user_args=[key])
                widgets.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))

        widgets.extend([
            urwid.Divider(),
            urwid.AttrMap(urwid.Button("Back", on_press=self.show_versions_menu), None, focus_map='reversed'),
        ])

        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(widgets))
        self.main_loop.widget = urwid.Frame(list_box)

    def on_resolution_wanted_change(self, radio_button, new_state):
        if new_state:
            self.versions[self.current_version]['resolution_wanted'] = radio_button.label
            self.save_versions()

    def on_checkbox_change(self, checkbox, new_state):
        key = checkbox.label.lower().replace(' ', '_')
        self.versions[self.current_version][key] = new_state
        self.save_versions()

    def on_resolution_change(self, radio_button, new_state):
        if new_state:
            self.versions[self.current_version]['max_resolution'] = radio_button.label
            self.save_versions()

    def on_edit_change(self, edit, new_edit_text, key):
        if key in ['resolution_weight', 'hdr_weight', 'similarity_weight', 'size_weight', 'bitrate_weight']:
            try:
                self.versions[self.current_version][key] = int(new_edit_text)
            except ValueError:
                pass  # Ignore invalid input
        elif key == 'min_size_gb':
            try:
                self.versions[self.current_version][key] = float(new_edit_text)
            except ValueError:
                pass  # Ignore invalid input
        else:
            self.versions[self.current_version][key] = new_edit_text
        self.save_versions()

    def on_weight_change(self, new_text, key):
        try:
            new_value = int(new_text)
            self.versions[self.current_version][key] = new_value
            self.save_versions()
        except ValueError:
            # Ignore invalid input
            pass

    def back_to_main_menu(self, button):
        self.main_loop.widget = self.main_loop.original_widget

    def edit_filter_list(self, button, user_data):
        key, value = user_data
        FilterListEditor(self.main_loop, self, key, value)

    def rename_version(self, button, version_name):
        self.edit_version_name(version_name)

    def edit_version_name(self, version_name):
        name_edit = urwid.Edit("Version name: ", version_name or "")
        save_button = urwid.Button("Save", on_press=self.save_version_name, user_data=(name_edit, version_name))
        cancel_button = urwid.Button("Cancel", on_press=self.show_versions_menu)

        pile = urwid.Pile([
            name_edit,
            urwid.Divider(),
            urwid.Columns([('pack', save_button), ('pack', cancel_button)])
        ])

        self.main_loop.widget = urwid.Filler(urwid.LineBox(pile))

    def save_version_name(self, button, user_data):
        name_edit, old_name = user_data
        new_name = name_edit.edit_text.strip()

        if not new_name:
            self.show_error("Version name cannot be empty")
            return

        if old_name and old_name in self.versions:
            self.versions[new_name] = self.versions.pop(old_name)
        else:
            self.versions[new_name] = self.get_default_settings()

        self.save_versions()
        self.edit_version(None, new_name)

    def save_filter_list(self, key, new_value):
        self.versions[self.current_version][key] = new_value
        self.save_versions()
        self.edit_version(None, self.current_version)

    def show_error(self, message):
        self.previous_widget = self.main_loop.widget
        error_widget = urwid.LineBox(urwid.Pile([
            urwid.Text(('error', message)),
            urwid.Button('OK', on_press=self.close_error)
        ]))
        self.main_loop.widget = urwid.Overlay(error_widget, self.previous_widget,
                                              align='center', width=('relative', 50),
                                              valign='middle', height=('relative', 20))

    def close_error(self, button):
        self.main_loop.widget = self.previous_widget

class FilterListEditor:
    def __init__(self, main_loop, scraping_editor, key, value):
        self.main_loop = main_loop
        self.scraping_editor = scraping_editor
        self.key = key
        self.value = value
        self.is_weighted = key.startswith('preferred_filter') or key == 'preferred_filter_out'
        self.show_list()

    def on_term_change(self, new_text, index):
        if self.is_weighted:
            current_item = self.value[index]
            if isinstance(current_item, tuple):
                self.value[index] = (new_text, current_item[1])
            else:
                self.value[index] = (new_text, 1)
        else:
            self.value[index] = new_text
        self.save_changes()

    def on_weight_change(self, new_text, index):
        if self.is_weighted:
            try:
                new_weight = int(new_text)
                current_item = self.value[index]
                if isinstance(current_item, tuple):
                    self.value[index] = (current_item[0], new_weight)
                else:
                    self.value[index] = (str(current_item), new_weight)
                self.save_changes()
            except ValueError:
                pass  # Ignore invalid input

    def save_changes(self):
        if self.is_weighted:
            # Ensure all items are tuples with both term and weight
            self.value = [(str(item[0]), int(item[1])) if isinstance(item, tuple) else (str(item), 1) for item in self.value]
        else:
            # Ensure all items are strings for non-weighted lists
            self.value = [str(item) for item in self.value]

        self.scraping_editor.versions[self.scraping_editor.current_version][self.key] = self.value
        self.scraping_editor.save_versions()

    def show_list(self):
        widgets = [
            urwid.Text(f"Editing {self.key.replace('_', ' ').title()}"),
            urwid.Divider(),
        ]

        for index, item in enumerate(self.value):
            widgets.append(self.create_item_widget(item, index))

        widgets.extend([
            urwid.Divider(),
            urwid.AttrMap(urwid.Button("Add New Item", on_press=self.add_new_item), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Back", on_press=self.back_to_version_edit), None, focus_map='reversed'),
        ])

        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(widgets))
        self.main_loop.widget = urwid.Frame(list_box)

    def create_item_widget(self, item, index):
        if self.is_weighted:
            term, weight = item if isinstance(item, tuple) else (str(item), 1)
            term_edit = urwid.Edit("Term: ", str(term))
            weight_edit = urwid.IntEdit("Weight: ", str(weight))
            urwid.connect_signal(term_edit, 'change', lambda w, text: self.on_term_change(text, index))
            urwid.connect_signal(weight_edit, 'change', lambda w, text: self.on_weight_change(text, index))
            return urwid.Columns([
                ('weight', 4, term_edit),
                ('weight', 2, weight_edit),
                ('weight', 1, urwid.Button("Remove", on_press=self.remove_item, user_data=index))
            ])
        else:
            term_edit = urwid.Edit("Term: ", str(item))
            urwid.connect_signal(term_edit, 'change', lambda w, text: self.on_term_change(text, index))
            return urwid.Columns([
                ('weight', 6, term_edit),
                ('weight', 1, urwid.Button("Remove", on_press=self.remove_item, user_data=index))
            ])

    def add_new_item(self, button):
        if self.is_weighted:
            self.value.append(("", 1))
        else:
            self.value.append("")
        self.save_changes()
        self.show_list()

    def remove_item(self, button, index):
        del self.value[index]
        self.save_changes()
        self.show_list()

    def back_to_version_edit(self, button):
        self.scraping_editor.edit_version(None, self.scraping_editor.current_version)

if __name__ == "__main__":
    SettingsEditor()
    # Relaunch main script
    # subprocess.run([sys.executable, 'main.py'])
