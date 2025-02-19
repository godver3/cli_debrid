import logging
import os

def update_show_ids():
    """Update show IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
    import sqlite3
    from cli_battery.app.direct_api import DirectAPI
    import os
    from fuzzywuzzy import fuzz
    import re
    import json
    api = DirectAPI()

    logging.info("Starting show ID update task")
    # Connect to media_items.db
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    db_path = os.path.join(db_content_dir, 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Get all unique shows by grouping episodes
        cursor.execute("""
            SELECT 
                title,
                imdb_id,
                year,
                GROUP_CONCAT(id) as episode_ids,
                COUNT(*) as episode_count
            FROM media_items 
            WHERE type='episode'
            GROUP BY title, imdb_id
        """)
        shows = cursor.fetchall()
        
        logging.info(f"Found {len(shows)} shows to check")

        for show in shows:
            try:
                show_title = show['title']
                show_imdb_id = show['imdb_id']
                episode_ids = show['episode_ids'].split(',')
                show_year = show['year']

                # Get show metadata from direct API
                metadata, source = api.get_show_metadata(show_imdb_id)

                if not metadata:
                    logging.warning(f"No metadata found in API for show {show_title} (imdb_id: {show_imdb_id})")
                    # Try searching Trakt directly
                    from cli_battery.app.trakt_metadata import TraktMetadata
                    trakt = TraktMetadata()
                    
                    sanitized_title = show_title
                    if '(' in show_title:
                        sanitized_title = show_title[:show_title.rfind('(')].strip()
                    
                    logging.info(f"Searching Trakt for show '{sanitized_title}'{f' ({show_year})' if show_year else ''}")
                    url = f"{trakt.base_url}/search/show?query={sanitized_title}"
                    response = trakt._make_request(url)
                    
                    if response and response.status_code == 200:
                        results = response.json()
                        if results:
                            for result in results:
                                show_data = result['show']
                                trakt_title = show_data['title']
                                trakt_year = show_data.get('year')
                                
                                similarity = fuzz.ratio(sanitized_title.lower(), trakt_title.lower())
                                
                                # Log result with year if available
                                year_match_str = ""
                                year_match = False
                                if show_year and trakt_year:
                                    year_match = show_year == trakt_year
                                    year_match_str = f", year match: {year_match} ({trakt_year})"
                                logging.info(f"Trakt result: '{trakt_title}' (similarity: {similarity}%{year_match_str})")
                                
                                # Consider both title similarity and year match
                                if similarity >= 85 and (year_match or not show_year or not trakt_year):
                                    new_imdb_id = show_data['ids'].get('imdb')
                                    if new_imdb_id:
                                        logging.info(f"Found potential match - Title: '{trakt_title}'{f', Year: {trakt_year}' if trakt_year else ''}, IMDb ID: {new_imdb_id}")
                                        logging.info(f"Attempting to get metadata with new IMDb ID: {new_imdb_id}")
                                        new_metadata, new_source = api.get_show_metadata(new_imdb_id)
                                        if new_metadata:
                                            metadata = new_metadata
                                            source = new_source
                                            logging.info(f"Successfully retrieved metadata using new IMDb ID for '{trakt_title}'")
                                            break
                                        else:
                                            logging.warning(f"Failed to get metadata for matching show using new IMDb ID: {new_imdb_id}")
                                    else:
                                        logging.warning(f"Matching show found but no IMDb ID available in Trakt data")
                                        logging.debug(f"Full Trakt show data for match: {show_data}")
                                        # Try TMDB as fallback if we have TMDB ID
                                        tmdb_id = show_data['ids'].get('tmdb')
                                        if tmdb_id:
                                            logging.info(f"Attempting to get IMDb ID from TMDB (ID: {tmdb_id})")
                                            # TMDB API requires an API key - we should get this from config
                                            from settings import get_setting
                                            tmdb_api_key = get_setting('TMDB','api_key')
                                            if tmdb_api_key:
                                                import requests
                                                tmdb_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                                                try:
                                                    tmdb_response = requests.get(tmdb_url)
                                                    if tmdb_response.status_code == 200:
                                                        tmdb_data = tmdb_response.json()
                                                        logging.debug(f"Full TMDB response: {tmdb_data}")
                                                        new_imdb_id = tmdb_data.get('imdb_id')
                                                        if new_imdb_id:
                                                            logging.info(f"Found IMDb ID from TMDB: {new_imdb_id}")
                                                            # Try getting metadata with this IMDb ID
                                                            new_metadata, new_source = api.get_show_metadata(new_imdb_id)
                                                            if new_metadata:
                                                                metadata = new_metadata
                                                                source = new_source
                                                                logging.info(f"Successfully retrieved metadata using IMDb ID from TMDB")
                                                                break
                                                            else:
                                                                logging.warning(f"Failed to get metadata using IMDb ID from TMDB: {new_imdb_id}")
                                                        else:
                                                            logging.warning("TMDB response did not contain IMDb ID")
                                                    else:
                                                        logging.warning(f"Failed to get TMDB data: {tmdb_response.status_code}")
                                                except Exception as e:
                                                    logging.warning(f"Error fetching from TMDB API: {str(e)}")
                                            else:
                                                logging.warning("TMDB API key not found")
                                else:
                                    skip_reason = []
                                    if similarity < 85:
                                        skip_reason.append("low similarity")
                                    if show_year and trakt_year and show_year != trakt_year:
                                        skip_reason.append("year mismatch")
                                    logging.debug(f"Skipping result '{trakt_title}' due to {' and '.join(skip_reason)}")
                
                if not metadata:
                    logging.warning(f"Could not find show metadata even after Trakt search")
                    continue

            except Exception as e:
                logging.error(f"Error processing show {show_title}: {str(e)}")
                continue

            # Get IDs from the nested 'ids' dictionary
            api_imdb_id = metadata.get('ids', {}).get('imdb')
            api_tmdb_id = metadata.get('ids', {}).get('tmdb')

            if not api_imdb_id:
                logging.warning(f"Show '{show_title}' - API returned no IMDB ID (current: {show_imdb_id})")
            elif api_imdb_id != show_imdb_id:
                logging.warning(f"Show comparison: {show_title} - Database IMDB ID: {show_imdb_id}, API IMDB ID: {api_imdb_id} - MISMATCH")
                
                # Get current aliases if any
                cursor.execute("SELECT imdb_aliases FROM media_items WHERE id = ? LIMIT 1", (episode_ids[0],))
                current_aliases_row = cursor.fetchone()
                current_aliases = []
                if current_aliases_row and current_aliases_row[0]:
                    try:
                        current_aliases = json.loads(current_aliases_row[0])
                    except json.JSONDecodeError:
                        logging.warning(f"Failed to decode existing imdb_aliases for {show_title}")
                
                # Add old IMDb ID to aliases if not already there
                if show_imdb_id and show_imdb_id not in current_aliases:
                    current_aliases.append(show_imdb_id)
                
                # Update the database with new IDs and aliases
                cursor.execute("""
                    UPDATE media_items
                    SET imdb_id = ?, tmdb_id = ?, imdb_aliases = ?
                    WHERE id IN ({})
                """.format(','.join('?' * len(episode_ids))), 
                    [api_imdb_id, api_tmdb_id, json.dumps(current_aliases)] + episode_ids)
                conn.commit()
                logging.info(f"Updated show '{show_title}' with new IMDb ID: {api_imdb_id} (old ID {show_imdb_id} added to aliases)")
            else:
                logging.info(f"Show comparison: {show_title} - Database IMDB ID: {show_imdb_id}, API IMDB ID: {api_imdb_id} - MATCH")

    except Exception as e:
        logging.error(f"Error in task_update_show_ids: {str(e)}")
    finally:
        cursor.close()
        conn.close()


def update_show_titles():
    """Update show titles in the database if they don't match the direct API, storing old titles in title_aliases."""
    import sqlite3
    from cli_battery.app.direct_api import DirectAPI
    import os
    from fuzzywuzzy import fuzz
    import re
    import json
    api = DirectAPI()

    logging.info("Starting show title update task")
    # Connect to media_items.db
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    db_path = os.path.join(db_content_dir, 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Get all unique shows by grouping episodes
        cursor.execute("""
            SELECT 
                title,
                imdb_id,
                year,
                GROUP_CONCAT(id) as episode_ids,
                COUNT(*) as episode_count
            FROM media_items 
            WHERE type='episode'
            GROUP BY title, imdb_id
        """)
        shows = cursor.fetchall()
        
        logging.info(f"Found {len(shows)} shows to check")

        for show in shows:
            try:
                show_title = str(show['title']) if show['title'] is not None else ''  # Convert to string to handle integer titles
                show_imdb_id = show['imdb_id']
                episode_ids = show['episode_ids'].split(',')
                show_year = show['year']

                # Get show metadata from direct API
                metadata, source = api.get_show_metadata(show_imdb_id)

                if metadata:
                    # Get title from metadata
                    api_title = str(metadata.get('title', ''))  # Convert API title to string as well
                    
                    if not api_title:
                        logging.warning(f"Show '{show_title}' - API returned no title")
                        continue
                                            
                    # Compare titles (case-insensitive)
                    if api_title.lower() != show_title.lower():
                        logging.info(f"Show title mismatch - Database: '{show_title}', API: '{api_title}'")
                        
                        # Get current title aliases if any
                        cursor.execute("SELECT title_aliases FROM media_items WHERE id = ? LIMIT 1", (episode_ids[0],))
                        current_aliases_row = cursor.fetchone()
                        current_aliases = []
                        if current_aliases_row and current_aliases_row[0]:
                            try:
                                current_aliases = json.loads(current_aliases_row[0])
                            except json.JSONDecodeError:
                                logging.warning(f"Failed to decode existing title_aliases for {show_title}")
                        
                        # Add old title to aliases if not already there
                        if show_title and show_title not in current_aliases:
                            current_aliases.append(show_title)
                        
                        new_title = api_title

                        # Update the database with new title and aliases
                        cursor.execute("""
                            UPDATE media_items
                            SET title = ?, title_aliases = ?
                            WHERE id IN ({})
                        """.format(','.join('?' * len(episode_ids))), 
                            [new_title, json.dumps(current_aliases)] + episode_ids)
                        conn.commit()
                        logging.info(f"Updated show title from '{show_title}' to '{new_title}' (old title added to aliases)")
                    else:
                        logging.info(f"Show title match - '{show_title}'")
                else:
                    logging.warning(f"No metadata found in API for show {show_title} (imdb_id: {show_imdb_id})")

            except Exception as e:
                logging.error(f"Error processing show {show['title']}: {str(e)}")
                continue

    except Exception as e:
        logging.error(f"Error in update_show_titles: {str(e)}")
    finally:
        cursor.close()
        conn.close()

def update_movie_ids():
    """Update movie IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
    import sqlite3
    from cli_battery.app.direct_api import DirectAPI
    import os
    from fuzzywuzzy import fuzz
    import re
    import json
    api = DirectAPI()

    logging.info("Starting movie ID update task")
    # Connect to media_items.db
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    db_path = os.path.join(db_content_dir, 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Get all movies
        cursor.execute("""
            SELECT 
                id,
                title,
                imdb_id,
                year
            FROM media_items 
            WHERE type='movie'
        """)
        movies = cursor.fetchall()
        
        logging.info(f"Found {len(movies)} movies to check")

        for movie in movies:
            try:
                movie_title = movie['title']
                movie_imdb_id = movie['imdb_id']
                movie_id = movie['id']
                movie_year = movie['year']

                # Get movie metadata from direct API
                metadata, source = api.get_movie_metadata(movie_imdb_id)

                if not metadata:
                    logging.warning(f"No metadata found in API for movie {movie_title} (imdb_id: {movie_imdb_id})")
                    # Try searching Trakt directly
                    from cli_battery.app.trakt_metadata import TraktMetadata
                    trakt = TraktMetadata()
                    
                    sanitized_title = movie_title
                    if '(' in movie_title:
                        sanitized_title = movie_title[:movie_title.rfind('(')].strip()
                    
                    logging.info(f"Searching Trakt for movie '{sanitized_title}'{f' ({movie_year})' if movie_year else ''}")
                    url = f"{trakt.base_url}/search/movie?query={sanitized_title}"
                    response = trakt._make_request(url)
                    
                    if response and response.status_code == 200:
                        results = response.json()
                        if results:
                            for result in results:
                                movie_data = result['movie']
                                trakt_title = movie_data['title']
                                trakt_year = movie_data.get('year')
                                
                                similarity = fuzz.ratio(sanitized_title.lower(), trakt_title.lower())
                                
                                # Log result with year if available
                                year_match_str = ""
                                year_match = False
                                if movie_year and trakt_year:
                                    year_match = movie_year == trakt_year
                                    year_match_str = f", year match: {year_match} ({trakt_year})"
                                logging.info(f"Trakt result: '{trakt_title}' (similarity: {similarity}%{year_match_str})")
                                
                                # Consider both title similarity and year match
                                if similarity >= 85 and (year_match or not movie_year or not trakt_year):
                                    new_imdb_id = movie_data['ids'].get('imdb')
                                    if new_imdb_id:
                                        logging.info(f"Found potential match - Title: '{trakt_title}'{f', Year: {trakt_year}' if trakt_year else ''}, IMDb ID: {new_imdb_id}")
                                        logging.info(f"Attempting to get metadata with new IMDb ID: {new_imdb_id}")
                                        new_metadata, new_source = api.get_movie_metadata(new_imdb_id)
                                        if new_metadata:
                                            metadata = new_metadata
                                            source = new_source
                                            logging.info(f"Successfully retrieved metadata using new IMDb ID for '{trakt_title}'")
                                            break
                                        else:
                                            logging.warning(f"Failed to get metadata for matching movie using new IMDb ID: {new_imdb_id}")
                                    else:
                                        logging.warning(f"Matching movie found but no IMDb ID available in Trakt data")
                                        logging.debug(f"Full Trakt movie data for match: {movie_data}")
                                        # Try TMDB as fallback if we have TMDB ID
                                        tmdb_id = movie_data['ids'].get('tmdb')
                                        if tmdb_id:
                                            logging.info(f"Attempting to get IMDb ID from TMDB (ID: {tmdb_id})")
                                            # TMDB API requires an API key - we should get this from config
                                            from settings import get_setting
                                            tmdb_api_key = get_setting('TMDB','api_key')
                                            if tmdb_api_key:
                                                import requests
                                                tmdb_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={tmdb_api_key}"
                                                try:
                                                    tmdb_response = requests.get(tmdb_url)
                                                    if tmdb_response.status_code == 200:
                                                        tmdb_data = tmdb_response.json()
                                                        logging.debug(f"Full TMDB response: {tmdb_data}")
                                                        new_imdb_id = tmdb_data.get('imdb_id')
                                                        if new_imdb_id:
                                                            logging.info(f"Found IMDb ID from TMDB: {new_imdb_id}")
                                                            # Try getting metadata with this IMDb ID
                                                            new_metadata, new_source = api.get_movie_metadata(new_imdb_id)
                                                            if new_metadata:
                                                                metadata = new_metadata
                                                                source = new_source
                                                                logging.info(f"Successfully retrieved metadata using IMDb ID from TMDB")
                                                                break
                                                            else:
                                                                logging.warning(f"Failed to get metadata using IMDb ID from TMDB: {new_imdb_id}")
                                                        else:
                                                            logging.warning("TMDB response did not contain IMDb ID")
                                                    else:
                                                        logging.warning(f"Failed to get TMDB data: {tmdb_response.status_code}")
                                                except Exception as e:
                                                    logging.warning(f"Error fetching from TMDB API: {str(e)}")
                                            else:
                                                logging.warning("TMDB API key not found")
                                else:
                                    skip_reason = []
                                    if similarity < 85:
                                        skip_reason.append("low similarity")
                                    if movie_year and trakt_year and movie_year != trakt_year:
                                        skip_reason.append("year mismatch")
                                    logging.debug(f"Skipping result '{trakt_title}' due to {' and '.join(skip_reason)}")
                
                if not metadata:
                    logging.warning(f"Could not find movie metadata even after Trakt search")
                    continue

            except Exception as e:
                logging.error(f"Error processing movie {movie_title}: {str(e)}")
                continue

            # Get IDs from the nested 'ids' dictionary
            api_imdb_id = metadata.get('ids', {}).get('imdb')
            api_tmdb_id = metadata.get('ids', {}).get('tmdb')

            if not api_imdb_id:
                logging.warning(f"Movie '{movie_title}' - API returned no IMDB ID (current: {movie_imdb_id})")
            elif api_imdb_id != movie_imdb_id:
                logging.warning(f"Movie comparison: {movie_title} - Database IMDB ID: {movie_imdb_id}, API IMDB ID: {api_imdb_id} - MISMATCH")
                
                # Get current aliases if any
                cursor.execute("SELECT imdb_aliases FROM media_items WHERE id = ?", (movie_id,))
                current_aliases_row = cursor.fetchone()
                current_aliases = []
                if current_aliases_row and current_aliases_row[0]:
                    try:
                        current_aliases = json.loads(current_aliases_row[0])
                    except json.JSONDecodeError:
                        logging.warning(f"Failed to decode existing imdb_aliases for {movie_title}")
                
                # Add old IMDb ID to aliases if not already there
                if movie_imdb_id and movie_imdb_id not in current_aliases:
                    current_aliases.append(movie_imdb_id)
                
                # Update the database with new IDs and aliases
                cursor.execute("""
                    UPDATE media_items
                    SET imdb_id = ?, tmdb_id = ?, imdb_aliases = ?
                    WHERE id = ?
                """, [api_imdb_id, api_tmdb_id, json.dumps(current_aliases), movie_id])
                conn.commit()
                logging.info(f"Updated movie '{movie_title}' with new IMDb ID: {api_imdb_id} (old ID {movie_imdb_id} added to aliases)")
            else:
                logging.info(f"Movie comparison: {movie_title} - Database IMDB ID: {movie_imdb_id}, API IMDB ID: {api_imdb_id} - MATCH")

    except Exception as e:
        logging.error(f"Error in update_movie_ids: {str(e)}")
    finally:
        cursor.close()
        conn.close()

def update_movie_titles():
    """Update movie titles in the database if they don't match the direct API, storing old titles in title_aliases."""
    import sqlite3
    from cli_battery.app.direct_api import DirectAPI
    import os
    from fuzzywuzzy import fuzz
    import re
    import json
    api = DirectAPI()

    logging.info("Starting movie title update task")
    # Connect to media_items.db
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    db_path = os.path.join(db_content_dir, 'media_items.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # Get all movies
        cursor.execute("""
            SELECT 
                id,
                title,
                imdb_id,
                year
            FROM media_items 
            WHERE type='movie'
        """)
        movies = cursor.fetchall()
        
        logging.info(f"Found {len(movies)} movies to check")

        for movie in movies:
            try:
                movie_title = str(movie['title']) if movie['title'] is not None else ''  # Convert to string to handle integer titles
                movie_imdb_id = movie['imdb_id']
                movie_id = movie['id']
                movie_year = movie['year']

                # Get movie metadata from direct API
                metadata, source = api.get_movie_metadata(movie_imdb_id)

                if metadata:
                    # Get title from metadata
                    api_title = str(metadata.get('title', ''))  # Convert API title to string as well
                    
                    if not api_title:
                        logging.warning(f"Movie '{movie_title}' - API returned no title")
                        continue
                                            
                    # Compare titles (case-insensitive)
                    if api_title.lower() != movie_title.lower():
                        logging.info(f"Movie title mismatch - Database: '{movie_title}', API: '{api_title}'")
                        
                        # Get current title aliases if any
                        cursor.execute("SELECT title_aliases FROM media_items WHERE id = ?", (movie_id,))
                        current_aliases_row = cursor.fetchone()
                        current_aliases = []
                        if current_aliases_row and current_aliases_row[0]:
                            try:
                                current_aliases = json.loads(current_aliases_row[0])
                            except json.JSONDecodeError:
                                logging.warning(f"Failed to decode existing title_aliases for {movie_title}")
                        
                        # Add old title to aliases if not already there
                        if movie_title and movie_title not in current_aliases:
                            current_aliases.append(movie_title)
                        
                        new_title = api_title

                        # Update the database with new title and aliases
                        cursor.execute("""
                            UPDATE media_items
                            SET title = ?, title_aliases = ?
                            WHERE id = ?
                        """, [new_title, json.dumps(current_aliases), movie_id])
                        conn.commit()
                        logging.info(f"Updated movie title from '{movie_title}' to '{new_title}' (old title added to aliases)")
                    else:
                        logging.info(f"Movie title match - '{movie_title}'")
                else:
                    logging.warning(f"No metadata found in API for movie {movie_title} (imdb_id: {movie_imdb_id})")

            except Exception as e:
                logging.error(f"Error processing movie {movie['title']}: {str(e)}")
                continue

    except Exception as e:
        logging.error(f"Error in update_movie_titles: {str(e)}")
    finally:
        cursor.close()
        conn.close()

def run_plex_library_maintenance():
    """
    Run maintenance tasks specific to Plex library management.
    - Verify Plex library existence and accessibility
    - Check media file presence in mounted location
    - Validate database location_on_disk against mounted files
    - Clean up orphaned records
    """
    logging.info("Starting Plex library maintenance tasks")
    try:
        from settings import get_setting
        from utilities.plex_removal_cache import cache_plex_removal
        from routes.debug_routes import move_item_to_wanted
        import os
        
        # Check if mount location is configured and enabled
        mounted_path = get_setting('Plex', 'mounted_file_location')
        disable_library_checks = get_setting('Plex', 'disable_plex_library_checks')
        
        if not mounted_path or disable_library_checks:
            logging.info("Mount location checks disabled or not configured, skipping file verification")
            return
            
        if not os.path.exists(mounted_path):
            logging.error(f"Configured mount location does not exist: {mounted_path}")
            return
            
        # Check if mount location appears to be empty
        try:
            # Use os.listdir() to check if directory has any contents
            dir_contents = os.listdir(mounted_path)
            if not dir_contents:
                logging.error(f"Mount location appears to be empty, possible mount issue: {mounted_path}")
                return
            logging.info(f"Mount location verified with {len(dir_contents)} items")
        except Exception as e:
            logging.error(f"Error accessing mount location {mounted_path}: {str(e)}")
            return
            
        logging.info(f"Using mount location: {mounted_path}")
            
        # Connect to database
        import sqlite3
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_content_dir, 'media_items.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        try:
            # Get all media items that should have files
            cursor.execute("""
                SELECT 
                    id, 
                    type, 
                    title, 
                    location_on_disk,
                    state,
                    filled_by_file,
                    filled_by_torrent_id,
                    original_path_for_symlink,
                    version,
                    episode_title
                FROM media_items 
                WHERE state IN ('Collected', 'Upgrading')
                AND location_on_disk IS NOT NULL
            """)
            media_items = cursor.fetchall()
            
            total_items = len(media_items)
            logging.info(f"Found {total_items} collected items with location_on_disk")
            
            missing_files = []
            verified_files = 0
            movies_checked = 0
            episodes_checked = 0
            movies_missing = 0
            episodes_missing = 0
            
            for item in media_items:
                (item_id, item_type, title, location_on_disk, state, 
                 filled_by_file, filled_by_torrent_id, original_path, version, episode_title) = item
                
                if not location_on_disk:
                    continue
                    
                # Track item type counts
                if item_type == 'movie':
                    movies_checked += 1
                elif item_type == 'episode':
                    episodes_checked += 1
                    
                # Construct full path using mount location
                full_path = os.path.join(mounted_path, location_on_disk)
                
                if not os.path.exists(full_path):
                    logging.warning(f"File not found for {title}: {full_path}")
                    missing_files.append({
                        'id': item_id,
                        'title': title,
                        'type': item_type,
                        'path': full_path,
                        'torrent_id': filled_by_torrent_id,
                        'original_file': filled_by_file,
                        'original_path': original_path,
                        'version': version,
                        'episode_title': episode_title
                    })
                    
                    # Track missing by type
                    if item_type == 'movie':
                        movies_missing += 1
                    elif item_type == 'episode':
                        episodes_missing += 1
                else:
                    verified_files += 1
            
            # Log summary statistics
            logging.info("\n=== Library Maintenance Summary ===")
            logging.info(f"Total items checked: {total_items}")
            logging.info(f"Files verified: {verified_files}")
            logging.info(f"Files missing: {len(missing_files)}")
            logging.info("\nBy Content Type:")
            logging.info(f"Movies checked: {movies_checked}")
            logging.info(f"Episodes checked: {episodes_checked}")
            logging.info(f"Movies missing: {movies_missing}")
            logging.info(f"Episodes missing: {episodes_missing}")
            
            if missing_files:
                logging.warning("\n=== Processing Missing Files ===")
                for missing in missing_files:
                    logging.warning(f"\nProcessing: {missing['title']} ({missing['type']})")
                    logging.warning(f"  Path: {missing['path']}")
                    
                    # Get the version without asterisks
                    clean_version = missing['version'].replace('*', '') if missing['version'] else None
                    
                    if clean_version:
                        # Check for other items with the same version and matching identifiers
                        if missing['type'] == 'movie':
                            # For movies, compare version and imdb_id
                            cursor.execute("""
                                SELECT COUNT(*) 
                                FROM media_items 
                                WHERE state IN ('Collected', 'Upgrading')
                                AND version LIKE ?
                                AND id != ?
                                AND type = 'movie'
                                AND imdb_id = (SELECT imdb_id FROM media_items WHERE id = ?)
                                AND location_on_disk IS NOT NULL
                                AND EXISTS (
                                    SELECT 1 
                                    FROM media_items m2 
                                    WHERE m2.id = media_items.id 
                                    AND m2.location_on_disk IS NOT NULL
                                    AND EXISTS (
                                        SELECT 1 
                                        FROM media_items m3 
                                        WHERE m3.location_on_disk = m2.location_on_disk 
                                        AND m3.id = m2.id
                                    )
                                )
                            """, (f"%{clean_version}%", missing['id'], missing['id']))
                        else:
                            # For TV shows, compare version, imdb_id, season_number, and episode_number
                            cursor.execute("""
                                SELECT COUNT(*) 
                                FROM media_items 
                                WHERE state IN ('Collected', 'Upgrading')
                                AND version LIKE ?
                                AND id != ?
                                AND type = 'episode'
                                AND imdb_id = (SELECT imdb_id FROM media_items WHERE id = ?)
                                AND season_number = (SELECT season_number FROM media_items WHERE id = ?)
                                AND episode_number = (SELECT episode_number FROM media_items WHERE id = ?)
                                AND location_on_disk IS NOT NULL
                                AND EXISTS (
                                    SELECT 1 
                                    FROM media_items m2 
                                    WHERE m2.id = media_items.id 
                                    AND m2.location_on_disk IS NOT NULL
                                    AND EXISTS (
                                        SELECT 1 
                                        FROM media_items m3 
                                        WHERE m3.location_on_disk = m2.location_on_disk 
                                        AND m3.id = m2.id
                                    )
                                )
                            """, (f"%{clean_version}%", missing['id'], missing['id'], missing['id'], missing['id']))
                        
                        remaining_items = cursor.fetchone()[0]
                        
                        logging.info(f"  Version: {clean_version}")
                        logging.info(f"  Remaining items with same version and identifiers: {remaining_items}")
                        
                        if remaining_items == 0:
                            # No other items with this version, move to rescrape
                            logging.info(f"  No other items with version {clean_version}, moving to rescrape")
                            try:
                                move_item_to_wanted(missing['id'])
                                logging.info(f"  Successfully moved item to Wanted state for rescraping")
                            except Exception as e:
                                logging.error(f"  Error moving item to Wanted state: {str(e)}")
                        else:
                            # Other items exist, just delete this one
                            logging.info(f"  Other items exist with version {clean_version}, deleting this item")
                            cursor.execute("DELETE FROM media_items WHERE id = ?", (missing['id'],))
                            conn.commit()
                    else:
                        # No version info, just delete
                        logging.info("  No version information, deleting item")
                        cursor.execute("DELETE FROM media_items WHERE id = ?", (missing['id'],))
                        conn.commit()
                    
                    # Remove from Plex
                    try:
                        if missing['type'] == 'movie':
                            cache_plex_removal(missing['title'], missing['original_file'])
                        else:
                            cache_plex_removal(missing['title'], missing['original_file'], missing['episode_title'])
                        logging.info("  Queued for removal from Plex")
                    except Exception as e:
                        logging.error(f"  Error queueing Plex removal: {str(e)}")
            
            logging.info("\nPlex library maintenance completed")
            
        finally:
            cursor.close()
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in Plex library maintenance: {str(e)}")

def check_database_symlinks(cursor, symlink_base_path):
    """
    Phase 1: Check all symlinks registered in the database.
    Returns a list of broken links with their issues.
    """
    broken_links = []  # (id, title, symlink_path, target_path, reason)
    checked_folders = set()
    
    cursor.execute("""
        SELECT 
            id,
            title,
            type,
            original_path_for_symlink,
            location_on_disk,
            version
        FROM media_items 
        WHERE state IN ('Collected', 'Upgrading')
        AND original_path_for_symlink IS NOT NULL
        AND location_on_disk IS NOT NULL
    """)
    db_items = cursor.fetchall()
    total_items = len(db_items)
    
    logging.info(f"Checking {total_items} symlinked items from database")
    
    for idx, item in enumerate(db_items, 1):
        if idx % 500 == 0:
            logging.info(f"Progress: {idx}/{total_items} items checked ({(idx/total_items)*100:.1f}%)")
            
        item_id, title, item_type, original_path, location_on_disk, version = item
        full_symlink_path = os.path.join(symlink_base_path, location_on_disk)
        parent_folder = os.path.dirname(full_symlink_path)
        checked_folders.add(parent_folder)
        
        # Basic existence check
        if not os.path.lexists(full_symlink_path):
            broken_links.append((item_id, title, full_symlink_path, None, "missing_symlink", version))
            continue
            
        # Verify it's actually a symlink
        if not os.path.islink(full_symlink_path):
            broken_links.append((item_id, title, full_symlink_path, None, "not_symlink", version))
            continue
            
        # Get the target and check if it exists
        try:
            target_path = os.readlink(full_symlink_path)
            if not os.path.exists(target_path):
                broken_links.append((item_id, title, full_symlink_path, target_path, "broken_target", version))
                continue
                
            # Optional: Verify target is readable
            if not os.access(target_path, os.R_OK):
                broken_links.append((item_id, title, full_symlink_path, target_path, "target_not_readable", version))
                continue
            
        except OSError as e:
            broken_links.append((item_id, title, full_symlink_path, None, f"error: {str(e)}", version))
    
    logging.info(f"Phase 1 complete - Found {len(broken_links)} broken links")
    return broken_links, checked_folders

def check_orphaned_symlinks(cursor, symlink_base_path, checked_folders):
    """
    Phase 2: Find symlinks in the filesystem that aren't tracked in the database.
    Returns a list of orphaned symlinks.
    """
    orphaned_links = []  # (symlink_path, target_path, file_size, modified_time)
    processed_dirs = 0
    total_dirs = 0
    
    logging.info("\nScanning filesystem for orphaned symlinks")
    
    # Pre-fetch all database paths into a lookup dictionary
    cursor.execute("""
        SELECT id, title, type, state, version, location_on_disk
        FROM media_items 
        WHERE state IN ('Collected', 'Upgrading')
        AND location_on_disk IS NOT NULL
    """)
    db_paths = {}
    for row in cursor.fetchall():
        full_path = os.path.join(symlink_base_path, row[5]) if row[5] else None
        if full_path:
            db_paths[full_path] = row
    
    logging.info(f"Loaded {len(db_paths)} paths from database")

    # First count directories for progress tracking
    logging.info("Counting directories to process...")
    for root, dirs, _ in os.walk(symlink_base_path):
        if root not in checked_folders:
            total_dirs += 1
    logging.info(f"Found {total_dirs} directories to check")

    # Walk the symlink directory
    for root, _, files in os.walk(symlink_base_path):
        # Skip folders we already checked in Phase 1
        if root in checked_folders:
            continue
            
        processed_dirs += 1
        if processed_dirs % 10 == 0 or processed_dirs == total_dirs:
            logging.info(f"Progress: {processed_dirs}/{total_dirs} directories checked ({(processed_dirs/total_dirs)*100:.1f}%)")
            
        for filename in files:
            full_path = os.path.join(root, filename)
            
            # Only process symlinks
            if not os.path.islink(full_path):
                continue
                
            try:
                # Get symlink information
                target_path = os.readlink(full_path)
                
                # Check if this symlink is in the database
                if full_path in db_paths:
                    continue
                
                # If we get here, the symlink is orphaned
                try:
                    file_size = os.path.getsize(full_path) if os.path.exists(full_path) else 0
                    modified_time = os.path.getmtime(full_path) if os.path.exists(full_path) else 0
                except OSError:
                    file_size = 0
                    modified_time = 0
                
                orphaned_links.append({
                    'path': full_path,
                    'target': target_path,
                    'size': file_size,
                    'modified': modified_time,
                    'target_exists': os.path.exists(target_path)
                })
                
            except OSError as e:
                logging.error(f"Error processing symlink {full_path}: {str(e)}")
    
    logging.info(f"\nPhase 2 complete - Found {len(orphaned_links)} orphaned symlinks in {processed_dirs} directories")
    return orphaned_links

def handle_maintenance_actions(cursor, broken_links, orphaned_links):
    """
    Phase 3: Take action on broken and orphaned symlinks.
    - Remove orphaned symlinks from disk
    - Handle broken symlinks by either moving to wanted state or removing from database
    """
    logging.info("\n=== Phase 3: Taking Action on Issues ===")
    
    # Handle orphaned symlinks first - just remove them from disk
    if orphaned_links:
        logging.info(f"\nRemoving {len(orphaned_links)} orphaned symlinks")
        for link in orphaned_links:
            try:
                # First remove the symlink from disk
                symlink_path = link['path']
                os.unlink(symlink_path)
                logging.info(f"Removed orphaned symlink from disk: {symlink_path}")

            except OSError as e:
                logging.error(f"Failed to remove orphaned symlink {link['path']}: {str(e)}")
    
    # Handle broken symlinks
    if broken_links:
        logging.info(f"\nProcessing {len(broken_links)} broken symlinks")
        for item_id, title, symlink_path, target_path, reason, version in broken_links:
            logging.info(f"\nProcessing: {title}")
            logging.info(f"  Symlink: {symlink_path}")
            if target_path:
                logging.info(f"  Target: {target_path}")
            logging.info(f"  Issue: {reason}")
            
            try:
                # First remove the symlink if it exists
                if os.path.lexists(symlink_path):
                    os.unlink(symlink_path)
                    logging.info("  Removed broken symlink from disk")
                    from time import sleep
                    sleep(1)  # Give the filesystem a moment to register the change
                    
                    # Then remove from Plex
                    from utilities.plex_functions import remove_file_from_plex
                    # Get episode title if this is a TV show
                    cursor.execute("""
                        SELECT type, episode_title 
                        FROM media_items 
                        WHERE id = ?
                    """, (item_id,))
                    result = cursor.fetchone()
                    if result:
                        item_type, episode_title = result
                        from utilities.plex_removal_cache import cache_plex_removal
                        if item_type == 'episode':
                            cache_plex_removal(title, symlink_path, episode_title)
                        else:
                            cache_plex_removal(title, symlink_path)
                        logging.info("  Queued for removal from Plex")
            except OSError as e:
                logging.error(f"  Failed to remove symlink {symlink_path}: {str(e)}")
                continue
            
            # Get the version without asterisks
            clean_version = version.replace('*', '') if version else None
            
            if clean_version:
                # Check for other items with the same version and matching identifiers
                cursor.execute("""
                    SELECT type FROM media_items WHERE id = ?
                """, (item_id,))
                item_type = cursor.fetchone()
                if not item_type:
                    logging.error(f"  Could not find item type for ID {item_id}")
                    continue
                    
                item_type = item_type[0]
                
                if item_type == 'movie':
                    # For movies, compare version and imdb_id
                    cursor.execute("""
                        SELECT COUNT(*) 
                        FROM media_items 
                        WHERE state IN ('Collected', 'Upgrading')
                        AND version LIKE ?
                        AND id != ?
                        AND type = 'movie'
                        AND imdb_id = (SELECT imdb_id FROM media_items WHERE id = ?)
                        AND location_on_disk IS NOT NULL
                        AND EXISTS (
                            SELECT 1 
                            FROM media_items m2 
                            WHERE m2.id = media_items.id 
                            AND m2.location_on_disk IS NOT NULL
                            AND EXISTS (
                                SELECT 1 
                                FROM media_items m3 
                                WHERE m3.location_on_disk = m2.location_on_disk 
                                AND m3.id = m2.id
                            )
                        )
                    """, (f"%{clean_version}%", item_id, item_id))
                else:
                    # For TV shows, compare version, imdb_id, season_number, and episode_number
                    cursor.execute("""
                        SELECT COUNT(*) 
                        FROM media_items 
                        WHERE state IN ('Collected', 'Upgrading')
                        AND version LIKE ?
                        AND id != ?
                        AND type = 'episode'
                        AND imdb_id = (SELECT imdb_id FROM media_items WHERE id = ?)
                        AND season_number = (SELECT season_number FROM media_items WHERE id = ?)
                        AND episode_number = (SELECT episode_number FROM media_items WHERE id = ?)
                        AND location_on_disk IS NOT NULL
                        AND EXISTS (
                            SELECT 1 
                            FROM media_items m2 
                            WHERE m2.id = media_items.id 
                            AND m2.location_on_disk IS NOT NULL
                            AND EXISTS (
                                SELECT 1 
                                FROM media_items m3 
                                WHERE m3.location_on_disk = m2.location_on_disk 
                                AND m3.id = m2.id
                            )
                        )
                    """, (f"%{clean_version}%", item_id, item_id, item_id, item_id))
                
                remaining_items = cursor.fetchone()[0]
                
                logging.info(f"  Version: {clean_version}")
                logging.info(f"  Remaining items with same version and identifiers: {remaining_items}")
                
                if remaining_items == 0:
                    # No other items with this version, move to rescrape
                    logging.info(f"  No other items with version {clean_version}, moving to rescrape")
                    try:
                        from routes.debug_routes import move_item_to_wanted
                        move_item_to_wanted(item_id)
                        logging.info(f"  Successfully moved item to Wanted state for rescraping")
                    except Exception as e:
                        logging.error(f"  Error moving item to Wanted state: {str(e)}")
                else:
                    # Other items exist, just delete this one
                    logging.info(f"  Other items exist with version {clean_version}, deleting this item")
                    cursor.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
                    cursor.connection.commit()
            else:
                # No version info, just delete
                logging.info("  No version information, deleting item")
                cursor.execute("DELETE FROM media_items WHERE id = ?", (item_id,))
                cursor.connection.commit()

def run_symlink_library_maintenance(skip_phase_1=False, skip_phase_2=False):
    """
    Run maintenance tasks specific to Symlinked/Local library management.
    Generates a report of issues and takes corrective action.
    
    Args:
        skip_phase_1 (bool): Debug flag to skip Phase 1 checks
        skip_phase_2 (bool): Debug flag to skip Phase 2 checks
    """
    logging.info("Starting Symlinked/Local library maintenance tasks")
    try:
        from settings import get_setting
        import os
        import sqlite3
        
        # Get symlink base path from settings
        symlink_base_path = get_setting('File Management', 'symlinked_files_path')
        
        if not symlink_base_path:
            logging.error("Symlink path not configured")
            return
            
        if not os.path.exists(symlink_base_path):
            logging.error(f"Symlink location does not exist: {symlink_base_path}")
            return
            
        # Connect to database
        db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        db_path = os.path.join(db_content_dir, 'media_items.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        try:
            checked_folders = set()
            broken_links = []
            orphaned_links = []
            
            # Phase 1: Check database symlinks
            if not skip_phase_1:
                logging.info("\n=== Phase 1: Checking Database Symlinks ===")
                broken_links, checked_folders = check_database_symlinks(cursor, symlink_base_path)
                
                # Report broken links
                if broken_links:
                    logging.warning("\nBroken Symlinks Report:")
                    for item_id, title, symlink, target, reason, version in broken_links:
                        logging.warning(f"\nItem {item_id} - {title}:")
                        logging.warning(f"  Symlink: {symlink}")
                        if target:
                            logging.warning(f"  Target: {target}")
                        logging.warning(f"  Issue: {reason}")
                        if version:
                            logging.warning(f"  Version: {version}")
                            
                    # Group by issue type for summary
                    issues_by_type = {}
                    for _, _, _, _, reason, _ in broken_links:
                        issues_by_type[reason] = issues_by_type.get(reason, 0) + 1
                    
                    logging.warning("\nBroken Links Summary:")
                    for issue_type, count in issues_by_type.items():
                        logging.warning(f"  {issue_type}: {count} items")
                else:
                    logging.info("\nNo broken symlinks found in database")
            else:
                logging.info("\nSkipping Phase 1 (Debug Mode)")
            
            # Phase 2: Check for orphaned symlinks
            if not skip_phase_2:
                logging.info("\n=== Phase 2: Checking for Orphaned Symlinks ===")
                orphaned_links = check_orphaned_symlinks(cursor, symlink_base_path, checked_folders)
                
                # Report orphaned links
                if orphaned_links:
                    # Group by whether target exists
                    valid_targets = [l for l in orphaned_links if l['target_exists']]
                    broken_targets = [l for l in orphaned_links if not l['target_exists']]
                    
                    logging.warning("\nOrphaned Symlinks Report:")
                    logging.warning(f"Total orphaned symlinks found: {len(orphaned_links)}")
                    logging.warning(f"  With valid targets: {len(valid_targets)}")
                    logging.warning(f"  With broken targets: {len(broken_targets)}")
                    
                    if valid_targets:
                        logging.warning("\nOrphaned symlinks with valid targets:")
                        for link in valid_targets:
                            logging.warning(f"  {link['path']} -> {link['target']}")
                            
                    if broken_targets:
                        logging.warning("\nOrphaned symlinks with broken targets:")
                        for link in broken_targets:
                            logging.warning(f"  {link['path']} -> {link['target']}")
                else:
                    logging.info("\nNo orphaned symlinks found")
            else:
                logging.info("\nSkipping Phase 2 (Debug Mode)")
            
            # Phase 3: Take action on issues
            handle_maintenance_actions(cursor, broken_links, orphaned_links)
            
            logging.info("\nSymlink library maintenance completed")
            
        finally:
            cursor.close()
            conn.close()
            
    except Exception as e:
        logging.error(f"Error in Symlinked/Local library maintenance: {str(e)}")

def run_library_maintenance():
    """
    Run library maintenance tasks to ensure database consistency and cleanup.
    This function will be called periodically when enabled in debug settings.
    """
    logging.info("Starting library maintenance task")
    
    try:
        from settings import get_setting
        from utilities.plex_removal_cache import process_removal_cache

        # Remove previous Plex removals
        process_removal_cache()

        # Get collection management type
        collection_type = get_setting('File Management', 'file_collection_management')
        
        # Run specific maintenance tasks based on collection type
        if collection_type == 'Plex':
            run_plex_library_maintenance()
        elif collection_type == 'Symlinked/Local':
            run_symlink_library_maintenance()
        else:
            logging.warning(f"Unknown collection management type: {collection_type}")
        
        logging.info("Library maintenance task completed successfully")
        
    except Exception as e:
        logging.error(f"Error in library maintenance task: {str(e)}")