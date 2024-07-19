import asyncio
import json
import curses
from scraper.zilean import scrape_zilean
from scraper.torrentio import scrape_torrentio
from scraper.knightcrawler import scrape_knightcrawler
from typing import Tuple

async def debug_scraper(scraper_func, query):
    print(f"Debugging {scraper_func.__name__}")
    print("-" * 50)
    try:
        if scraper_func.__name__ == 'scrape_torrentio' or scraper_func.__name__ == 'scrape_knightcrawler':
            content_type, imdb_id, season, episode = parse_query(query)
            print("Parsed Query:")
            print(f"  IMDb ID: {imdb_id}")
            print(f"  Content Type: {content_type}")
            print(f"  Season: {season}")
            print(f"  Episode: {episode}")
            url, results = await scraper_func(imdb_id, content_type, season, episode)
            print(f"Request URL: {url}")
        else:
            results = await scraper_func(query)

        if isinstance(results, list):
            print(f"Number of results: {len(results)}")
        else:
            print("Results are not in the expected list format.")
        return results
    except Exception as e:
        print(f"Error occurred: {str(e)}")
        return []

def parse_query(query: str) -> Tuple[str, str, int, int]:
    # Implement your query parsing logic here
    content_type = "movie"  # or "episode"
    imdb_id = query  # Example: "tt1234567"
    season = 1
    episode = 1
    return content_type, imdb_id, season, episode

def display_results(stdscr, results):
    curses.curs_set(0)  # Hide the cursor
    current_pos = 0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        for i, result in enumerate(results[current_pos:current_pos+height-1]):
            if i == 0:
                stdscr.attron(curses.A_REVERSE)
            result_str = json.dumps(result, ensure_ascii=False)[:width-1]
            stdscr.addstr(i, 0, result_str)
            if i == 0:
                stdscr.attroff(curses.A_REVERSE)

        stdscr.refresh()

        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == curses.KEY_UP and current_pos > 0:
            current_pos -= 1
        elif key == curses.KEY_DOWN and current_pos < len(results) - 1:
            current_pos += 1

async def main():
    query = input("Enter your search query: ")
    scrapers = [
        scrape_zilean,
        scrape_torrentio,
        scrape_knightcrawler
    ]
    for scraper in scrapers:
        results = await debug_scraper(scraper, query)
        if results:
            print("Press any key to view results...")
            input()
            curses.wrapper(display_results, results)

if __name__ == "__main__":
    asyncio.run(main())
