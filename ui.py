import urwid
from logging_config import get_logger
import logging

logger = get_logger()

class QueueColumn(urwid.ListBox):
    def __init__(self, title):
        self.title = title
        self.content = urwid.SimpleFocusListWalker([])
        super().__init__(self.content)

    def update(self, items):
        self.content.clear()
        for item in items:
            self.content.append(self.format_item(item))

    def format_item(self, item):
        if item['type'] == 'movie':
            return urwid.Text(f"{item['title']} ({item['year']})")
        elif item['type'] == 'episode':
            return urwid.Text(f"{item['title']} S{item['season_number']:02d}E{item['episode_number']:02d}")
        else:
            return urwid.Text(str(item))

class LogBox(urwid.ListBox):
    def __init__(self):
        self.content = urwid.SimpleFocusListWalker([])
        super().__init__(self.content)

    def add_log(self, message):
        self.content.append(urwid.Text(message))
        self.set_focus(len(self.content) - 1)

class UI:
    def __init__(self, queue_manager):
        self.queue_manager = queue_manager
        self.columns = {
            "Wanted": QueueColumn("Wanted"),
            "Scraping": QueueColumn("Scraping"),
            "Adding": QueueColumn("Adding"),
            "Checking": QueueColumn("Checking"),
            "Sleeping": QueueColumn("Sleeping")
        }
        self.log_box = LogBox()

        column_widgets = [
            urwid.LineBox(self.columns[key], title=f"{key} (0)")
            for key in ["Wanted", "Scraping", "Adding", "Checking", "Sleeping"]
        ]
        self.columns_widget = urwid.Columns(column_widgets)

        self.layout = urwid.Frame(
            urwid.Pile([
                ('weight', 70, urwid.LineBox(self.columns_widget, title="Queues")),
                ('weight', 30, urwid.LineBox(self.log_box, title="Logs"))
            ]),
            header=urwid.AttrMap(urwid.Text("CLI Debrid", align='center'), 'header'),
            footer=urwid.AttrMap(urwid.Text("Press Q to quit", align='center'), 'footer')
        )

        self.palette = [
            ('header', 'white', 'dark blue'),
            ('footer', 'white', 'dark red'),
            ('log', 'light gray', 'dark gray'),
            ('queue_title', 'black', 'light gray'),
            ('default', 'light gray', 'black'),
        ]

        self.loop = None

    @property
    def main_widget(self):
        return urwid.AttrMap(self.layout, 'default')

    def set_loop(self, loop):
        self.loop = loop

    def stop(self):
        raise urwid.ExitMainLoop()

    def update(self, queue_contents):
        try:
            for queue_name, items in queue_contents.items():
                self.columns[queue_name].update(items)
                # Update the queue title with the item count
                self.columns_widget.contents[list(self.columns.keys()).index(queue_name)][0].set_title(f"{queue_name} ({len(items)})")
        except Exception as e:
            logger.error(f"Error updating UI: {str(e)}", exc_info=True)

    def handle_input(self, key):
        if key in ('q', 'Q'):
            self.stop()

    def add_log(self, message):
        self.log_box.add_log(message)

class UrwidHandler(logging.Handler):
    def __init__(self, ui):
        super().__init__()
        self.ui = ui

    def emit(self, record):
        log_entry = self.format(record)
        def safe_emit():
            self.ui.add_log(log_entry)
            if self.ui.loop:
                self.ui.loop.draw_screen()
        if self.ui.loop:
            self.ui.loop.set_alarm_in(0, lambda loop, data: safe_emit())
