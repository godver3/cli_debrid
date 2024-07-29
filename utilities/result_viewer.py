import curses
from typing import List, Dict

def truncate_string(string, length):
    return string[:length - 3] + '...' if len(string) > length else string.ljust(length)

def display_results(results: List[Dict]):
    def main(stdscr):
        curses.curs_set(0)  # Hide the cursor
        stdscr.clear()

        height, width = stdscr.getmaxyx()
        table_height = height - 4  # Leave room for header and footer
        current_pos = 0
        start_pos = 0

        while True:
            stdscr.clear()

            # Calculate column widths
            name_width = width - 85  # Adjust this value to allocate space for other columns

            # Display header
            stdscr.addstr(0, 0, "Name".ljust(name_width) + "Size/File".ljust(15) + "Source".ljust(15) + "Est. Bitrate".ljust(15))
            stdscr.addstr(1, 0, "-" * (width - 1))

            # Display results
            for i in range(start_pos, min(start_pos + table_height, len(results))):
                result = results[i]
                if i == current_pos:
                    stdscr.attron(curses.A_REVERSE)
                stdscr.addstr(i - start_pos + 2, 0,
                              truncate_string(result.get('title', 'N/A'), name_width) +
                              f"{result.get('size', 0):.2f} GB".ljust(15) +
                              truncate_string(result.get('source', 'N/A'), 15) +
                              f"{result.get('bitrate', 0):.2f} mbps".ljust(15))
                if i == current_pos:
                    stdscr.attroff(curses.A_REVERSE)

            # Display footer
            stdscr.addstr(height - 1, 0, "Use arrow keys to navigate, Enter to select, q to quit")

            # Handle key presses
            key = stdscr.getch()
            if key == ord('q'):
                return None
            elif key == curses.KEY_UP and current_pos > 0:
                current_pos -= 1
                if current_pos < start_pos:
                    start_pos = current_pos
            elif key == curses.KEY_DOWN and current_pos < len(results) - 1:
                current_pos += 1
                if current_pos >= start_pos + table_height:
                    start_pos = current_pos - table_height + 1
            elif key == curses.KEY_PPAGE:  # Page Up
                current_pos = max(0, current_pos - table_height)
                start_pos = max(0, start_pos - table_height)
            elif key == curses.KEY_NPAGE:  # Page Down
                current_pos = min(len(results) - 1, current_pos + table_height)
                start_pos = min(len(results) - table_height, start_pos + table_height)
            elif key == 10:  # Enter key
                return results[current_pos]

    return curses.wrapper(main)
