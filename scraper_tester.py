import urwid
import configparser
from typing import List, Dict, Any
from scraper.scraper import scrape, rank_result_key
from settings import get_setting, set_setting, get_all_settings
import subprocess, sys

CONFIG_FILE = './config.ini'

class ScraperTester:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config.read(CONFIG_FILE)
        self.settings_widgets = {}
        self.results = []
        self.palette = [
            ('reversed', 'standout', ''),
            ('header', 'white', 'dark blue'),
            ('result', 'black', 'light gray'),
            ('footer', 'white', 'dark blue'),
            ('highlight', 'black', 'light green'),
            ('score_box', 'white', 'dark blue'),
        ]
        self.main_loop = urwid.MainLoop(self.main_view(), self.palette, unhandled_input=self.handle_input)

    def main_view(self):
        self.results_list = self.results_view()
        self.score_box = self.create_score_box()
        main_area = urwid.Columns([
            ('weight', 30, urwid.Pile([
                ('weight', 70, self.settings_view()),
                ('weight', 30, self.score_box)
            ])),
            ('weight', 70, self.results_list),
        ])
        footer = urwid.Text(('footer', "Press 'q' to quit"))
        return urwid.Frame(main_area, footer=footer)

    def settings_view(self):
        widgets = [urwid.Text(('header', "Scraping Settings")), urwid.Divider()]

        scraping_settings = get_all_settings('Scraping')

        for key, value in scraping_settings.items():
            if key.startswith('enable_'):
                if isinstance(value, str):
                    state = value.lower() == 'true'
                elif isinstance(value, bool):
                    state = value
                else:
                    state = False
                checkbox = urwid.CheckBox(key, state=state)
                urwid.connect_signal(checkbox, 'change', self.on_checkbox_change, user_args=['Scraping', key])
                self.settings_widgets[('Scraping', key)] = checkbox
                widgets.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
            else:
                edit = urwid.Edit(f"{key}: ", str(value))
                urwid.connect_signal(edit, 'change', self.on_setting_change, user_args=['Scraping', key])
                self.settings_widgets[('Scraping', key)] = edit
                widgets.append(urwid.AttrMap(edit, None, focus_map='reversed'))

        widgets.append(urwid.Divider())
        widgets.append(urwid.AttrMap(urwid.Button("Refresh Results", on_press=self.refresh_results), None, focus_map='reversed'))

        return urwid.ListBox(urwid.SimpleFocusListWalker(widgets))

    def results_view(self):
        header = urwid.AttrMap(urwid.Columns([
            ('weight', 40, urwid.Text("Name")),
            ('weight', 15, urwid.Text("Size")),
            ('weight', 15, urwid.Text("Est. Mbps")),
            ('weight', 15, urwid.Text("Scraper")),
            ('weight', 15, urwid.Text("Score")),
        ]), 'header')

        self.result_widgets = [urwid.AttrMap(SelectableColumns(self.format_result(result)), None, focus_map='highlight') for result in self.results]
        listwalker = urwid.SimpleFocusListWalker([header, urwid.Divider()] + self.result_widgets)
        return urwid.ListBox(listwalker)

    def format_result(self, result: Dict[str, Any]) -> List[urwid.Widget]:
        title = result.get('title', 'Unknown')
        size = result.get('size', 'Unknown')
        bitrate = result.get('bitrate', 'Unknown')
        scraper = result.get('scraper', 'Unknown')
        score = result.get('score_breakdown', {}).get('total_score', 'N/A')

        return [
            ('weight', 40, urwid.Text(title)),
            ('weight', 15, urwid.Text(f"{size:.2f}" if isinstance(size, (int, float)) else str(size))),
            ('weight', 15, urwid.Text(f"{bitrate:.0f}" if isinstance(bitrate, (int, float)) else str(bitrate))),
            ('weight', 15, urwid.Text(scraper)),
            ('weight', 15, urwid.Text(f"{score:.2f}" if isinstance(score, (int, float)) else str(score))),
        ]

    def create_score_box(self):
        self.score_breakdown_text = urwid.Text("Select a result to see score breakdown")
        return urwid.LineBox(self.score_breakdown_text, title="Score Breakdown")

    def on_setting_change(self, section: str, key: str, widget: urwid.Edit, new_value: str):
        set_setting(section, key, new_value)
        self.config.read(CONFIG_FILE)  # Reload the config

    def on_checkbox_change(self, section: str, key: str, widget: urwid.CheckBox, new_state: bool):
        set_setting(section, key, str(new_state))
        self.config.read(CONFIG_FILE)  # Reload the config

    def refresh_results(self, button):
        imdb_id = 'tt0078748'  # Alien (1979)
        content_type = 'movie'
        self.results = scrape(imdb_id, "Alien", 1979, content_type)

        # Calculate scores for each result
        for result in self.results:
            rank_result_key(result, "Alien", 1979, None, None, False)
            # The score_breakdown is now directly added to the result by rank_result_key

        self.main_loop.widget = self.main_view()

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()
        elif key in ('up', 'down', 'enter'):
            self.update_score_box()

    def update_score_box(self):
        focus_widget, focus_pos = self.results_list.get_focus()
        if isinstance(focus_widget, urwid.AttrMap) and isinstance(focus_widget.original_widget, SelectableColumns):
            result = self.results[focus_pos - 2]  # Adjust for header and divider
            score_breakdown = result.get('score_breakdown', {})
            breakdown_text = ["Score breakdown:"]
            for k, v in score_breakdown.items():
                breakdown_text.append(f"{k}: {v:.2f}")
            self.score_breakdown_text.set_text("\n".join(breakdown_text))
        else:
            self.score_breakdown_text.set_text("Select a result to see score breakdown")

    def run(self):
        self.refresh_results(None)  # Initial load of results
        self.main_loop.run()

class SelectableColumns(urwid.Columns):
    def selectable(self):
        return True

    def keypress(self, size, key):
        return key

def scraper_tester():
    ScraperTester().run()
    #subprocess.run([sys.executable, 'main.py'])

if __name__ == "__main__":
    ScraperTester().run()
