import urwid, os
import configparser
from typing import List, Dict, Any, Optional
from scraper.scraper import scrape, rank_result_key, parse_size, calculate_bitrate
from settings import set_setting, get_scraping_settings, load_config, save_config
from utilities.manual_scrape import imdb_id_to_title_and_year, run_manual_scrape
import logging

CONFIG_FILE = './config.ini'

class ScraperTester:
    def __init__(self, imdb_id: str, tmdb_id: str, title: str, year: int, movie_or_episode: str, season: int = None, episode: int = None, multi: bool = False):
        self.config = load_config()
        self.settings_widgets = {}
        self.results = []
        self.imdb_id = imdb_id
        self.tmdb_id = tmdb_id
        self.title = title
        self.year = year
        self.movie_or_episode = 'movie' if movie_or_episode.lower() == 'movie' else 'episode'
        self.season = season
        self.episode = episode
        self.multi = multi
        self.versions = list(self.config.get('Scraping', {}).get('versions', {}).keys())
        self.current_version = self.versions[0] if self.versions else None
        self.palette = [
            ('reversed', 'standout', ''),
            ('header', 'white', 'dark blue'),
            ('result', 'black', 'light gray'),
            ('footer', 'white', 'dark blue'),
            ('highlight', 'black', 'light green'),
            ('score_box', 'white', 'dark blue'),
        ]
        self.main_loop = None
        self.show_filtered_out = False  # New attribute to track display mode
        self.filtered_out_results = []  # New attribute to store filtered out results

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
        return urwid.Frame(main_area)

    def settings_view(self):
        widgets = [urwid.Text(('header', "Scraping Settings")), urwid.Divider()]

        # Add version selector
        version_options = []
        for v in self.versions:
            rb = urwid.RadioButton(version_options, v, on_state_change=self.on_version_change)
            if v == self.current_version:
                rb.set_state(True, do_callback=False)
        version_selector = urwid.Pile([urwid.Text("Select Version:"), urwid.Columns(version_options)])
        widgets.append(version_selector)
        widgets.append(urwid.Divider())

        scraping_settings = get_scraping_settings()

        for key, (label, value) in scraping_settings.items():
            if key.startswith(f"{self.current_version}_"):
                if key.endswith('_enable_hdr'):
                    state = value if isinstance(value, bool) else (value.lower() in ['true', '1', 'yes', 'on'] if isinstance(value, str) else False)
                    checkbox = urwid.CheckBox(label, state=state)
                    urwid.connect_signal(checkbox, 'change', self.on_checkbox_change, user_args=['Scraping', key])
                    self.settings_widgets[('Scraping', key)] = checkbox
                    widgets.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
                elif key.endswith('_filter_in') or key.endswith('_filter_out') or key.endswith('_preferred_filter_in') or key.endswith('_preferred_filter_out'):
                    filter_text = ', '.join([f"{item[0]} ({item[1]})" if isinstance(item, (list, tuple)) else str(item) for item in value])
                    widgets.append(urwid.Text(f"{label}: {filter_text}"))
                else:
                    edit = urwid.Edit(f"{label}: ", str(value))
                    urwid.connect_signal(edit, 'change', self.on_setting_change, user_args=['Scraping', key])
                    self.settings_widgets[('Scraping', key)] = edit
                    widgets.append(urwid.AttrMap(edit, None, focus_map='reversed'))

        widgets.append(urwid.Divider())
        widgets.append(urwid.AttrMap(urwid.Button("Refresh Results", on_press=self.refresh_results), None, focus_map='reversed'))
        widgets.append(urwid.AttrMap(urwid.Button("Quit", on_press=self.quit_program), None, focus_map='reversed'))

        # Add toggle button for filtered/filtered-out results
        toggle_button = urwid.Button("Toggle Filtered/Filtered-out Results", on_press=self.toggle_results_view)
        widgets.append(urwid.AttrMap(toggle_button, None, focus_map='reversed'))

        return urwid.ListBox(urwid.SimpleFocusListWalker(widgets))

    def toggle_results_view(self, button):
        self.show_filtered_out = not self.show_filtered_out
        self.refresh_view()

    def quit_program(self, button):
        raise urwid.ExitMainLoop()

    def results_view(self):
        header = urwid.AttrMap(urwid.Columns([
            ('weight', 40, urwid.Text("Name")),
            ('weight', 15, urwid.Text("Size")),
            ('weight', 15, urwid.Text("Est. Mbps")),
            ('weight', 15, urwid.Text("Scraper")),
            ('weight', 15, urwid.Text("Score")),
        ]), 'header')

        results_to_display = self.filtered_out_results if self.show_filtered_out else self.results
        self.result_widgets = [urwid.AttrMap(SelectableColumns(self.format_result(result)), None, focus_map='highlight') for result in results_to_display]
        
        status_text = f"Showing {'Filtered-out' if self.show_filtered_out else 'Filtered'} Results"
        status_widget = urwid.Text(('header', status_text))

        listwalker = urwid.SimpleFocusListWalker([status_widget, header, urwid.Divider()] + self.result_widgets)
        return urwid.ListBox(listwalker)

    def format_result(self, result: Dict[str, Any]) -> List[urwid.Widget]:
        title = result.get('title', 'Unknown')
        size = result.get('size', 'Unknown')
        bitrate = result.get('bitrate', 'Unknown')
        scraper = result.get('scraper', 'Unknown')
        score = result.get('score_breakdown', {}).get('total_score', 'N/A')

        return [
            ('weight', 40, urwid.Text(str(title))),
            ('weight', 15, urwid.Text(f"{size:.2f}" if isinstance(size, (int, float)) else str(size))),
            ('weight', 15, urwid.Text(f"{bitrate:.0f}" if isinstance(bitrate, (int, float)) else str(bitrate))),
            ('weight', 15, urwid.Text(str(scraper))),
            ('weight', 15, urwid.Text(f"{score:.2f}" if isinstance(score, (int, float)) else str(score))),
        ]

    def on_setting_change(self, section: str, key: str, widget: urwid.Edit, new_value: str):
        version, setting = key.split('_', 1)
        if 'versions' not in self.config['Scraping']:
            self.config['Scraping']['versions'] = {}
        if version not in self.config['Scraping']['versions']:
            self.config['Scraping']['versions'][version] = {}
        self.config['Scraping']['versions'][version][setting] = new_value
        save_config(self.config)

    def on_checkbox_change(self, section: str, key: str, widget: urwid.CheckBox, new_state: bool):
        version, setting = key.split('_', 1)
        if 'versions' not in self.config['Scraping']:
            self.config['Scraping']['versions'] = {}
        if version not in self.config['Scraping']['versions']:
            self.config['Scraping']['versions'][version] = {}
        self.config['Scraping']['versions'][version][setting] = new_state
        save_config(self.config)

    def on_version_change(self, radio_button, new_state):
        if new_state:
            self.current_version = radio_button.label
            self.refresh_view()
            self.refresh_results(None)

    def refresh_view(self):
        if self.main_loop:
            self.main_loop.widget = self.main_view()

    def refresh_results(self, button):
        logging.debug(f"Refreshing results with movie_or_episode: {self.movie_or_episode}, version: {self.current_version}")
        
        scrape_results = scrape(self.imdb_id, self.tmdb_id, self.title, self.year, self.movie_or_episode, self.current_version, self.season, self.episode, self.multi)
        
        if isinstance(scrape_results, tuple) and len(scrape_results) == 2:
            self.results, self.filtered_out_results = scrape_results
        elif isinstance(scrape_results, list):
            self.results = scrape_results
            self.filtered_out_results = []
        else:
            logging.error(f"Unexpected return type from scrape: {type(scrape_results)}")
            self.results = []
            self.filtered_out_results = []
        
        logging.debug(f"Number of filtered results: {len(self.results)}")
        logging.debug(f"Number of filtered out results: {len(self.filtered_out_results)}")

        # Calculate scores and ensure bitrate for each result
        for result_list in [self.results, self.filtered_out_results]:
            for result in result_list:
                # Ensure bitrate is calculated and stored in the result
                if 'bitrate' not in result:
                    size_gb = parse_size(result.get('size', 0))
                    runtime = result.get('runtime', 0)
                    if runtime > 0:
                        result['bitrate'] = calculate_bitrate(size_gb, runtime)
                    else:
                        result['bitrate'] = 0  # Set a default value if we can't calculate
                        logging.warning(f"Unable to calculate bitrate for result: {result.get('title', 'Unknown')}. Missing runtime.")

                # Now that we ensure 'bitrate' exists, we can safely call rank_result_key
                rank_result_key(result, self.results, self.title, self.year, self.season, self.episode, self.multi, self.movie_or_episode, self.config['Scraping']['versions'][self.current_version])

        # Sort both result lists based on the total score
        self.results.sort(key=lambda x: x.get('score_breakdown', {}).get('total_score', 0), reverse=True)
        self.filtered_out_results.sort(key=lambda x: x.get('score_breakdown', {}).get('total_score', 0), reverse=True)

        self.refresh_view()

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()

        if key in ('up', 'down', 'enter'):
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
                results_to_use = self.filtered_out_results if self.show_filtered_out else self.results
                result = results_to_use[focus_pos - 3]  # Adjust for status, header and divider
                score_breakdown = result.get('score_breakdown', {})
                
                def format_value(v):
                    if isinstance(v, (int, float)):
                        return f"{v:.2f}"
                    elif isinstance(v, dict):
                        return "" + "".join(f"{sub_k}: {format_value(sub_v)}" for sub_k, sub_v in v.items())
                    elif isinstance(v, list):
                        return "" + "".join(str(item) for item in v)
                    else:
                        return str(v)

                # Create two columns for the score breakdown
                left_column = []
                right_column = []
                for i, (k, v) in enumerate(score_breakdown.items()):
                    if k == 'version':  # Handle version information separately
                        continue
                    text = f"{k}:\n{format_value(v)}"
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

                logging.debug(f"Updated score breakdown: {score_breakdown}")
            except Exception as e:
                error_message = f"Error updating score breakdown: {str(e)}"
                self.score_breakdown_text.set_text(error_message)
                self.score_columns.contents = []
                logging.error(error_message)
        else:
            self.score_columns.contents = []
        
        # Force a redraw of the main loop
        if self.main_loop:
            self.main_loop.draw_screen()

    def run(self):
        self.main_loop = urwid.MainLoop(self.main_view(), self.palette, unhandled_input=self.handle_input)
        self.refresh_results(None)  # Initial load of results
        self.main_loop.run()

class SelectableColumns(urwid.Columns):
    def selectable(self):
        return True

    def keypress(self, size, key):
        return key

def scraper_tester(imdb_id: str, tmdb_id: str, title: str, year: int, movie_or_episode: str, season: int = None, episode: int = None, multi: bool = False):
    ScraperTester(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi).run()

def run_tester():
    search_term = input("Enter search term (you can include year, season, and/or episode): ")

    # Use the run_manual_scrape function to get the details
    details = run_manual_scrape(search_term, return_details=True)

    if not details:
        print("Search cancelled or no results found.")
        return

    imdb_id = details['imdb_id']
    tmdb_id = details['tmdb_id']
    title = details['title']
    year = int(details['year'])
    movie_or_episode = details['movie_or_episode']
    season = int(details['season']) if details['season'] else None
    episode = int(details['episode']) if details['episode'] else None
    multi = details['multi']  # Assuming 'multi' is already a boolean in the details dictionary

    logging.debug(f"movie_or_episode set to: {movie_or_episode}")
    logging.debug(f"multi set to: {multi}")

    os.system('clear')
    scraper_tester(imdb_id, tmdb_id, title, year, movie_or_episode, season, episode, multi)

if __name__ == "__main__":
    run_tester()
