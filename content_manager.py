import json
import os
import urwid
import logging

CONFIG_FILE = './config/config.json'

class ContentManager:
    def __init__(self, main_loop):
        self.main_loop = main_loop
        self.content_sources = self.load_content_sources()
        self.content_source_types = {
            'MDBList': ['enabled', 'urls', 'versions'],
            'Collected': ['enabled', 'versions'],
            'Trakt Watchlist': ['enabled', 'versions'],
            'Trakt Lists': ['enabled', 'trakt_lists', 'versions'],
            'Overseerr': ['enabled', 'url', 'api_key', 'versions']
        }
        self.back_callback = None

    def load_content_sources(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as config_file:
                    config = json.load(config_file)
                    return config.get('Content Sources', {})
        except Exception as e:
            logging.error(f"Error loading content sources: {str(e)}")
        return {}

    def save_content_sources(self):
        try:
            config = self.load_config()
            config['Content Sources'] = self.content_sources
            with open(CONFIG_FILE, 'w') as config_file:
                json.dump(config, config_file, indent=2)
            logging.debug("Content sources saved successfully")
        except Exception as e:
            logging.error(f"Error saving content sources: {str(e)}")

    def load_config(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as config_file:
                    return json.load(config_file)
        except Exception as e:
            logging.error(f"Error loading config: {str(e)}")
        return {}

    def add_content_source(self, source_type):
        index = 1
        while f"{source_type}_{index}" in self.content_sources:
            index += 1
        new_source = {setting: '' for setting in self.content_source_types[source_type]}
        new_source['enabled'] = False  # Set enabled to False by default
        new_source['versions'] = {'Default': True}  # Set Default version
        self.content_sources[f"{source_type}_{index}"] = new_source
        self.save_content_sources()
        return f"{source_type}_{index}"

    def update_content_source(self, source_id, key, value):
        if source_id in self.content_sources:
            self.content_sources[source_id][key] = value
            self.save_content_sources()
            logging.debug(f"Updated {source_id}: {key} = {value}")

    def remove_content_source(self, source_id):
        if source_id in self.content_sources:
            del self.content_sources[source_id]
            self.save_content_sources()

    def get_content_source(self, source_id):
        return self.content_sources.get(source_id, {})

    def get_all_content_sources(self):
        return self.content_sources

    def show_content_sources_menu(self, back_callback):
        self.back_callback = back_callback
        content_menu = [
            urwid.Text("Content Sources"),
            urwid.Divider()
        ]

        for source_id, source_data in self.content_sources.items():
            content_menu.append(self.create_content_source_item(source_id, source_data))

        content_menu.extend([
            urwid.Divider(),
            urwid.AttrMap(urwid.Button("Add New Content Source", on_press=self.show_add_new_content_source_menu), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Back", on_press=self.back_to_main_menu), None, focus_map='reversed')
        ])

        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(content_menu))
        self.main_loop.widget = urwid.Frame(list_box, footer=urwid.Text("Use arrow keys to navigate, Enter to select"))

    def create_content_source_item(self, source_id, source_data):
        enabled = source_data.get('enabled', False)
        enabled_text = "Enabled" if enabled else "Disabled"
        return urwid.Columns([
            ('weight', 4, urwid.Text(f"{source_id} ({enabled_text})")),
            ('weight', 2, urwid.AttrMap(urwid.Button("Edit", on_press=self.edit_content_source, user_data=source_id), None, focus_map='reversed')),
            ('weight', 2, urwid.AttrMap(urwid.Button("Remove", on_press=self.remove_content_source_ui, user_data=source_id), None, focus_map='reversed'))
        ])

    def show_add_new_content_source_menu(self, button):
        menu = [urwid.Text("Select Content Source Type"), urwid.Divider()]
        for source_type in self.content_source_types.keys():
            menu.append(urwid.AttrMap(urwid.Button(source_type, on_press=self.create_new_content_source, user_data=source_type), None, focus_map='reversed'))
        menu.append(urwid.AttrMap(urwid.Button("Back", on_press=self.return_to_content_sources_menu), None, focus_map='reversed'))
        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(menu))
        self.main_loop.widget = urwid.Frame(list_box)

    def create_new_content_source(self, button, source_type):
        new_source_id = self.add_content_source(source_type)
        self.edit_content_source(None, new_source_id)

    def edit_content_source(self, button, source_id):
        source_data = self.get_content_source(source_id)
        source_type = source_id.split('_')[0]
        menu = [urwid.Text(f"Editing {source_id}"), urwid.Divider()]
        
        self.edit_widgets = {}
        for key in self.content_source_types[source_type]:
            value = source_data.get(key, '')
            if key == 'enabled':
                checkbox = urwid.CheckBox(f"Enabled", state=value, on_state_change=self.on_content_source_checkbox_change, user_data=(source_id, key))
                menu.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
                self.edit_widgets[key] = checkbox
            elif key == 'versions':
                versions = self.load_versions()
                if not versions:
                    versions = ['Default']
                version_checkboxes = []
                for version in versions:
                    checkbox = urwid.CheckBox(f"Version: {version}", state=version in value, on_state_change=self.on_version_checkbox_change, user_data=(source_id, version))
                    version_checkboxes.append(checkbox)
                    menu.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
                self.edit_widgets[key] = version_checkboxes
            else:
                edit = urwid.Edit(f"{key.capitalize()}: ", str(value))
                urwid.connect_signal(edit, 'change', self.on_content_source_edit_change, user_args=[source_id, key])
                menu.append(urwid.AttrMap(edit, None, focus_map='edit_focus'))
                self.edit_widgets[key] = edit
        
        menu.extend([
            urwid.Divider(),
            urwid.AttrMap(urwid.Button("Save", on_press=self.save_content_source_changes, user_data=source_id), None, focus_map='reversed'),
            urwid.AttrMap(urwid.Button("Back", on_press=self.return_to_content_sources_menu), None, focus_map='reversed')
        ])
        
        list_box = urwid.ListBox(urwid.SimpleFocusListWalker(menu))
        self.main_loop.widget = urwid.Frame(list_box)

    def on_content_source_checkbox_change(self, checkbox, new_state, user_data):
        source_id, key = user_data
        self.update_content_source(source_id, key, new_state)

    def on_version_checkbox_change(self, checkbox, new_state, user_data):
        source_id, version = user_data
        current_versions = self.content_sources[source_id]['versions']
        if new_state:
            current_versions[version] = True
        elif version in current_versions and len(current_versions) > 1:
            del current_versions[version]
        else:
            # Prevent unchecking the last version
            checkbox.set_state(True)
        self.update_content_source(source_id, 'versions', current_versions)

    def on_content_source_edit_change(self, edit, new_edit_text, source_id, key):
        # We'll update the content source when the Save button is pressed
        pass

    def save_content_source_changes(self, button, source_id):
        changes_made = False
        new_versions = {}
        for key, widget in self.edit_widgets.items():
            if key == 'versions':
                for checkbox in widget:
                    if checkbox.state:
                        version = checkbox.label.split(': ')[1]
                        new_versions[version] = True
                if not new_versions:
                    self.show_error("At least one version must be selected")
                    return
                if self.get_content_source(source_id).get(key) != new_versions:
                    self.update_content_source(source_id, key, new_versions)
                    changes_made = True
            elif isinstance(widget, urwid.CheckBox):
                value = widget.state
                if self.get_content_source(source_id).get(key) != value:
                    self.update_content_source(source_id, key, value)
                    changes_made = True
            else:
                value = widget.edit_text
                if self.get_content_source(source_id).get(key) != value:
                    self.update_content_source(source_id, key, value)
                    changes_made = True
        
        if changes_made:
            logging.info(f"Changes saved for {source_id}")
        else:
            logging.info("No changes detected")
        self.return_to_content_sources_menu(None)
        
    def remove_content_source_ui(self, button, source_id):
        self.remove_content_source(source_id)
        logging.debug(f"Removed content source {source_id}")
        self.return_to_content_sources_menu(None)

    def return_to_content_sources_menu(self, button):
        self.show_content_sources_menu(self.back_callback)

    def back_to_main_menu(self, button):
        if self.back_callback:
            self.back_callback(button)

    def load_versions(self):
        config = self.load_config()
        versions = list(config.get('Scraping', {}).get('versions', {}).keys())
        return versions if versions else ['Default']
        
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