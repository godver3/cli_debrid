import curses
from typing import List, Dict

def truncate_string(string, length):
    return string[:length - 3] + '...' if len(string) > length else string.ljust(length)

def display_results(results: List[Dict], filtered_out_results: List[Dict]):
    def main(stdscr):
        curses.curs_set(0)  # Hide the cursor
        curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)  # Initialize color pair for red text
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        table_height = height - 6  # Leave room for header, footer, and filtered out results header
        current_pos = 0
        start_pos = 0
        show_filtered_out = False  # Toggle for showing filtered out results

        while True:
            stdscr.clear()
            # Calculate column widths
            name_width = width - 95  # Adjust this value to allocate space for other columns

            # Display header
            stdscr.addstr(0, 0, "Name".ljust(name_width) + "Size/File".ljust(15) + "Source".ljust(25) + "Est. Bitrate".ljust(15))
            stdscr.addstr(1, 0, "-" * (width - 1))

            # Display results
            display_results = results if not show_filtered_out else filtered_out_results
            for i in range(start_pos, min(start_pos + table_height, len(display_results))):
                result = display_results[i]
                if i == current_pos:
                    stdscr.attron(curses.A_REVERSE)
                if show_filtered_out:
                    stdscr.attron(curses.color_pair(1))
                stdscr.addstr(i - start_pos + 2, 0,
                              truncate_string(result.get('title', 'N/A'), name_width) +
                              f"{result.get('size', 0):.2f} GB".ljust(15) +
                              truncate_string(result.get('source', 'N/A'), 25) +
                              f"{result.get('bitrate', 0):.2f} mbps".ljust(15))
                if show_filtered_out:
                    stdscr.attroff(curses.color_pair(1))
                if i == current_pos:
                    stdscr.attroff(curses.A_REVERSE)

            # Display footer
            footer = "Use arrow keys to navigate, Enter to select, q to quit, f to toggle filtered results"
            stdscr.addstr(height - 1, 0, footer)

            # Handle key presses
            key = stdscr.getch()
            if key == ord('q'):
                return None
            elif key == ord('f'):
                show_filtered_out = not show_filtered_out
                current_pos = 0
                start_pos = 0
            elif key == curses.KEY_UP and current_pos > 0:
                current_pos -= 1
                if current_pos < start_pos:
                    start_pos = current_pos
            elif key == curses.KEY_DOWN and current_pos < len(display_results) - 1:
                current_pos += 1
                if current_pos >= start_pos + table_height:
                    start_pos = current_pos - table_height + 1
            elif key == curses.KEY_PPAGE:  # Page Up
                current_pos = max(0, current_pos - table_height)
                start_pos = max(0, start_pos - table_height)
            elif key == curses.KEY_NPAGE:  # Page Down
                current_pos = min(len(display_results) - 1, current_pos + table_height)
                start_pos = min(len(display_results) - table_height, start_pos + table_height)
            elif key == 10:  # Enter key
                return display_results[current_pos] if not show_filtered_out else None

    return curses.wrapper(main)