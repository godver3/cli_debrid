import logging
from typing import List, Dict, Any
from guessit import guessit
from scraper.scraper import scrape
from queues.adding_queue import AddingQueue
from queues.scraping_queue import ScrapingQueue
from queues.anime_matcher import AnimeMatcher
from metadata.metadata import get_overseerr_cookies, get_overseerr_show_details
from settings import get_setting
from api_tracker import api
import urllib.parse

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AnimeHelper:
    def __init__(self):
        self.adding_queue = AddingQueue()
        self.scraping_queue = ScrapingQueue()
        self.anime_matcher = AnimeMatcher(self.adding_queue.calculate_absolute_episode)

    def analyze_anime(self, imdb_id: str, title: str, year: int, season: int = None, episode: int = None):
        content_type = 'episode' if season is not None else 'movie'
        multi = season is not None and episode is None
        
        logging.info(f"Analyzing anime: {title} ({year}) - IMDB: {imdb_id}")
        logging.info(f"Content type: {content_type}, Multi: {multi}, Season: {season}, Episode: {episode}")

        # Try to get TMDB ID
        tmdb_id = self.get_tmdb_id(imdb_id, title, year)

        # Scrape results
        results, _ = scrape(imdb_id, tmdb_id, title, year, content_type, "default", season, episode, multi)

        if not results:
            logging.info("No results found.")
            return

        logging.info(f"Total results: {len(results)}")

        # Analyze file formats
        self.analyze_file_formats(results)

        # Match files to episodes
        if content_type == 'episode':
            self.match_files_to_episodes(results, season, episode)

    def get_tmdb_id(self, imdb_id: str, title: str, year: int) -> str:
        overseerr_url = get_setting('Overseerr', 'url')
        overseerr_api_key = get_setting('Overseerr', 'api_key')
        cookies = get_overseerr_cookies(overseerr_url)

        try:
            # Search using the title
            search_results = self.search_overseerr(overseerr_url, overseerr_api_key, title, cookies)
            
            # Find the TV result with matching IMDB ID
            tv_result = next((result for result in search_results.get('results', []) 
                              if result.get('mediaType') == 'tv' and result.get('externalIds', {}).get('imdb') == imdb_id), None)
            
            if tv_result:
                tmdb_id = str(tv_result.get('id', ''))
                # Now get the full details using the TMDB ID
                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if show_details:
                    return tmdb_id
            else:
                logging.warning(f"No TV show found for IMDB ID: {imdb_id}")
        except Exception as e:
            logging.warning(f"Failed to get TMDB ID: {str(e)}")
        
        return ""

    def search_overseerr(self, overseerr_url: str, overseerr_api_key: str, query: str, cookies: api.cookies.RequestsCookieJar) -> Dict[str, Any]:
        headers = {
            'X-Api-Key': overseerr_api_key,
            'Accept': 'application/json'
        }
        encoded_query = urllib.parse.quote(query)
        url = f"{overseerr_url}/api/v1/search?query={encoded_query}"
        
        try:
            response = api.get(url, headers=headers, cookies=cookies, timeout=10)
            response.raise_for_status()
            return response.json()
        except api.exceptions.RequestException as e:
            logging.error(f"Error searching Overseerr for query {query}: {str(e)}")
            return {}

    def analyze_file_formats(self, results: List[Dict[str, Any]]):
        logging.info("\nAnalyzing file formats:")
        for result in results:
            title = result.get('title', '')
            guess = guessit(title)
            logging.info(f"\nFile: {title}")
            logging.info(f"Guessit result: {guess}")

            # Extract relevant information
            detected_type = guess.get('type', 'Unknown')
            season = guess.get('season', 'N/A')
            episode = guess.get('episode', 'N/A')
            episode_title = guess.get('episode_title', 'N/A')
            
            if isinstance(episode, list):
                episode = f"{episode[0]}-{episode[-1]}"

            logging.info(f"Detected type: {detected_type}")
            logging.info(f"Season: {season}")
            logging.info(f"Episode: {episode}")
            logging.info(f"Episode title: {episode_title}")

    def match_files_to_episodes(self, results: List[Dict[str, Any]], season: int, episode: int):
        logging.info("\nMatching files to episodes:")
        
        # Create dummy items for matching
        items = [
            {'type': 'episode', 'season_number': season, 'episode_number': i}
            for i in range(1, 26)  # Assuming max 25 episodes per season
        ]

        files = [result.get('title', '') for result in results]
        matches = self.anime_matcher.match_anime_files(files, items)

        for file, item in matches:
            logging.info(f"\nFile: {file}")
            logging.info(f"Matched to: Season {item['season_number']}, Episode {item['episode_number']}")

def main():
    helper = AnimeHelper()

    # Example usage
    helper.analyze_anime("tt0988824", "Naruto Shippuden", 2007, season=1)
    helper.analyze_anime("tt0988824", "Naruto Shippuden", 2007, season=1, episode=1)

if __name__ == "__main__":
    main()