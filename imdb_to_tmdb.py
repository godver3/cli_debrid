import argparse
from content_checkers.overseerr import imdb_to_tmdb
from settings import get_setting

def main():
    parser = argparse.ArgumentParser(description="Convert IMDB ID to TMDB ID using Overseerr API")
    parser.add_argument("imdb_id", help="IMDB ID to convert (with or without 'imdb:' prefix)")
    
    args = parser.parse_args()
    
    # Ensure IMDB ID has the 'imdb:' prefix
    imdb_id = args.imdb_id if args.imdb_id.startswith('imdb:') else f"{args.imdb_id}"
    
    # Get Overseerr URL and API key from settings
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    
    if not overseerr_url or not overseerr_api_key:
        print("Error: Overseerr URL or API key not set in settings.")
        return
    
    tmdb_id = imdb_to_tmdb(overseerr_url, overseerr_api_key, imdb_id)
    
    if tmdb_id:
        print(f"The TMDB ID for IMDB ID {imdb_id} is: {tmdb_id}")
    else:
        print(f"Could not find a TMDB ID for IMDB ID {imdb_id}")

if __name__ == "__main__":
    main()
