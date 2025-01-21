import sqlite3
import os
import shutil
import logging
from typing import Dict, List, Any
from utilities.file_lock import FileLock

PLEX_DB_PATH = "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"

def copy_db(source_path, destination_path):
    with open(source_path, 'rb') as source_file:
        with FileLock(source_file):
            with open(destination_path, 'wb') as dest_file:
                shutil.copyfileobj(source_file, dest_file)

def get_collected_from_plex(request: str = 'all') -> Dict[str, List[Dict[str, Any]]]:
    try:
        temp_db_path = PLEX_DB_PATH + '.temp'
        logging.debug(f"Copying Plex database to temporary file: {temp_db_path}")
        copy_db(PLEX_DB_PATH, temp_db_path)

        collected_content = {'movies': [], 'episodes': []}

        try:
            logging.debug("Connecting to the temporary database")
            conn = sqlite3.connect(f'file:{temp_db_path}?mode=ro', uri=True)
            cursor = conn.cursor()

            # Find the correct tag_type for IMDb IDs
            cursor.execute("SELECT DISTINCT tag_type FROM tags WHERE tag LIKE 'imdb://tt%' LIMIT 1;")
            imdb_tag_type = cursor.fetchone()
            if imdb_tag_type:
                imdb_tag_type = imdb_tag_type[0]
                logging.debug(f"Found IMDb tag_type: {imdb_tag_type}")
            else:
                logging.error("Could not find IMDb tag_type")
                return collected_content

            # Query for movies with IMDb IDs
            movie_query = f"""
            SELECT metadata_items.id, tags.tag, metadata_items.title, metadata_items.year,
                   metadata_items.added_at, metadata_items.originally_available_at
            FROM metadata_items
            INNER JOIN taggings ON metadata_items.id = taggings.metadata_item_id
            INNER JOIN tags ON tags.id = taggings.tag_id
            WHERE metadata_items.library_section_id = 31
            AND tags.tag_type = {imdb_tag_type}
            AND tags.tag LIKE 'imdb://tt%'
            ORDER BY metadata_items.id;
            """
            
            if request == 'recent':
                movie_query = movie_query.replace("ORDER BY", "AND metadata_items.added_at > datetime('now', '-4 hours') ORDER BY")
            
            logging.debug(f"Executing movie query: {movie_query}")
            cursor.execute(movie_query)
            
            movie_results = cursor.fetchall()
            logging.debug(f"Fetched {len(movie_results)} movie results")
            
            for row in movie_results:
                movie_id, imdb_tag, title, year, added_at, release_date = row
                imdb_id = imdb_tag.split('://')[1]
                
                collected_content['movies'].append({
                    'imdb_id': imdb_id,
                    'title': title,
                    'year': year,
                    'addedAt': added_at,
                    'release_date': release_date,
                })
                logging.debug(f"Added movie: {title} (IMDb: {imdb_id})")

            # Query for TV shows with IMDb IDs
            show_query = f"""
            SELECT metadata_items.id, tags.tag, metadata_items.title, metadata_items.year,
                   metadata_items.added_at, metadata_items.originally_available_at
            FROM metadata_items
            INNER JOIN taggings ON metadata_items.id = taggings.metadata_item_id
            INNER JOIN tags ON tags.id = taggings.tag_id
            WHERE metadata_items.library_section_id = 30
            AND tags.tag_type = {imdb_tag_type}
            AND tags.tag LIKE 'imdb://tt%'
            ORDER BY metadata_items.id;
            """
            
            if request == 'recent':
                show_query = show_query.replace("ORDER BY", "AND metadata_items.added_at > datetime('now', '-4 hours') ORDER BY")
            
            logging.debug(f"Executing TV show query: {show_query}")
            cursor.execute(show_query)
            
            show_results = cursor.fetchall()
            logging.debug(f"Fetched {len(show_results)} TV show results")
            
            for row in show_results:
                show_id, imdb_tag, title, year, added_at, release_date = row
                imdb_id = imdb_tag.split('://')[1]
                
                collected_content['episodes'].append({
                    'imdb_id': imdb_id,
                    'title': title,
                    'year': year,
                    'addedAt': added_at,
                    'release_date': release_date,
                })
                logging.debug(f"Added TV show: {title} (IMDb: {imdb_id})")

            conn.close()
            logging.debug("Database connection closed")

        finally:
            logging.debug(f"Removing temporary database file: {temp_db_path}")
            os.remove(temp_db_path)

        logging.debug(f"Collection complete: {len(collected_content['movies'])} movies and {len(collected_content['episodes'])} TV shows collected.")

        return collected_content

    except Exception as e:
        logging.error(f"Error collecting content from Plex database: {str(e)}", exc_info=True)
        return None