from plexapi.server import PlexServer
import sys, os
import logging
from datetime import datetime, timedelta
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from settings import get_setting

def get_guid(media, guid_type='imdb'):
    for guid in media.guids:
        logging.debug(f"Processing GUID: {guid.id}")
        if guid_type.lower() in guid.id.lower():
            try:
                return guid.id.split('://')[1]
            except IndexError:
                logging.error(f"Error parsing GUID: {guid.id}")
    return None

def process_movie(movie, collected_content, missing_guid_items):
    try:
        if not movie.title or not movie.year:
            logging.warning(f"Skipping movie without title or year: {movie}")
            movie.refresh()
            return
        
        imdb_id = get_guid(movie, 'imdb')
        tmdb_id = get_guid(movie, 'tmdb')
        if not imdb_id and not tmdb_id:
            missing_guid_items['movies'].append(movie.title)
            logging.warning(f"Skipping movie without valid IMDb or TMDb ID: {movie.title}")
            return
        
        logging.debug(f"Collected movie: {movie.title}, IMDb: {imdb_id}, TMDb: {tmdb_id}")
        collected_content['movies'].append({
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'title': movie.title,
            'year': movie.year,
            'addedAt': movie.addedAt
        })
    except Exception as e:
        logging.error(f"Error processing movie {movie.title}: {str(e)}")
        logging.debug(f"Movie not processed: {movie.title}, addedAt: {movie.addedAt}")

def process_episode(show, episode, collected_content, missing_guid_items):
    try:
        if not show.title or not show.year:
            logging.warning(f"Skipping episode without title or year: {episode} from {show}")
            episode.refresh()
            return

        show_imdb_id = get_guid(show, 'imdb')
        show_tmdb_id = get_guid(show, 'tmdb')
        if not show_imdb_id and not show_tmdb_id:
            missing_guid_items['episodes'].append(f"{episode.title} from {show.title}")
            logging.warning(f"Skipping episode without valid show IMDb or TMDb ID: {episode.title} from show {show.title}")
            return

        episode_info = {
            'imdb_id': show_imdb_id,
            'tmdb_id': show_tmdb_id,
            'title': show.title,
            'episode_title': episode.title,
            'year': show.year,
            'season_number': episode.seasonNumber,
            'episode_number': episode.index,
            'addedAt': episode.addedAt
        }

        collected_content['episodes'].append(episode_info)
        logging.debug(f"Collected episode: {episode.title} from show: {show.title}, IMDb: {show_imdb_id}, TMDb: {show_tmdb_id}")
    except Exception as e:
        logging.error(f"Error processing episode {episode.title} of {show.title}: {str(e)}")
        logging.debug(f"Episode not processed: {episode.title} of {show.title}, addedAt: {episode.addedAt}")

def get_collected_from_plex(request='all'):
    try:
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
        logging.debug(f"Plex URL: {plex_url}")
        logging.debug(f"Plex token: {plex_token}")
        plex = PlexServer(plex_url, plex_token, timeout=60)  # Increased timeout
        collected_content = {'movies': [], 'episodes': []}
        missing_guid_items = {'movies': [], 'episodes': []}
        current_time = datetime.now()
        time_limit = current_time - timedelta(minutes=60)
        logging.debug(f"Current time: {current_time}")
        logging.debug(f"Time limit: {time_limit}")
        logging.debug(f"Time window: {current_time - time_limit}")

        def log_progress(current, total, item_type):
            progress = (current / total) * 100
            if progress % 10 < (current - 1) / total * 100 % 10:
                logging.info(f"{item_type} progress: {current}/{total} ({progress:.1f}%)")

        if request == 'recent':
            # Fetch recently added movies
            movies_section = plex.library.section('Films')
            recent_movies = movies_section.recentlyAdded()
            logging.info(f"Number of recent movies found: {len(recent_movies)}")
            for i, movie in enumerate(recent_movies, start=1):
                if movie.addedAt >= time_limit:
                    process_movie(movie, collected_content, missing_guid_items)
                    log_progress(i, len(recent_movies), "Recent movies")
                else:
                    logging.debug(f"Skipping movie {movie.title} due to time limit")

            # Fetch recently added TV shows
            shows_section = plex.library.section('TV Shows')
            recent_shows = shows_section.recentlyAdded()
            logging.info(f"Number of recent shows found: {len(recent_shows)}")
            for i, show in enumerate(recent_shows, start=1):
                recent_episodes = [ep for ep in show.episodes() if ep.addedAt >= time_limit]
                logging.debug(f"Processing show: {show.title}, Recent episodes: {len(recent_episodes)}")
                for episode in recent_episodes:
                    process_episode(show, episode, collected_content, missing_guid_items)
                log_progress(i, len(recent_shows), "Recent shows")

        else:
            # Fetch all movies
            movies_section = plex.library.section('Films')
            movies = movies_section.all()
            for i, movie in enumerate(movies, start=1):
                process_movie(movie, collected_content, missing_guid_items)
                log_progress(i, len(movies), "Movies")

            # Fetch all TV shows
            shows_section = plex.library.section('TV Shows')
            shows = shows_section.all()
            for i, show in enumerate(shows, start=1):
                all_episodes = show.episodes()
                for episode in all_episodes:
                    process_episode(show, episode, collected_content, missing_guid_items)
                log_progress(i, len(shows), "Shows")

        logging.info(f"Collection complete: {len(collected_content['movies'])} movies and {len(collected_content['episodes'])} episodes collected.")
        logging.debug(f"Content collected: {collected_content}")

        # Log missing GUID items
        if missing_guid_items['movies']:
            logging.warning(f"Movies without valid IMDb or TMDb IDs: {missing_guid_items['movies']}")
        if missing_guid_items['episodes']:
            logging.warning(f"Episodes without valid show IMDb or TMDb IDs: {missing_guid_items['episodes']}")

        return collected_content
    except Exception as e:
        logging.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        return None
