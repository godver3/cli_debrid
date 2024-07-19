from plexapi.server import PlexServer
import logging
from datetime import datetime
from logging_config import get_logger, get_log_messages

logger = get_logger()

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def get_guid(media, guid_type='imdb'):
    for guid in media.guids:
        logger.debug(f"Processing GUID: {guid.id}")
        if guid_type.lower() in guid.id.lower():
            try:
                return guid.id.split('://')[1]
            except IndexError:
                logger.error(f"Error parsing GUID: {guid.id}")
    return None

def collect_content_from_plex(plex_url, plex_token):
    try:
        plex = PlexServer(plex_url, plex_token, timeout=60)  # Increased timeout
        collected_content = {'movies': [], 'shows': []}

        # Process Movies
        movies_section = plex.library.section('Films')
        movies = movies_section.all()
        for i, movie in enumerate(movies, 1):
            try:
                imdb_id = get_guid(movie, 'imdb')
                tmdb_id = get_guid(movie, 'tmdb')
                logger.debug(f"Collected movie: {movie.title}, IMDb: {imdb_id}, TMDb: {tmdb_id}")
                collected_content['movies'].append({
                    'imdb_id': imdb_id,
                    'tmdb_id': tmdb_id,
                    'title': movie.title,
                    'year': movie.year
                })
            except Exception as e:
                logger.error(f"Error processing movie {movie.title}: {str(e)}")

            if i % 10 == 0:
                logger.info(f"Processed {i} movies")

        # Process TV Shows
        shows_section = plex.library.section('TV Shows')
        shows = shows_section.all()
        for i, show in enumerate(shows, 1):
            try:
                show_imdb_id = get_guid(show, 'imdb')
                show_tmdb_id = get_guid(show, 'tmdb')

                # Process episodes in batches
                all_episodes = show.episodes()
                for j, episode in enumerate(all_episodes, 1):
                    try:
                        collected_content['shows'].append({
                            'show_imdb_id': show_imdb_id,
                            'show_tmdb_id': show_tmdb_id,
                            'show_title': show.title,
                            'episode_title': episode.title,
                            'year': episode.year,
                            'season_number': episode.seasonNumber,
                            'episode_number': episode.index
                        })
                    except Exception as e:
                        logger.error(f"Error processing episode {episode.title} of {show.title}: {str(e)}")

                    if j % 10 == 0:
                        logger.info(f"Processed {j} episodes of show {show.title}")

                logger.debug(f"Collected show: {show.title}, IMDb: {show_imdb_id}, TMDb: {show_tmdb_id}")
            except Exception as e:
                logger.error(f"Error processing show {show.title}: {str(e)}")

            if i % 10 == 0:
                logger.info(f"Processed {i} shows")

        logger.debug(f"Content collected: {len(collected_content['movies'])} movies and {len(collected_content['shows'])} TV episodes")
        return collected_content
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}")
        return None

def populate_db_from_plex(plex_url, plex_token):
    collected_content = collect_content_from_plex(plex_url, plex_token)
    if collected_content:
        from database import add_or_update_collected_movies_batch, add_or_update_collected_episodes_batch, verify_database
        verify_database()  # Ensure database and tables exist
        add_or_update_collected_movies_batch(collected_content['movies'])
        add_or_update_collected_episodes_batch(collected_content['shows'])
        logger.debug("Database updated with Plex content successfully.")
    else:
        logger.error("Failed to update database from Plex.")
