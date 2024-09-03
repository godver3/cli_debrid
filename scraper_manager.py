import json
import os
import urwid
from urllib.parse import urlparse
import logging

CONFIG_FILE = './config/config.json'

class ScraperManager:
    def __init__(self, main_loop=None):
        self.main_loop = main_loop
        self.config = self.load_config()
        self.scrapers = self.config.get('Scrapers', {})
        self.scraper_settings = {
            'Zilean': ['enabled', 'url'],
            'Comet': ['enabled', 'url'],
            'Jackett': ['enabled', 'url', 'api', 'enabled_indexers'],
            'Prowlarr': ['enabled', 'url', 'api'],
            'Torrentio': ['enabled', 'opts'],
            'Nyaa': ['enabled', 'url', 'categories', 'filter']
        }
        self.back_callback = None

    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as config_file:
                    return json.load(config_file)
        except Exception as e:
            logging.error(f"Error loading config: {str(e)}")
        return {}

    def save_config(self):
        try:
            with open(CONFIG_FILE, 'w') as config_file:
                json.dump(self.config, config_file, indent=2)
            logging.info("Config saved successfully")
        except Exception as e:
            logging.error(f"Error saving config: {str(e)}")

    def add_scraper(self, scraper_type):
        index = 1
        while f"{scraper_type}_{index}" in self.scrapers:
            index += 1
        new_scraper_id = f"{scraper_type}_{index}"
        new_scraper = {setting: '' for setting in self.scraper_settings[scraper_type]}
        new_scraper['enabled'] = False
        self.scrapers[new_scraper_id] = new_scraper
        self.config['Scrapers'] = self.scrapers
        self.save_config()
        return new_scraper_id

    def update_scraper(self, scraper_id, key, value):
        if scraper_id in self.scrapers:
            if key == 'enabled':
                value = value == 'on' or value == True
            elif key == 'url':
                value = self.validate_url(value)
            self.scrapers[scraper_id][key] = value
            self.config['Scrapers'] = self.scrapers
            self.save_config()
            logging.info(f"Updated {scraper_id}: {key} = {value}")

    def load_scrapers(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as config_file:
                    config = json.load(config_file)
                    return config.get('Scrapers', {})
        except Exception as e:
            logging.debug(f"Error loading scrapers: {str(e)}")
        return {}
        
    def remove_scraper(self, scraper_id):
        if scraper_id in self.scrapers:
            del self.scrapers[scraper_id]
            self.config['Scrapers'] = self.scrapers
            self.save_config()
            logging.info(f"Removed scraper {scraper_id}")

    def get_scraper(self, scraper_id):
        return self.scrapers.get(scraper_id, {})

    def get_all_scrapers(self):
        return self.scrapers

    def get_scraper_types(self):
        return list(self.scraper_settings.keys())

    def show_scrapers_menu(self, back_callback):
        if self.main_loop:
            self.back_callback = back_callback
            scraper_menu = [
                urwid.Text("Scrapers"),
                urwid.Divider()
            ]

            for scraper_id, scraper_data in self.scrapers.items():
                scraper_menu.append(self.create_scraper_item(scraper_id, scraper_data))

            scraper_menu.extend([
                urwid.Divider(),
                urwid.AttrMap(urwid.Button("Add New Scraper", on_press=self.show_add_new_scraper_menu), None, focus_map='reversed'),
                urwid.AttrMap(urwid.Button("Back", on_press=self.back_to_main_menu), None, focus_map='reversed')
            ])

            list_box = urwid.ListBox(urwid.SimpleFocusListWalker(scraper_menu))
            self.main_loop.widget = urwid.Frame(list_box, footer=urwid.Text("Use arrow keys to navigate, Enter to select"))
            pass
        else:
            logging.warning("show_scrapers_menu called without main_loop, ignoring.")

    def create_scraper_item(self, scraper_id, scraper_data):
        return urwid.Columns([
            ('weight', 4, urwid.Text(scraper_id)),
            ('weight', 2, urwid.AttrMap(urwid.Button("Edit", on_press=self.edit_scraper, user_data=scraper_id), None, focus_map='reversed')),
            ('weight', 2, urwid.AttrMap(urwid.Button("Remove", on_press=self.remove_scraper_ui, user_data=scraper_id), None, focus_map='reversed'))
        ])

    def show_add_new_scraper_menu(self, button):
        menu = [urwid.Text("Select Scraper Type"), urwid.Divider()]
        for scraper_type in self.scraper_settings.keys():
            menu.append(urwid.AttrMap(urwid.Button(scraper_type, on_press=self.create_new_scraper, user_data=scraper_type), None, focus_map='reversed'))
        menu.append(urwid.AttrMap(urwid.Button("Back", on_press=self.return_to_scrapers_menu), None, focus_map='reversed'))
        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(menu))
        self.main_loop.widget = urwid.Frame(list_box)

    def create_new_scraper(self, button, scraper_type):
        new_scraper_id = self.add_scraper(scraper_type)
        self.edit_scraper(None, new_scraper_id)

    def edit_scraper(self, button, scraper_id):
        if self.main_loop:
            scraper_data = self.get_scraper(scraper_id)
            scraper_type = scraper_id.split('_')[0]
            menu = [urwid.Text(f"Editing {scraper_id}"), urwid.Divider()]
            
            for key in self.scraper_settings[scraper_type]:
                value = scraper_data.get(key, '')
                if key == 'enabled':
                    checkbox = urwid.CheckBox(f"{scraper_type} - {key}", state=value, on_state_change=self.on_scraper_checkbox_change, user_data=(scraper_id, key))
                    menu.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
                elif key == 'opts':
                    edit = urwid.Edit(f"{scraper_type} - {key} (leave blank for sane defaults): ", str(value))
                    urwid.connect_signal(edit, 'change', self.on_scraper_edit_change, user_args=[scraper_id, key])
                    menu.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))
                else:
                    edit = urwid.Edit(f"{scraper_type} - {key}: ", str(value))
                    urwid.connect_signal(edit, 'change', self.on_scraper_edit_change, user_args=[scraper_id, key])
                    menu.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))
            
            menu.extend([
                urwid.Divider(),
                urwid.AttrMap(urwid.Button("Save", on_press=self.save_scraper_changes, user_data=scraper_id), None, focus_map='reversed'),
                urwid.AttrMap(urwid.Button("Back", on_press=self.return_to_scrapers_menu), None, focus_map='reversed')
            ])
            
            self.edit_widgets = {key: widget for key, widget in zip(self.scraper_settings[scraper_type], menu[2:-2])}
            list_box = urwid.ListBox(urwid.SimpleFocusListWalker(menu))
            self.main_loop.widget = urwid.Frame(list_box)
            pass
        else:
            logging.warning("edit_scraper called without main_loop, ignoring.")

    def on_scraper_checkbox_change(self, checkbox, new_state, user_data):
        scraper_id, key = user_data
        self.update_scraper(scraper_id, key, new_state)

    def on_scraper_edit_change(self, edit, new_edit_text, scraper_id, key):
        # We'll update the scraper when the Save button is pressed
        pass

    def save_scraper_changes(self, button, scraper_id):
        changes_made = False
        for key, widget in self.edit_widgets.items():
            if isinstance(widget.original_widget, urwid.CheckBox):
                value = widget.original_widget.state
            else:
                value = widget.original_widget.edit_text
            
            if key.lower().endswith('url'):
                validated_url = validate_url(value)
                if validated_url:
                    if validated_url != value:
                        logging.debug(f"URL adjusted: {value} -> {validated_url}")
                    self.update_scraper(scraper_id, key, validated_url)
                    changes_made = True
                else:
                    logging.debug(f"Invalid URL for {key}: {value}")
                    return  # Stop processing if an invalid URL is found
            else:
                if self.get_scraper(scraper_id).get(key) != value:
                    self.update_scraper(scraper_id, key, value)
                    changes_made = True
        
        if changes_made:
            logging.debug(f"Changes saved for {scraper_id}")
        else:
            logging.debug("No changes detected")
        self.return_to_scrapers_menu(None)

    def remove_scraper_ui(self, button, scraper_id):
        self.remove_scraper(scraper_id)
        logging.debug(f"Removed scraper {scraper_id}")
        self.return_to_scrapers_menu(None)

    def return_to_scrapers_menu(self, button):
        self.show_scrapers_menu(self.back_callback)

    def back_to_main_menu(self, button):
        if self.back_callback:
            self.back_callback(button)
            
    @staticmethod
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