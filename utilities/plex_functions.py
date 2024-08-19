from plexapi.server import PlexServer
import sys, os
import logging
from datetime import datetime, timedelta
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from settings import get_setting

def get_guid(media, guid_type='imdb'):
    for guid in media.guids:
        #logging.debug(f"Processing GUID: {guid.id}")
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
        
        # Create a base movie entry
        base_movie_entry = {
            'imdb_id': imdb_id,
            'tmdb_id': tmdb_id,
            'title': movie.title,
            'year': movie.year,
            'addedAt': movie.addedAt,
        }
        
        # Add an entry for each location
        for location in movie.locations:
            movie_entry = base_movie_entry.copy()
            movie_entry['location'] = location
            collected_content['movies'].append(movie_entry)
            
        #logging.debug(f"Added {len(movie.locations)} location(s) for movie: {movie.title}")
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

        base_episode_info = {
            'imdb_id': show_imdb_id,
            'tmdb_id': show_tmdb_id,
            'title': show.title,
            'episode_title': episode.title,
            'year': show.year,
            'season_number': episode.seasonNumber,
            'episode_number': episode.index,
            'addedAt': episode.addedAt,
        }

        # Add an entry for each location
        for location in episode.locations:
            episode_info = base_episode_info.copy()
            episode_info['location'] = location
            collected_content['episodes'].append(episode_info)

        logging.debug(f"Collected episode: {episode.title} from show: {show.title}, IMDb: {show_imdb_id}, TMDb: {show_tmdb_id}")
        #logging.debug(f"Added {len(episode.locations)} location(s) for episode: {episode.title}")
    except Exception as e:
        logging.error(f"Error processing episode {episode.title} of {show.title}: {str(e)}")
        logging.debug(f"Episode not processed: {episode.title} of {show.title}, addedAt: {episode.addedAt}")

def get_collected_from_plex(request='all'):
    try:
        plex_url = get_setting('Plex', 'url')
        plex_token = get_setting('Plex', 'token')
        
        movie_libraries = [lib.strip() for lib in get_setting('Plex', 'movie_libraries', '').split(',') if lib.strip()]
        show_libraries = [lib.strip() for lib in get_setting('Plex', 'shows_libraries', '').split(',') if lib.strip()]
        
        logging.debug(f"Plex URL: {plex_url}")
        logging.debug(f"Plex token: {plex_token}")
        logging.debug(f"Movie libraries: {movie_libraries}")
        logging.debug(f"TV Show libraries: {show_libraries}")
        
        plex = PlexServer(plex_url, plex_token, timeout=60)
        collected_content = {'movies': [], 'episodes': []}
        missing_guid_items = {'movies': [], 'episodes': []}
        current_time = datetime.now()
        time_limit = current_time - timedelta(minutes=240)
        logging.debug(f"Current time: {current_time}")
        logging.debug(f"Time limit: {time_limit}")
        logging.debug(f"Time window: {current_time - time_limit}")

        def log_progress(current, total, item_type):
            progress = (current / total) * 100
            if progress % 10 < (current - 1) / total * 100 % 10:
                logging.debug(f"{item_type} progress: {current}/{total} ({progress:.1f}%)")

        def get_library_section(plex, library_identifier):
            if library_identifier.isdigit():
                section = next((section for section in plex.library.sections() if str(section.key) == library_identifier), None)
                if section:
                    return section
                else:
                    logging.error(f"No library found with section ID: {library_identifier}")
                    return None
            else:
                try:
                    return plex.library.section(library_identifier)
                except:
                    logging.error(f"No library found with name: {library_identifier}")
                    return None

        if request == 'recent':
            logging.info("Gathering recently added from Plex")

            for library_identifier in movie_libraries:
                try:
                    movies_section = get_library_section(plex, library_identifier)
                    if movies_section:
                        recent_movies = movies_section.recentlyAdded()
                        logging.debug(f"Number of recent movies found in '{movies_section.title}': {len(recent_movies)}")
                        for i, movie in enumerate(recent_movies, start=1):
                            logging.debug(f"Processing movie: {movie.title}, Added at: {movie.addedAt}, Current time: {current_time}")
                            if movie.addedAt >= time_limit:
                                process_movie(movie, collected_content, missing_guid_items)
                                log_progress(i, len(recent_movies), f"Recent movies in '{movies_section.title}'")
                            else:
                                logging.debug(f"Skipping movie {movie.title} due to time limit. Added at: {movie.addedAt}, Time limit: {time_limit}")
                except Exception as e:
                    logging.error(f"Error processing movie library '{library_identifier}': {str(e)}")

            for library_identifier in show_libraries:
                try:
                    shows_section = get_library_section(plex, library_identifier)
                    if shows_section:
                        recent_shows = shows_section.recentlyAdded()
                        logging.debug(f"Number of recent shows found in '{shows_section.title}': {len(recent_shows)}")
                        for i, show in enumerate(recent_shows, start=1):
                            recent_episodes = [ep for ep in show.episodes() if ep.addedAt >= time_limit]
                            logging.debug(f"Processing show: {show.title}, Recent episodes: {len(recent_episodes)}")
                            for episode in recent_episodes:
                                logging.debug(f"Processing episode: {episode.title}, Added at: {episode.addedAt}, Current time: {current_time}")
                                process_episode(show, episode, collected_content, missing_guid_items)
                            log_progress(i, len(recent_shows), f"Recent shows in '{shows_section.title}'")
                except Exception as e:
                    logging.error(f"Error processing TV show library '{library_identifier}': {str(e)}")

        else:
            logging.info("Gathering all collected from Plex")
            for library_identifier in movie_libraries:
                try:
                    movies_section = get_library_section(plex, library_identifier)
                    if movies_section:
                        movies = movies_section.all()
                        logging.debug(f"Number of movies found in '{movies_section.title}': {len(movies)}")
                        for i, movie in enumerate(movies, start=1):
                            logging.debug(f"Processing movie: {movie.title}, Added at: {movie.addedAt}, Current time: {current_time}")
                            process_movie(movie, collected_content, missing_guid_items)
                            log_progress(i, len(movies), f"Movies in '{movies_section.title}'")
                except Exception as e:
                    logging.error(f"Error processing movie library '{library_identifier}': {str(e)}")

            for library_identifier in show_libraries:
                try:
                    shows_section = get_library_section(plex, library_identifier)
                    if shows_section:
                        shows = shows_section.all()
                        logging.debug(f"Number of shows found in '{shows_section.title}': {len(shows)}")
                        for i, show in enumerate(shows, start=1):
                            all_episodes = show.episodes()
                            for episode in all_episodes:
                                logging.debug(f"Processing episode: {episode.title}, Added at: {episode.addedAt}, Current time: {current_time}")
                                process_episode(show, episode, collected_content, missing_guid_items)
                            log_progress(i, len(shows), f"Shows in '{shows_section.title}'")
                except Exception as e:
                    logging.error(f"Error processing TV show library '{library_identifier}': {str(e)}")

        logging.debug(f"Collection complete: {len(collected_content['movies'])} movies and {len(collected_content['episodes'])} episodes collected.")
        logging.debug(f"Content collected: {collected_content}")

        if missing_guid_items['movies']:
            logging.debug(f"Movies without valid IMDb or TMDb IDs: {missing_guid_items['movies']}")
        if missing_guid_items['episodes']:
            logging.debug(f"Episodes without valid show IMDb or TMDb IDs: {missing_guid_items['episodes']}")

        return collected_content
    except Exception as e:
        logging.error(f"Error collecting content from Plex: {str(e)}", exc_info=True)
        return None
