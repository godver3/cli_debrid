import sys
sys.path.append('/path/to/your/project')  # Adjust this path as needed

from content_checkers.overseerr import (
    get_setting,
    get_overseerr_cookies,
    get_overseerr_movie_details,
    get_overseerr_headers,
    get_release_date,
    parse_date
)
import logging

logging.basicConfig(level=logging.DEBUG)

def check_movie_release_date(tmdb_id):
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        print("Overseerr URL or API key not set. Please configure in settings.")
        return

    try:
        cookies = get_overseerr_cookies(overseerr_url)
        movie_details = get_overseerr_movie_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
        
        if movie_details is None:
            print(f"Unable to fetch details for TMDB ID: {tmdb_id}")
            print("Selected Release Date: Unknown")
            return

        print(f"Movie: {movie_details.get('title')} (TMDB ID: {tmdb_id})")
        print(f"General Release Date: {movie_details.get('releaseDate')}")
        
        releases = movie_details.get('releases', {}).get('results', [])
        print("\nAll Release Dates:")
        for release in releases:
            country = release.get('iso_3166_1', 'Unknown Country')
            for date in release.get('release_dates', []):
                parsed_date = parse_date(date.get('release_date'))
                formatted_date = parsed_date.strftime("%Y-%m-%d") if parsed_date else "Unparseable"
                print(f"Country: {country}, Type: {date.get('type')}, Original Date: {date.get('release_date')}, Parsed Date: {formatted_date}")
        
        print("\nSelected Release Date (based on new logic):")
        release_date = get_release_date(movie_details, 'movie')
        print(f"Selected Date: {release_date}")
        
        if release_date == 'Unknown':
            print("No suitable release date found.")

    except Exception as e:
        print(f"Error checking release date for TMDB ID {tmdb_id}: {str(e)}")
        print("Selected Release Date: Unknown")

def main():
    tmdb_ids = [10193, 519182, 835113, 131556]  # Added the problematic TMDB ID
    for tmdb_id in tmdb_ids:
        check_movie_release_date(tmdb_id)
        print("\n" + "="*50 + "\n")

if __name__ == "__main__":
    main()
