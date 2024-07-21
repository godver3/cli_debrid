from plexapi.server import PlexServer
import sys, os
import logging
from datetime import datetime, timedelta
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from logging_config import get_logger, get_log_messages
from settings import get_setting

logger = get_logger()

def get_guid(media, guid_type='imdb'):
    for guid in media.guids:
        logger.debug(f"Processing GUID: {guid.id}")
        if guid_type.lower() in guid.id.lower():
            try:
                return guid.id.split('://')[1]
            except IndexError:
                logger.error(f"Error parsing GUID: {guid.id}")
    return None

def process_movie(movie, collected_content):
    try:
        imdb_id = get_guid(movie, 'imdb')
        tmdb_id = get_guid(movie, 'tmdb')
        logger.debug(f"Collected movie: {movie.title}, IMDb: {imdb_id}, TMDb: {tmdb_id}")
        collected_content['movies'].append({
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'title': movie.title,
            'year': movie.year,
            'addedAt': movie.addedAt
        })
    except Exception as e:
        logger.error(f"Error processing movie {movie.title}: {str(e)}")
        logger.debug(f"Movie not processed: {movie.title}, addedAt: {movie.addedAt}")

def process_episode(show, episode, collected_content):
    try:
        show_imdb_id = get_guid(show, 'imdb')
        show_tmdb_id = get_guid(show, 'tmdb')
        collected_content['episodes'].append({
            'imdb_id': show_imdb_id,
            'tmdb_id': show_tmdb_id,
            'title': show.title,
            'episode_title': episode.title,
            'year': episode.year,
            'season_number': episode.seasonNumber,
            'episode_number': episode.index,
            'addedAt': episode.addedAt
        })
        logger.debug(f"Collected episode: {episode.title} from show: {show.title}, IMDb: {show_imdb_id}, TMDb: {show_tmdb_id}")
    except Exception as e:
        logger.error(f"Error processing episode {episode.title} of {show.title}: {str(e)}")
        logger.debug(f"Episode not processed: {episode.title} of {show.title}, addedAt: {episode.addedAt}")

def get_collected_from_plex(request='all'):
    try:
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')

        logger.debug(f"Plex URL: {plex_url}")
        logger.debug(f"Plex token: {plex_token}")

        plex = PlexServer(plex_url, plex_token, timeout=60)  # Increased timeout
        collected_content = {'movies': [], 'episodes': []}

        time_limit = datetime.now() - timedelta(minutes=70)
        item_count = 0

        if request == 'recent':
            # Fetch recently added movies and shows
            movies_section = plex.library.section('Films')
            recent_movies = movies_section.recentlyAddedMovies(maxresults=50)
            for movie in recent_movies:
                if movie.addedAt >= time_limit:
                    process_movie(movie, collected_content)
                    item_count += 1
                    if item_count % 50 == 0:
                        logger.info(f"Progress: {item_count} items processed.")

            shows_section = plex.library.section('TV Shows')
            recent_episodes = shows_section.recentlyAdded(maxresults=50)
            for episode in recent_episodes:
                if episode.addedAt >= time_limit:
                    process_episode(episode.show(), episode, collected_content)
                    item_count += 1
                    if item_count % 50 == 0:
                        logger.info(f"Progress: {item_count} items processed.")
        else:
            # Fetch all movies and shows
            movies_section = plex.library.section('Films')
            movies = movies_section.all()
            for i, movie in enumerate(movies, start=1):
                process_movie(movie, collected_content)
                if i % 50 == 0:
                    logger.info(f"Movies progress: {i}/{len(movies)}")

            shows_section = plex.library.section('TV Shows')
            shows = shows_section.all()
            for show in shows:
                all_episodes = show.episodes()
                for i, episode in enumerate(all_episodes, start=1):
                    process_episode(show, episode, collected_content)
                    if i % 50 == 0:
                        logger.info(f"Episodes progress: {i}/{len(all_episodes)}")

        logger.info(f"Collection complete: {len(collected_content['movies'])} movies and {len(collected_content['episodes'])} episodes collected.")
        logger.debug(f"Content collected: {collected_content}")
        return collected_content
    except Exception as e:
        logger.error(f"Error collecting content from Plex: {str(e)}")
        return None
