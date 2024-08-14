import logging
from typing import List, Dict, Any, Tuple
from database import get_all_media_items
from settings import get_all_settings

def get_wanted_from_collected() -> List[Tuple[List[Dict[str, Any]], Dict[str, bool]]]:
    content_sources = get_all_settings().get('Content Sources', {})
    collected_sources = [data for source, data in content_sources.items() if source.startswith('Collected') and data.get('enabled', False)]
    
    if not collected_sources:
        logging.warning("No enabled Collected sources found in settings.")
        return []

    all_wanted_items = []

    for source in collected_sources:
        versions = source.get('versions', {})

        wanted_items = get_all_media_items(state="Wanted", media_type="episode")
        collected_items = get_all_media_items(state="Collected", media_type="episode")
        
        all_items = wanted_items + collected_items
        consolidated_items = {}

        for item in all_items:
            imdb_id = item['imdb_id']
            if imdb_id not in consolidated_items:
                consolidated_items[imdb_id] = {
                    'imdb_id': imdb_id,
                    'media_type': 'tv'
                }

        result = list(consolidated_items.values())

        # Debug printing
        logging.info(f"Retrieved {len(result)} unique TV shows from local database")
        for item in result:
            logging.debug(f"IMDB ID: {item['imdb_id']}, Media Type: {item['media_type']}")

        all_wanted_items.append((result, versions))

    return all_wanted_items

def map_collected_media_to_wanted():
    overseerr_url = get_setting('Overseerr', 'url')
    overseerr_api_key = get_setting('Overseerr', 'api_key')
    if not overseerr_url or not overseerr_api_key:
        logging.error("Overseerr URL or API key not set. Please configure in settings.")
        return {'episodes': []}

    try:
        logging.debug("Starting map_collected_media_to_wanted function")
        cookies = get_overseerr_cookies(overseerr_url)
        wanted_episodes = []

        # Process collected and wanted episodes
        collected_episodes = get_all_media_items(state="Collected", media_type="episode")
        wanted_episodes_db = get_all_media_items(state="Wanted", media_type="episode")
        all_episodes = collected_episodes + wanted_episodes_db

        logging.info(f"Processing episodes for {len(set(episode['tmdb_id'] for episode in all_episodes))} unique shows")

        processed_tmdb_ids = set()
        for i, episode in enumerate(all_episodes, 1):
            try:
                tmdb_id = episode['tmdb_id']

                if i % 20 == 0:
                    logging.info(f"Processed {i}/{len(all_episodes)} episodes")

                if tmdb_id is None:
                    logging.warning(f"Skipping episode due to None TMDB ID")
                    continue

                if tmdb_id in processed_tmdb_ids:
                    continue

                processed_tmdb_ids.add(tmdb_id)

                show_details = get_overseerr_show_details(overseerr_url, overseerr_api_key, tmdb_id, cookies)
                if show_details:
                    known_seasons = set(ep['season_number'] for ep in all_episodes if ep['tmdb_id'] == tmdb_id and ep['season_number'] != 0)

                    for season in show_details.get('seasons', []):
                        season_number = season.get('seasonNumber')
                        if season_number == 0:
                            continue  # Skip season 0

                        season_details = get_overseerr_show_episodes(overseerr_url, overseerr_api_key, tmdb_id, season_number, cookies)
                        if season_details:
                            known_episodes_this_season = [ep for ep in all_episodes if ep['tmdb_id'] == tmdb_id and ep['season_number'] == season_number]
                            known_episode_numbers = set(ep['episode_number'] for ep in known_episodes_this_season)

                            for overseerr_episode in season_details.get('episodes', []):
                                if overseerr_episode['episodeNumber'] not in known_episode_numbers:
                                    release_date = get_release_date(overseerr_episode, 'tv')
                                    wanted_episodes.append({
                                        'imdb_id': show_details.get('externalIds', {}).get('imdbId', 'Unknown IMDb ID'),
                                        'tmdb_id': tmdb_id,
                                        'title': show_details.get('name', 'Unknown Show Title'),
                                        'episode_title': overseerr_episode.get('name', 'Unknown Episode Title'),
                                        'year': release_date[:4] if release_date != 'Unknown' else 'Unknown Year',
                                        'season_number': season_number,
                                        'episode_number': overseerr_episode['episodeNumber'],
                                        'release_date': release_date
                                    })

            except requests.exceptions.RequestException as e:
                logging.error(f"Error processing show TMDB ID: {tmdb_id}: {str(e)}")
            except Exception as e:
                logging.error(f"Unexpected error processing show TMDB ID: {tmdb_id}: {str(e)}")

        logging.info(f"Retrieved {len(wanted_episodes)} additional wanted episodes")

        # Log details of wanted episodes
        for episode in wanted_episodes:
            logging.info(f"Wanted episode: {episode['title']} S{episode['season_number']}E{episode['episode_number']} - {episode['episode_title']} - IMDB: {episode['imdb_id']}, TMDB: {episode['tmdb_id']} - Air Date: {episode['release_date']}")

        return {'episodes': wanted_episodes}
    except Exception as e:
        logging.error(f"Unexpected error while mapping collected media to wanted: {str(e)}")
        logging.exception("Traceback:")
        return {'episodes': []}
