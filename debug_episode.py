from cli_battery.app.direct_api import DirectAPI
import logging

logging.basicConfig(level=logging.DEBUG)

show_imdb = "tt5594440"  # People Magazine Investigates
season = 8
episode = 10

# Try to get episode metadata directly
print("\nTrying to get show metadata to find episode IMDb ID:")
show_metadata, source = DirectAPI.get_show_metadata(show_imdb)
if show_metadata:
    print(f"Source: {source}")
    print(f"Show metadata: {show_metadata}")
    
    # Look for season 8 episode 10
    seasons = show_metadata.get('seasons', {})
    if str(season) in seasons:
        season_data = seasons[str(season)]
        print(f"\nSeason {season} data:")
        for ep in season_data.get('episodes', {}).values():
            if ep.get('episode_number') == episode:
                print(f"Found episode {episode}:")
                print(ep)
                if 'imdb_id' in ep:
                    print(f"\nTrying to get episode metadata for {ep['imdb_id']}:")
                    episode_metadata, source = DirectAPI.get_episode_metadata(ep['imdb_id'])
                    print(f"Source: {source}")
                    print(f"Episode metadata: {episode_metadata}")
                break
