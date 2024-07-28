import urwid, os
import configparser
from typing import List, Dict, Any
from scraper.scraper import scrape, rank_result_key, parse_size, calculate_bitrate
from settings import set_setting, get_all_settings
from utilities.manual_scrape import imdb_id_to_title_and_year
import logging

CONFIG_FILE = './config.ini'

class ScraperTester:
    def __init__(self, imdb_id: str, title: str, year: int, movie_or_episode: str, season: int = None, episode: int = None, multi: bool = False):
        self.config = configparser.ConfigParser()
        self.config.read(CONFIG_FILE)
        self.settings_widgets = {}
        self.results = []
        self.imdb_id = imdb_id
        self.title = title
        self.year = year
        self.movie_or_episode = movie_or_episode
        self.season = season
        self.episode = episode
        self.multi = multi
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
                ('weight', 60, self.settings_view()),
                ('weight', 40, self.score_box)
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
        
        # Calculate bitrate if it's not already present
        if 'bitrate' not in result:
            size_gb = parse_size(size)
            runtime = result.get('runtime', 0)
            result['bitrate'] = calculate_bitrate(size_gb, runtime)
        
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

    def on_setting_change(self, section: str, key: str, widget: urwid.Edit, new_value: str):
        set_setting(section, key, new_value)
        self.config.read(CONFIG_FILE)  # Reload the config

    def on_checkbox_change(self, section: str, key: str, widget: urwid.CheckBox, new_state: bool):
        set_setting(section, key, str(new_state))
        self.config.read(CONFIG_FILE)  # Reload the config

    def refresh_results(self, button):
        self.results = scrape(self.imdb_id, self.title, self.year, self.movie_or_episode, self.season, self.episode, self.multi)

        # Calculate scores for each result
        for result in self.results:
            rank_result_key(result, self.results, self.title, self.year, self.season, self.episode, self.multi)
            # Ensure bitrate is calculated and stored in the result
            if 'bitrate' not in result:
                size_gb = parse_size(result.get('size', 0))
                runtime = result.get('runtime', 0)
                result['bitrate'] = calculate_bitrate(size_gb, runtime)

        # Sort the results based on the total score
        self.results.sort(key=lambda x: x.get('score_breakdown', {}).get('total_score', 0), reverse=True)

        self.main_loop.widget = self.main_view()

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()
            
        elif key in ('up', 'down', 'enter'):
            self.update_score_box()

    def create_score_box(self):
        self.score_breakdown_text = urwid.Text("Select a result to see score breakdown")
        self.score_columns = urwid.Columns([])
        score_pile = urwid.Pile([self.score_breakdown_text, self.score_columns])
        self.score_box = urwid.LineBox(urwid.Filler(score_pile, valign='top'), title="Score Breakdown")
        return self.score_box

    def update_score_box(self):
        focus_widget, focus_pos = self.results_list.get_focus()
        if isinstance(focus_widget, urwid.AttrMap) and isinstance(focus_widget.original_widget, SelectableColumns):
            try:
                result = self.results[focus_pos - 2]  # Adjust for header and divider
                score_breakdown = result.get('score_breakdown', {})
                
                # Create two columns for the score breakdown
                left_column = []
                right_column = []
                for i, (k, v) in enumerate(score_breakdown.items()):
                    text = f"{k}: {v:.2f}"
                    if i % 2 == 0:
                        left_column.append(text)
                    else:
                        right_column.append(text)
                
                # Create Text widgets for each column
                left_text = urwid.Text("\n".join(left_column))
                right_text = urwid.Text("\n".join(right_column))
                
                # Update the Columns widget
                self.score_columns.contents = [
                    (left_text, self.score_columns.options()),
                    (right_text, self.score_columns.options())
                ]
                
                # Update the header text
                self.score_breakdown_text.set_text("Score breakdown:")
                
                logging.debug(f"Updated score breakdown: {score_breakdown}")
            except Exception as e:
                error_message = f"Error updating score breakdown: {str(e)}"
                self.score_breakdown_text.set_text(error_message)
                self.score_columns.contents = []
                logging.error(error_message)
        else:
            self.score_breakdown_text.set_text("Select a result to see score breakdown")
            self.score_columns.contents = []

        # Force a redraw of the main loop
        if hasattr(self, 'main_loop') and self.main_loop is not None:
            self.main_loop.draw_screen()
        
    def run(self):
        self.refresh_results(None)  # Initial load of results
        self.main_loop.run()

class SelectableColumns(urwid.Columns):
    def selectable(self):
        return True

    def keypress(self, size, key):
        return key

def scraper_tester(imdb_id: str, title: str, year: int, movie_or_episode: str, season: int = None, episode: int = None, multi: bool = False):
    ScraperTester(imdb_id, title, year, movie_or_episode, season, episode, multi).run()

def run_tester():
    imdb_id = input("Enter IMDb ID: ")
    title = input("Enter title (optional, press Enter to fetch from IMDb ID): ")
    year = input("Enter year (optional, press Enter to fetch from IMDb ID): ")
    movie_or_episode = input("Enter type (movie or episode): ")
    season = input("Enter season number (if applicable): ")
    episode = input("Enter episode number (if applicable): ")
    multi = input("Enter multi-pack (if applicable - true or false): ").strip().lower() == 'true'

    if not title or not year:
        fetched_title, fetched_year = imdb_id_to_title_and_year(imdb_id, movie_or_episode)
        if not title:
            title = fetched_title
        if not year:
            year = str(fetched_year)

    os.system('clear')
    scraper_tester(imdb_id, title, int(year), movie_or_episode, int(season) if season else None, int(episode) if episode else None, multi)

if __name__ == "__main__":
    run_tester()
