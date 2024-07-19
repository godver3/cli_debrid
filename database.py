import sqlite3
import logging
from tqdm import tqdm
from datetime import datetime, date
import unicodedata
from trakt_config import get_trakt_movie_release_date, get_trakt_episode_release_date
from cache import create_cache_table
import pickle
import os
from logging_config import get_logger

logger = get_logger()

COLLECTED_CACHE_FILE = 'collected_content_cache.pkl'

def get_db_connection():
    conn = sqlite3.connect('content_verification.db')
    conn.row_factory = sqlite3.Row
    return conn

def normalize_string(input_str):
    return ''.join(
        c for c in unicodedata.normalize('NFKD', input_str)
        if unicodedata.category(c) != 'Mn'
    )

def create_tables():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS collected_movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT,
            tmdb_id TEXT,
            title TEXT,
            year INTEGER,
            last_updated TIMESTAMP,
            UNIQUE(title, year)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS collected_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_imdb_id TEXT,
            show_tmdb_id TEXT,
            show_title TEXT,
            episode_title TEXT,
            year INTEGER,
            season_number INTEGER,
            episode_number INTEGER,
            last_updated TIMESTAMP,
            UNIQUE(show_title, season_number, episode_number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS wanted_movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT,
            tmdb_id TEXT,
            title TEXT,
            year INTEGER,
            release_date DATE,
            last_updated TIMESTAMP,
            UNIQUE(title, year)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS wanted_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_imdb_id TEXT,
            show_tmdb_id TEXT,
            show_title TEXT,
            episode_title TEXT,
            year INTEGER,
            season_number INTEGER,
            episode_number INTEGER,
            release_date DATE,
            last_updated TIMESTAMP,
            UNIQUE(show_title, season_number, episode_number)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS working_movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imdb_id TEXT,
            tmdb_id TEXT,
            title TEXT,
            year INTEGER,
            release_date DATE,
            state TEXT,
            filled_by_title TEXT,
            filled_by_magnet TEXT,
            last_updated TIMESTAMP,
            UNIQUE(title, year)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS working_episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            show_imdb_id TEXT,
            show_tmdb_id TEXT,
            show_title TEXT,
            episode_title TEXT,
            year INTEGER,
            season_number INTEGER,
            episode_number INTEGER,
            release_date DATE,
            state TEXT,
            filled_by_title TEXT,
            filled_by_magnet TEXT,
            last_updated TIMESTAMP,
            UNIQUE(show_title, season_number, episode_number)
        )
    ''')
    conn.commit()
    conn.close()

def create_database():
    create_tables()
    logger.info("Database created and tables initialized.")

def get_cached_release_date(imdb_id, media_type, season=None, episode=None):
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT release_date, last_checked FROM cache_release_dates
        WHERE imdb_id = ? AND media_type = ? AND (season IS NULL OR season = ?) AND (episode IS NULL OR episode = ?)
    ''', (imdb_id, media_type, season, episode))
    result = cursor.fetchone()
    conn.close()
    return result

def update_cache_release_date(imdb_id, media_type, release_date, season=None, episode=None):
    conn = get_db_connection()
    conn.execute('''
        INSERT OR REPLACE INTO cache_release_dates
        (imdb_id, media_type, season, episode, release_date, last_checked)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (imdb_id, media_type, season, episode, release_date, datetime.now()))
    conn.commit()
    conn.close()

def load_collected_cache():
    if os.path.exists(COLLECTED_CACHE_FILE):
        with open(COLLECTED_CACHE_FILE, 'rb') as f:
            return pickle.load(f)
    return {'movies': set(), 'episodes': set()}

def save_collected_cache(cache):
    with open(COLLECTED_CACHE_FILE, 'wb') as f:
        pickle.dump(cache, f)

def update_collected_cache(media_type, item):
    cache = load_collected_cache()
    if media_type == 'movie':
        cache['movies'].add((item['title'], item['year']))
    elif media_type == 'episode':
        cache['episodes'].add((item['show_title'], item['season_number'], item['episode_number']))
    save_collected_cache(cache)

def is_in_collected_cache(media_type, item):
    cache = load_collected_cache()
    if media_type == 'movie':
        return (item['title'], item['year']) in cache['movies']
    elif media_type == 'episode':
        return (item['show_title'], item['season_number'], item['episode_number']) in cache['episodes']
    return False

def add_or_update_wanted_movies_batch(movies_batch):
    conn = get_db_connection()
    try:
        existing_collected_movies = conn.execute('SELECT title, year FROM collected_movies').fetchall()
        existing_wanted_movies = conn.execute('SELECT imdb_id FROM wanted_movies').fetchall()

        existing_collected_movie_titles = {(normalize_string(movie['title']), movie['year']) for movie in existing_collected_movies}
        existing_wanted_movie_ids = {movie['imdb_id'] for movie in existing_wanted_movies}

        logger.debug(f"Existing collected movie titles: {existing_collected_movie_titles}")
        logger.debug(f"Existing wanted movie IDs: {existing_wanted_movie_ids}")

        today = date.today()
        added_movies_count = 0
        updated_wanted_movie_ids = set()

        for index, movie in enumerate(movies_batch):
            try:
                normalized_title = normalize_string(movie['title'])
                year = int(movie['year']) if movie['year'] is not None else 0
                imdb_id = movie.get('imdb_id')

                logger.debug(f"Processing movie: {movie}")
                logger.debug(f"Normalized title: {normalized_title}, Year: {year}, IMDb ID: {imdb_id}")

                if imdb_id not in existing_wanted_movie_ids and (normalized_title, year) not in existing_collected_movie_titles:
                    release_date = get_trakt_movie_release_date(movie.get('imdb_id'))

                    if release_date == 'Unknown':
                        release_date = movie.get('release_date', 'Unknown')

                    logger.debug(f"Release date: {release_date}")

                    if release_date != 'Unknown':
                        if isinstance(release_date, str):
                            try:
                                release_date = datetime.strptime(release_date, "%Y-%m-%d").date()
                            except ValueError:
                                logger.debug(f"Invalid date format for movie {normalized_title}: {release_date}")
                                continue
                        elif isinstance(release_date, (datetime, date)):
                            release_date = release_date.date() if isinstance(release_date, datetime) else release_date
                        else:
                            logger.debug(f"Unexpected type for release date of movie {normalized_title}: {type(release_date)}")
                            continue

                        if release_date <= today:
                            conn.execute('''
                                INSERT OR REPLACE INTO wanted_movies
                                (imdb_id, tmdb_id, title, year, release_date, last_updated)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (movie.get('imdb_id'), movie.get('tmdb_id'), normalized_title, year, release_date, datetime.now()))
                            logger.debug(f"Added movie to wanted DB: {normalized_title}, IMDb: {movie['imdb_id']}, TMDb: {movie['tmdb_id']}, Release Date: {release_date}")
                            existing_wanted_movie_ids.add(imdb_id)  # Add to the set to avoid re-adding
                            added_movies_count += 1
                        else:
                            logger.debug(f"Skipping future movie: {normalized_title}, Release Date: {release_date}")
                    else:
                        logger.debug(f"Skipping movie with unknown release date: {normalized_title}")
                updated_wanted_movie_ids.add(imdb_id)  # Mark this movie as still wanted
                if index % 10 == 0:  # Log progress every 10 movies
                    logger.info(f"Processed {index + 1}/{len(movies_batch)} movies")
            except Exception as e:
                logger.debug(f"Error processing movie {movie.get('title', 'Unknown')}: {str(e)}")

        # Remove movies that are no longer wanted
        movies_to_remove = existing_wanted_movie_ids - updated_wanted_movie_ids
        for imdb_id in movies_to_remove:
            conn.execute('DELETE FROM wanted_movies WHERE imdb_id = ?', (imdb_id,))
            logger.debug(f"Removed movie from wanted DB: IMDb ID {imdb_id}")

        conn.commit()
        logger.info(f"Successfully processed batch of {len(movies_batch)} movies, added {added_movies_count} new movies")
    except Exception as e:
        logger.debug(f"Error processing batch of movies: {str(e)}")
    finally:
        conn.close()

def add_or_update_wanted_episodes_batch(episodes_batch):
    conn = get_db_connection()
    try:
        existing_collected_episodes = conn.execute('SELECT show_title, season_number, episode_number FROM collected_episodes').fetchall()
        existing_wanted_episodes = conn.execute('SELECT show_title, season_number, episode_number FROM wanted_episodes').fetchall()

        existing_collected_episode_ids = {(normalize_string(episode['show_title']), episode['season_number'], episode['episode_number']) for episode in existing_collected_episodes}
        existing_wanted_episode_ids = {(normalize_string(episode['show_title']), episode['season_number'], episode['episode_number']) for episode in existing_wanted_episodes}

        today = date.today()
        added_episodes_count = 0
        updated_wanted_episode_ids = set()

        for index, episode in enumerate(episodes_batch):
            try:
                normalized_title = normalize_string(episode['show_title'])
                season_number = int(episode['season_number'])
                episode_number = int(episode['episode_number'])

                logger.debug(f"Processing episode: {normalized_title} S{season_number}E{episode_number}")

                if (normalized_title, season_number, episode_number) not in existing_collected_episode_ids and (normalized_title, season_number, episode_number) not in existing_wanted_episode_ids:
                    release_date = get_trakt_episode_release_date(episode.get('show_imdb_id'), season_number, episode_number)

                    if release_date == 'Unknown':
                        release_date = episode.get('release_date', 'Unknown')

                    if release_date != 'Unknown':
                        if isinstance(release_date, str):
                            try:
                                release_date = datetime.strptime(release_date, "%Y-%m-%d").date()
                            except ValueError:
                                logger.debug(f"Invalid date format for episode {normalized_title} S{season_number}E{episode_number}: {release_date}")
                                continue
                        elif isinstance(release_date, (datetime, date)):
                            release_date = release_date.date() if isinstance(release_date, datetime) else release_date
                        else:
                            logger.debug(f"Unexpected type for release date of episode {normalized_title} S{season_number}E{episode_number}: {type(release_date)}")
                            continue

                        if release_date <= today:
                            year = int(episode['year']) if episode['year'] is not None else 0
                            conn.execute('''
                                INSERT OR REPLACE INTO wanted_episodes
                                (show_imdb_id, show_tmdb_id, show_title, episode_title, year, season_number, episode_number, release_date, last_updated)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                episode['show_imdb_id'], episode['show_tmdb_id'], normalized_title,
                                episode['episode_title'], year, season_number,
                                episode_number, release_date, datetime.now()
                            ))
                            logger.debug(f"Added episode to wanted DB: {normalized_title} S{season_number}E{episode_number}, IMDb: {episode['show_imdb_id']}, TMDb: {episode['show_tmdb_id']}, Release Date: {release_date}")
                            existing_wanted_episode_ids.add((normalized_title, season_number, episode_number))  # Add to the set to avoid re-adding
                            added_episodes_count += 1
                        else:
                            logger.debug(f"Skipping future episode: {normalized_title} S{season_number}E{episode_number}, Release Date: {release_date}")
                    else:
                        logger.debug(f"Skipping episode with unknown release date: {normalized_title} S{season_number}E{episode_number}")
                updated_wanted_episode_ids.add((normalized_title, season_number, episode_number))  # Mark this episode as still wanted
                if index % 10 == 0:  # Log progress every 10 episodes
                    logger.info(f"Processed {index + 1}/{len(episodes_batch)} episodes")
            except Exception as e:
                logger.debug(f"Error processing episode {episode.get('show_title', 'Unknown')} S{episode.get('season_number', 'Unknown')}E{episode.get('episode_number', 'Unknown')}: {str(e)}")

        # Remove episodes that are no longer wanted
        episodes_to_remove = existing_wanted_episode_ids - updated_wanted_episode_ids
        for show_title, season_number, episode_number in episodes_to_remove:
            conn.execute('DELETE FROM wanted_episodes WHERE show_title = ? AND season_number = ? AND episode_number = ?', (show_title, season_number, episode_number))
            logger.debug(f"Removed episode from wanted DB: {show_title} S{season_number}E{episode_number}")

        conn.commit()
        logger.info(f"Successfully processed batch of {len(episodes_batch)} episodes, added {added_episodes_count} new episodes")
    except Exception as e:
        logger.debug(f"Error processing batch of episodes: {str(e)}")
    finally:
        conn.close()

def add_or_update_collected_movies_batch(movies_batch):
    conn = get_db_connection()
    try:
        existing_movies = conn.execute('SELECT title, year FROM collected_movies').fetchall()
        existing_movie_titles = {(normalize_string(movie['title']), movie['year']) for movie in existing_movies}

        new_movie_titles = {(normalize_string(movie['title']), int(movie['year']) if movie['year'] is not None else 0) for movie in movies_batch}

        # Identify movies to delete by checking existing movies that are not in the new batch
        movies_to_delete = existing_movie_titles - new_movie_titles

        logger.debug(f"Movies to delete: {movies_to_delete}")

        # Delete movies not in the new batch
        for movie in movies_to_delete:
            conn.execute('DELETE FROM collected_movies WHERE title = ? AND year = ?', (movie[0], movie[1]))
            logger.debug(f"Deleted movie from DB: {movie[0]} ({movie[1]})")

        added_movies_count = 0
        # Add new movies
        for index, movie in enumerate(movies_batch):
            year = movie['year'] if movie['year'] is not None else 0
            if (normalize_string(movie['title']), int(year)) not in existing_movie_titles:
                conn.execute('''
                    INSERT OR IGNORE INTO collected_movies
                    (imdb_id, tmdb_id, title, year, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                ''', (movie.get('imdb_id'), movie.get('tmdb_id'), movie['title'], int(year), datetime.now()))
                logger.debug(f"Adding movie to DB: {movie['title']}, IMDb: {movie['imdb_id']}, TMDb: {movie['tmdb_id']}")
                added_movies_count += 1
            if index % 10 == 0:  # Log progress every 10 movies
                logger.info(f"Processed {index + 1}/{len(movies_batch)} movies")

        conn.commit()
        logger.info(f"Successfully processed batch of {len(movies_batch)} movies, added {added_movies_count} new movies")
    except Exception as e:
        logger.error(f"Error processing batch of movies: {e}")
    finally:
        conn.close()

def add_or_update_collected_episodes_batch(episodes_batch):
    conn = get_db_connection()
    try:
        existing_episodes = conn.execute('SELECT show_title, season_number, episode_number FROM collected_episodes').fetchall()
        existing_episode_ids = {(normalize_string(episode['show_title']), episode['season_number'], episode['episode_number']) for episode in existing_episodes}

        logger.debug(f"Number of existing episodes: {len(existing_episode_ids)}")
        logger.debug(f"Sample of existing episodes: {list(existing_episode_ids)[:5]}")

        new_episode_ids = set()
        for episode in episodes_batch:
            normalized_title = normalize_string(episode['show_title'])
            episode_id = (normalized_title, int(episode['season_number']), int(episode['episode_number']))
            new_episode_ids.add(episode_id)
            logger.debug(f"Normalized episode: {episode['show_title']} -> {normalized_title}, S{episode['season_number']}E{episode['episode_number']}")

        logger.debug(f"Number of new episodes: {len(new_episode_ids)}")
        logger.debug(f"Sample of new episodes: {list(new_episode_ids)[:5]}")

        episodes_to_delete = existing_episode_ids - new_episode_ids
        logger.debug(f"Number of episodes to delete: {len(episodes_to_delete)}")

        for episode in episodes_to_delete:
            conn.execute('DELETE FROM collected_episodes WHERE show_title = ? AND season_number = ? AND episode_number = ?',
                         (episode[0], episode[1], episode[2]))
            logger.debug(f"Deleted episode from DB: {episode[0]} S{episode[1]}E{episode[2]}")

        added_episodes_count = 0
        for index, episode in enumerate(episodes_batch):
            normalized_title = normalize_string(episode['show_title'])
            episode_id = (normalized_title, int(episode['season_number']), int(episode['episode_number']))

            if episode_id not in existing_episode_ids:
                year = episode['year'] if episode['year'] is not None else 0
                try:
                    conn.execute('''
                        INSERT OR IGNORE INTO collected_episodes
                        (show_imdb_id, show_tmdb_id, show_title, episode_title, year, season_number, episode_number, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        episode['show_imdb_id'], episode['show_tmdb_id'], normalized_title,
                        episode['episode_title'], int(year), int(episode['season_number']),
                        int(episode['episode_number']), datetime.now()
                    ))
                    logger.debug(f"Added episode to DB: {normalized_title} S{episode['season_number']}E{episode['episode_number']}, IMDb: {episode['show_imdb_id']}, TMDb: {episode['show_tmdb_id']}")
                    added_episodes_count += 1
                except sqlite3.IntegrityError as e:
                    logger.error(f"IntegrityError while adding episode: {normalized_title} S{episode['season_number']}E{episode['episode_number']}, Error: {e}")
                except Exception as e:
                    logger.error(f"Error adding episode: {normalized_title} S{episode['season_number']}E{episode['episode_number']}, Error: {e}")
            else:
                logger.debug(f"Skipped existing episode: {normalized_title} S{episode['season_number']}E{episode['episode_number']}")

            if index % 10 == 0:  # Log progress every 10 episodes
                logger.info(f"Processed {index + 1}/{len(episodes_batch)} episodes")

        conn.commit()
        logger.info(f"Successfully processed batch of {len(episodes_batch)} episodes, added {added_episodes_count} new episodes")
    except Exception as e:
        logger.error(f"Error processing batch of episodes: {e}")
    finally:
        conn.close()

def verify_database():
    create_tables()
    logger.info("Database verified and tables created if not exists.")

def get_all_collected_movies():
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM collected_movies')
    movies = cursor.fetchall()
    conn.close()
    return movies

def get_all_collected_episodes():
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM collected_episodes')
    episodes = cursor.fetchall()
    conn.close()
    return episodes

def get_all_wanted_movies():
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM wanted_movies')
    movies = cursor.fetchall()
    conn.close()
    return movies

def get_all_wanted_episodes():
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM wanted_episodes')
    episodes = cursor.fetchall()
    conn.close()
    return episodes

def search_collected_movies(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM collected_movies WHERE title LIKE ?', (f'%{search_term}%',))
    movies = cursor.fetchall()
    conn.close()
    return movies

def search_collected_episodes(search_term):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM collected_episodes WHERE show_title LIKE ? OR episode_title LIKE ?', (f'%{search_term}%', f'%{search_term}%'))
    episodes = cursor.fetchall()
    conn.close()
    return episodes

def purge_database():
    conn = get_db_connection()
    try:
        conn.execute('DROP TABLE IF EXISTS collected_movies')
        conn.execute('DROP TABLE IF EXISTS collected_episodes')
        conn.commit()
        logger.info("Database purged successfully. All tables have been deleted.")
    except Exception as e:
        logger.error(f"Error purging database: {e}")
    finally:
        conn.close()
    create_tables()

def purge_wanted_database():
    conn = get_db_connection()
    try:
        conn.execute('DROP TABLE IF EXISTS wanted_movies')
        conn.execute('DROP TABLE IF EXISTS wanted_episodes')
        conn.commit()
        logger.info("Database purged successfully. All tables have been deleted.")
    except Exception as e:
        logger.error(f"Error purging database: {e}")
    finally:
        conn.close()
    create_tables()

def clone_wanted_to_working():
    conn = get_db_connection()
    try:
        # Clone wanted movies to working movies without replacing existing entries
        conn.execute('''
            INSERT OR IGNORE INTO working_movies
            (imdb_id, tmdb_id, title, year, release_date, state, last_updated)
            SELECT imdb_id, tmdb_id, title, year, release_date, 'Wanted', datetime('now')
            FROM wanted_movies
        ''')

        # Update the existing working_movies entries if necessary
        conn.execute('''
            UPDATE working_movies
            SET title = (SELECT title FROM wanted_movies WHERE wanted_movies.imdb_id = working_movies.imdb_id),
                year = (SELECT year FROM wanted_movies WHERE wanted_movies.imdb_id = working_movies.imdb_id),
                release_date = (SELECT release_date FROM wanted_movies WHERE wanted_movies.imdb_id = working_movies.imdb_id),
                state = 'Wanted',
                last_updated = datetime('now')
            WHERE imdb_id IN (SELECT imdb_id FROM wanted_movies)
        ''')

        # Clone wanted episodes to working episodes without replacing existing entries
        conn.execute('''
            INSERT OR IGNORE INTO working_episodes
            (show_imdb_id, show_tmdb_id, show_title, episode_title, year, season_number, episode_number, release_date, state, last_updated)
            SELECT show_imdb_id, show_tmdb_id, show_title, episode_title, year, season_number, episode_number, release_date, 'Wanted', datetime('now')
            FROM wanted_episodes
        ''')

        # Update the existing working_episodes entries if necessary
        conn.execute('''
            UPDATE working_episodes
            SET show_title = (SELECT show_title FROM wanted_episodes WHERE wanted_episodes.show_imdb_id = working_episodes.show_imdb_id AND wanted_episodes.season_number = working_episodes.season_number AND wanted_episodes.episode_number = working_episodes.episode_number),
                episode_title = (SELECT episode_title FROM wanted_episodes WHERE wanted_episodes.show_imdb_id = working_episodes.show_imdb_id AND wanted_episodes.season_number = working_episodes.season_number AND wanted_episodes.episode_number = working_episodes.episode_number),
                year = (SELECT year FROM wanted_episodes WHERE wanted_episodes.show_imdb_id = working_episodes.show_imdb_id AND wanted_episodes.season_number = working_episodes.season_number AND wanted_episodes.episode_number = working_episodes.episode_number),
                release_date = (SELECT release_date FROM wanted_episodes WHERE wanted_episodes.show_imdb_id = working_episodes.show_imdb_id AND wanted_episodes.season_number = working_episodes.season_number AND wanted_episodes.episode_number = working_episodes.episode_number),
                state = 'Wanted',
                last_updated = datetime('now')
            WHERE (show_imdb_id, season_number, episode_number) IN (SELECT show_imdb_id, season_number, episode_number FROM wanted_episodes)
        ''')

        conn.commit()
        logger.info("Successfully cloned wanted items to working tables.")
    except Exception as e:
        logger.error(f"Error cloning wanted items to working tables: {str(e)}")
    finally:
        conn.close()

def update_working_movie(movie_id, state, filled_by_title=None, filled_by_magnet=None):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE working_movies
            SET state = ?, filled_by_title = ?, filled_by_magnet = ?, last_updated = ?
            WHERE id = ?
        ''', (state, filled_by_title, filled_by_magnet, datetime.now(), movie_id))
        conn.commit()
        logger.debug(f"Updated working movie (ID: {movie_id}) state to {state}")
    except Exception as e:
        logger.error(f"Error updating working movie (ID: {movie_id}): {str(e)}")
    finally:
        conn.close()

def update_working_episode(episode_id, state, filled_by_title=None, filled_by_magnet=None):
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE working_episodes
            SET state = ?, filled_by_title = ?, filled_by_magnet = ?, last_updated = ?
            WHERE id = ?
        ''', (state, filled_by_title, filled_by_magnet, datetime.now(), episode_id))
        conn.commit()
        logger.debug(f"Updated working episode (ID: {episode_id}) state to {state}")
    except Exception as e:
        logger.error(f"Error updating working episode (ID: {episode_id}): {str(e)}")
    finally:
        conn.close()

def get_working_movies_by_state(state):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM working_movies WHERE state = ?', (state,))
    movies = cursor.fetchall()
    conn.close()
    logger.debug(f"Retrieved {len(movies)} movies with state '{state}'. Sample: {dict(movies[0]) if movies else 'No movies'}")
    return movies

def get_working_episodes_by_state(state):
    conn = get_db_connection()
    cursor = conn.execute('SELECT * FROM working_episodes WHERE state = ?', (state,))
    episodes = cursor.fetchall()
    conn.close()
    logger.debug(f"Retrieved {len(episodes)} episodes with state '{state}'. Sample: {dict(episodes[0]) if episodes else 'No episodes'}")
    return episodes

def remove_from_working_movies(movie_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM working_movies WHERE id = ?', (movie_id,))
        conn.commit()
        logger.info(f"Removed movie (ID: {movie_id}) from working movies")
    except Exception as e:
        logger.error(f"Error removing movie (ID: {movie_id}) from working movies: {str(e)}")
    finally:
        conn.close()

def remove_from_working_episodes(episode_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM working_episodes WHERE id = ?', (episode_id,))
        conn.commit()
        logger.info(f"Removed episode (ID: {episode_id}) from working episodes")
    except Exception as e:
        logger.error(f"Error removing episode (ID: {episode_id}) from working episodes: {str(e)}")
    finally:
        conn.close()

# Add these functions to your existing set of functions

def purge_working_database():
    conn = get_db_connection()
    try:
        conn.execute('DROP TABLE IF EXISTS working_movies')
        conn.execute('DROP TABLE IF EXISTS working_episodes')
        conn.commit()
        logger.info("Working database purged successfully. All tables have been deleted.")
    except Exception as e:
        logger.error(f"Error purging working database: {e}")
    finally:
        conn.close()
    create_tables()

def get_title_by_imdb_id(imdb_id: str) -> str:
    logger.info(f"Looking up title for IMDb ID: {imdb_id}")
    conn = get_db_connection()
    cursor = conn.execute('''
        SELECT title FROM wanted_movies WHERE imdb_id = ?
        UNION
        SELECT show_title FROM wanted_episodes WHERE show_imdb_id = ?
    ''', (imdb_id, imdb_id))
    result = cursor.fetchone()
    conn.close()
    if result:
        logger.info(f"Found title: {result['title']} for IMDb ID: {imdb_id}")
        return result['title']
    logger.warning(f"No title found for IMDb ID: {imdb_id}")
    return ""

def remove_from_wanted_movies(title, year):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM wanted_movies WHERE title = ? AND year = ?', (title, year))
        conn.commit()
        logger.info(f"Removed movie from wanted database: {title} ({year})")
    except Exception as e:
        logger.error(f"Error removing movie from wanted database: {title} ({year}): {str(e)}")
    finally:
        conn.close()

def remove_from_wanted_episodes(show_title, season_number, episode_number):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM wanted_episodes WHERE show_title = ? AND season_number = ? AND episode_number = ?', 
                     (show_title, season_number, episode_number))
        conn.commit()
        logger.info(f"Removed episode from wanted database: {show_title} S{season_number}E{episode_number}")
    except Exception as e:
        logger.error(f"Error removing episode from wanted database: {show_title} S{season_number}E{episode_number}: {str(e)}")
    finally:
        conn.close()

async def fetch_item_status(item_id: int) -> str:
    conn = get_db_connection()
    try:
        cursor = conn.execute('SELECT state FROM working_episodes WHERE id = ?', (item_id,))
        row = cursor.fetchone()
        if row:
            return row['state']
        else:
            logger.error(f"Item with id {item_id} not found in database.")
            return 'Unknown'
    except Exception as e:
        logger.error(f"Error fetching status for item id {item_id}: {str(e)}")
        return 'Unknown'
    finally:
        conn.close()
