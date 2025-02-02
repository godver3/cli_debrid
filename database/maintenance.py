import logging

def update_show_ids():
    """Update show IDs (imdb_id and tmdb_id) in the database if they don't match the direct API."""
    import sqlite3
    from cli_battery.app.direct_api import DirectAPI
    import os
    from fuzzywuzzy import fuzz
    import re
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
                show_title = show['title']
                show_imdb_id = show['imdb_id']
                episode_ids = show['episode_ids'].split(',')
                show_year = show['year']

                # Get show metadata from direct API
                metadata, source = api.get_show_metadata(show_imdb_id)

                if metadata:
                    # Get title from metadata
                    api_title = metadata.get('title')
                    
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
                logging.error(f"Error processing show {show_title}: {str(e)}")
                continue

    except Exception as e:
        logging.error(f"Error in update_show_titles: {str(e)}")
    finally:
        cursor.close()
        conn.close()