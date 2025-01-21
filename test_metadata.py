from cli_battery.app.trakt_metadata import TraktMetadata
import logging
import json

logging.basicConfig(level=logging.INFO)

def test_show_status():
    # Test with a few different shows - one completed and one ongoing
    test_shows = [
        "tt0903747",  # Breaking Bad (completed)
        "tt1190634",  # The Boys (ongoing)
        "tt0944947",  # Game of Thrones (completed)
    ]
    
    trakt = TraktMetadata()
    
    for imdb_id in test_shows:
        print(f"\nTesting show with IMDb ID: {imdb_id}")
        try:
            # First get the show's Trakt slug
            search_result = trakt._search_by_imdb(imdb_id)
            if search_result and search_result['type'] == 'show':
                show = search_result['show']
                slug = show['ids']['slug']
                
                # Now get the full show data using the slug
                url = f"{trakt.base_url}/shows/{slug}?extended=full"
                response = trakt._make_request(url)
                if response and response.status_code == 200:
                    show_data = response.json()
                    print(f"Title: {show_data.get('title')}")
                    print(f"Status: {show_data.get('status')}")
                    print(f"Network: {show_data.get('network')}")
                    print(f"First Aired: {show_data.get('first_aired')}")
                    print(f"Last Aired: {show_data.get('last_aired')}")
                    print(f"Runtime: {show_data.get('runtime')} minutes")
                    print(f"Number of Aired Episodes: {show_data.get('aired_episodes')}")
                    print("\nFull show data:")
                    print(json.dumps(show_data, indent=2))
            else:
                print("No show data found")
        except Exception as e:
            print(f"Error processing show {imdb_id}: {str(e)}")

if __name__ == "__main__":
    test_show_status() 