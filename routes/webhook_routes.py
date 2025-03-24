from flask import jsonify, request, Blueprint
import logging
from queues.run_program import process_overseerr_webhook
from routes.extensions import app
from queues.queue_manager import QueueManager
from utilities.local_library_scan import check_local_file_for_item, get_symlink_path, create_symlink
from utilities.plex_functions import plex_update_item
from utilities.emby_functions import emby_update_item
from utilities.settings import get_setting
from urllib.parse import unquote
import os.path
from content_checkers.overseerr import get_overseerr_details, get_overseerr_headers
from routes.api_tracker import api
from typing import Dict, Any
from scraper.functions.ptt_parser import parse_with_ptt
from cli_battery.app.trakt_metadata import TraktMetadata
from cli_battery.app.direct_api import DirectAPI
from fuzzywuzzy import fuzz
from database.core import get_db_connection
from utilities.reverse_parser import parse_filename_for_version
from datetime import datetime, timezone
from database.database_writing import update_media_item_state, add_media_item, update_release_date_and_state
from database.database_reading import get_media_item_by_id, get_media_item_presence
import json
from metadata.metadata import get_release_date
import time

webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/', methods=['POST'])
def webhook():
    data = request.json
    logging.debug(f"Received webhook: {data}")
    try:
        # Handle test notifications separately
        if data.get('notification_type') == 'TEST_NOTIFICATION':
            logging.info("Received test notification from Overseerr")
            return jsonify({"status": "success", "message": "Test notification received"}), 200

        # If this is a TV show request, look for season information
        if data.get('media', {}).get('media_type') == 'tv':
            # Look for season information in the extra field
            extra_items = data.get('extra', [])
            for item in extra_items:
                if item.get('name') == 'Requested Seasons':
                    try:
                        # The value could be a single season or a comma-separated list
                        seasons_str = item.get('value', '')
                        requested_seasons = [int(s.strip()) for s in seasons_str.split(',')]
                        if requested_seasons:
                            # Add to media section
                            data['media']['requested_seasons'] = requested_seasons
                            logging.info(f"Added season information to webhook data: {requested_seasons}")
                    except ValueError as e:
                        logging.error(f"Error parsing season information from webhook: {str(e)}")

            # Only process if partial requests are allowed
            if get_setting('debug', 'allow_partial_overseerr_requests', False):
                logging.info("Partial requests are not allowed, clearing requested seasons")
                if 'requested_seasons' in data['media']:
                    del data['media']['requested_seasons']

        # Mark this request as coming from Overseerr
        if data.get('media'):
            data['media']['from_overseerr'] = True

        logging.debug(f"Final webhook data before processing: {data}")
        process_overseerr_webhook(data)
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logging.error(f"Error processing webhook: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def handle_rclone_file(file_path: str) -> Dict[str, Any]:
    """
    Handle a file from rclone webhook by creating a database entry and symlink.
    This bypasses the upgrading logic since these are new files.
    """
    try:
        # Get the full path components
        path_parts = file_path.split('/')
        filename = path_parts[-1]  # The actual file
        folder_name = path_parts[-2] if len(path_parts) > 1 else ''  # The containing folder
        logging.debug(f"Processing rclone file: {filename} from folder: {folder_name}")

        # First check the checking queue for matches
        queue_manager = QueueManager()
        checking_queue = queue_manager.queues["Checking"]
        matched_items = [item for item in checking_queue.items if item.get('filled_by_file') == filename]
        
        if matched_items:
            logging.info(f"Found {len(matched_items)} matching items in checking queue")
            processed_items = []
            
            for item in matched_items:
                logging.info(f"Processing matching item {item['id']} for file {filename}")
                # Update the item with the new file location
                update_media_item_state(
                    item['id'],
                    'Collected',
                    filled_by_file=filename,
                    filled_by_title=folder_name  # Use folder name as title
                )
                processed_items.append(item['id'])
            
            return {
                'success': True,
                'message': f"Processed {len(processed_items)} items from checking queue",
                'processed_items': processed_items
            }

        # If no matches in checking queue, proceed with metadata lookup
        logging.info(f"No matches found in checking queue, proceeding with metadata lookup")

        # Process the file using just the filename
        result = process_rclone_file(filename)
        
        if not result['success']:
            return result

        if not (result.get('metadata') and result.get('best_match')):
            return {
                'success': False,
                'message': "Missing metadata or match information"
            }

        # Get the source and destination paths
        original_path = get_setting('File Management', 'original_files_path')
        source_file = os.path.join(original_path, folder_name, filename)  # Use folder name in path

        # Prepare item for database
        item = {
            'title': result['metadata'].get('title'),
            'year': result['metadata'].get('year'),
            'imdb_id': result['best_match']['ids'].get('imdb'),
            'tmdb_id': result['best_match']['ids'].get('tmdb'),
            'type': 'movie' if not result['parsed_info'].get('episodes') and not result['parsed_info'].get('seasons') else 'episode',
            'version': result.get('version', ''),
            'filled_by_file': filename,
            'filled_by_title': folder_name,  # Use folder name as title
            'genres': json.dumps(result['metadata'].get('genres', [])),  # Convert genres list to JSON string
            'release_date': result.get('release_date'),  # Use the release date from process_rclone_file
            'state': 'Collected',  # Set initial state as Collected
            'collected_at': datetime.now(),
            'original_collected_at': datetime.now(),
            'original_path_for_symlink': source_file
        }

        # Add episode-specific information if it's a TV show
        if item['type'] == 'episode':
            item.update({
                'season_number': result['parsed_info'].get('season', 1),
                'episode_number': result['parsed_info'].get('episodes', [1])[0] if result['parsed_info'].get('episodes') else 1,
                'episode_title': result['metadata'].get('episode_title', '')
            })

        # Get destination path before adding to database
        dest_file = get_symlink_path(item, source_file)
        if not dest_file:
            return {
                'success': False,
                'message': "Failed to generate symlink path"
            }

        # Add location to item
        item['location_on_disk'] = dest_file

        # Add item to database
        item_id = add_media_item(item)
        if not item_id:
            return {
                'success': False,
                'message': "Failed to add item to database"
            }
        
        item['id'] = item_id

        update_release_date_and_state(
            item_id,
            item['release_date'],
            'Collected'
        )

        # Create symlink
        symlink_success = create_symlink(source_file, dest_file, item_id)
        if not symlink_success:
            return {
                'success': False,
                'message': "Failed to create symlink"
            }

        # Update media servers
        if get_setting('Debug', 'emby_jellyfin_url', default=False):
            emby_update_item(item)
            logging.info(f"Updated Emby/Jellyfin for item {item_id}")
        elif get_setting('File Management', 'plex_url_for_symlink', default=False):
            plex_update_item(item)
            logging.info(f"Updated Plex for item {item_id}")

        return {
            'success': True,
            'message': "Successfully processed file",
            'item': item,
            'symlink_path': dest_file
        }

    except Exception as e:
        logging.error(f"Error handling rclone file: {str(e)}")
        return {
            'success': False,
            'message': str(e)
        }

@webhook_bp.route('/rclone', methods=['POST', 'GET'])
def rclone_webhook():
    try:
        file_path = request.args.get('file')
        
        if not file_path:
            return jsonify({"status": "error", "message": "No file path provided"}), 400

        # URL decode the file path
        file_path = unquote(file_path)
        logging.info(f"Received rclone webhook for path: {file_path}")

        # Strip off the leading directory (movies/)
        if '/' in file_path:
            file_path = file_path.split('/', 1)[1]
        logging.debug(f"Processing path after stripping prefix: {file_path}")

        # Get the original path from settings
        original_path = get_setting('File Management', 'original_files_path')
        logging.debug(f"Original path from settings: {original_path}")
        
        # Get the full path - this is a directory containing the media file
        dir_path = os.path.join(original_path, file_path)
        logging.debug(f"Full directory path: {dir_path}")

        # Try up to 10 times with 5 second delay
        for attempt in range(10):
            if os.path.exists(dir_path):
                break
            if attempt < 9:  # Don't sleep on last attempt
                logging.info(f"Directory not found, attempt {attempt + 1}/10. Retrying in 5 seconds...")
                time.sleep(5)
        
        if not os.path.exists(dir_path):
            logging.error(f"Directory not found after 10 attempts: {dir_path}")
            return jsonify({
                "status": "error",
                "message": f"Directory not found: {file_path}"
            }), 404

        # List directory contents to debug
        try:
            dir_contents = os.listdir(dir_path)
            logging.debug(f"Directory contents: {dir_contents}")
        except Exception as e:
            logging.error(f"Error listing directory: {str(e)}")
            return jsonify({
                "status": "error",
                "message": f"Error listing directory: {str(e)}"
            }), 500

        results = []
        # Look for media files in the directory
        for file in dir_contents:
            file_path = os.path.join(dir_path, file)
            logging.debug(f"Checking file: {file}")
            if os.path.isfile(file_path) and file.lower().endswith(('.mkv', '.mp4', '.avi')):
                logging.info(f"Found media file: {file}")
                rel_path = os.path.relpath(file_path, original_path)
                logging.debug(f"Processing relative path: {rel_path}")
                result = handle_rclone_file(rel_path)
                results.append({
                    'file': rel_path,
                    'result': result
                })
            else:
                logging.debug(f"Skipping non-media file: {file}")

        if not results:
            logging.warning("No media files were processed")
            
        return jsonify({
            "status": "success",
            "message": f"Processed {len(results)} files",
            "details": results
        }), 200

    except Exception as e:
        logging.error(f"Error processing rclone webhook: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

def process_rclone_file(file_path: str) -> Dict[str, Any]:
    """
    Process a file from rclone webhook, checking if it matches any existing items
    or if it should create a new symlink.
    
    Args:
        file_path: The path to the file
        
    Returns:
        Dict containing the processing results
    """
    try:
        # Import dependencies inside function to avoid circular imports
        from queues.queue_manager import QueueManager
        from utilities.local_library_scan import check_local_file_for_item
        from utilities.plex_functions import plex_update_item
        from utilities.emby_functions import emby_update_item
        from utilities.settings import get_setting
        from database.core import get_db_connection
        from database.database_reading import get_media_item_presence
        
        # Get just the filename for matching
        filename = os.path.basename(file_path)
        
        # First check the checking queue for matches
        queue_manager = QueueManager()
        checking_queue = queue_manager.queues["Checking"]
        matched_items = [item for item in checking_queue.items if item.get('filled_by_title') == filename]
        
        if matched_items:
            logging.info(f"Found {len(matched_items)} matching items in checking queue")
            processed_items = []
            
            for item in matched_items:
                logging.info(f"Processing matching item {item['id']} for file {filename}")
                if check_local_file_for_item(item, is_webhook=True):
                    logging.info(f"Local file found and symlinked for item {item['id']}")
                    
                    # Check for Plex or Emby/Jellyfin configuration and update accordingly
                    if get_setting('Debug', 'emby_jellyfin_url', default=False):
                        emby_update_item(item)
                    elif get_setting('File Management', 'plex_url_for_symlink', default=False):
                        plex_update_item(item)
                    
                    # Check if the item was marked for upgrading
                    conn = get_db_connection()
                    cursor = conn.execute('SELECT state FROM media_items WHERE id = ?', (item['id'],))
                    current_state = cursor.fetchone()['state']
                    conn.close()
                    
                    if current_state == 'Upgrading':
                        logging.info(f"Item {item['id']} is marked for upgrading, keeping in Upgrading state")
                    else:
                        # Move to collected without creating another notification
                        queue_manager.move_to_collected(item, "Checking", skip_notification=True)
                    
                    processed_items.append(item['id'])
            
            return {
                'success': True,
                'message': f"Processed {len(processed_items)} items from checking queue",
                'processed_items': processed_items
            }
            
        # If no matches in checking queue, proceed with parsing and metadata lookup
        logging.info(f"No matches found in checking queue, proceeding with metadata lookup")
        
        # Import additional dependencies needed for metadata lookup
        from scraper.functions.ptt_parser import parse_with_ptt
        from cli_battery.app.trakt_metadata import TraktMetadata
        from cli_battery.app.direct_api import DirectAPI
        from fuzzywuzzy import fuzz
        
        # Parse the filename using PTT
        parsed_info = parse_with_ptt(filename)
        
        if not parsed_info or parsed_info.get('parsing_error'):
            logging.error(f"Failed to parse filename: {filename}")
            return {
                'success': False,
                'message': f"Failed to parse filename: {filename}"
            }
            
        # Initialize APIs
        trakt = TraktMetadata()
        api = DirectAPI()
        
        # Clean up title for search
        sanitized_title = parsed_info['title']
        if '(' in sanitized_title:
            sanitized_title = sanitized_title[:sanitized_title.rfind('(')].strip()
            
        logging.info(f"Searching metadata for: {sanitized_title}")
        
        # Search based on media type - check if episodes or seasons exist to determine type
        search_type = 'show' if (parsed_info.get('episodes') or parsed_info.get('seasons')) else 'movie'
        logging.info(f"Detected media type: {search_type}")
        
        # For TV shows with 'US' in title, append it to search to improve matching
        if search_type == 'show' and 'US' in filename:
            sanitized_title = f"{sanitized_title} US"
            logging.info(f"Adjusted search title for US show: {sanitized_title}")
            
        url = f"{trakt.base_url}/search/{search_type}?query={sanitized_title}"
        response = trakt._make_request(url)
        
        if not response or response.status_code != 200:
            logging.error(f"Failed to search Trakt for {sanitized_title}")
            return {
                'success': False,
                'message': f"Failed to find metadata for {sanitized_title}"
            }
            
        results = response.json()
        if not results:
            logging.error(f"No results found for {sanitized_title}")
            return {
                'success': False,
                'message': f"No metadata found for {sanitized_title}"
            }
            
        # Find best match considering title similarity and year
        best_match = None
        best_score = 0
        
        for result in results:
            media_data = result[search_type]  # 'movie' or 'show' based on type
            trakt_title = media_data['title']
            trakt_year = media_data.get('year')
            
            # Calculate match score
            title_similarity = fuzz.ratio(sanitized_title.lower(), trakt_title.lower())
            
            # Year match bonus
            year_match = False
            if parsed_info.get('year') and trakt_year:
                year_match = int(parsed_info['year']) == int(trakt_year)
            
            # Adjust score based on year match
            final_score = title_similarity + (20 if year_match else 0)
            
            logging.debug(f"Match candidate: '{trakt_title}' ({trakt_year}) - Score: {final_score}")
            
            if final_score > best_score:
                best_score = final_score
                best_match = media_data
        
        # Require a minimum match score - more flexible for TV shows
        min_score = 80 if search_type == 'show' else 85
        if not best_match or best_score < min_score:
            logging.error(f"No good matches found for {sanitized_title} (best score: {best_score}, required: {min_score})")
            return {
                'success': False,
                'message': f"No confident metadata match found for {sanitized_title}"
            }
            
        # Get IMDb ID from best match
        imdb_id = best_match['ids'].get('imdb')
        if not imdb_id:
            logging.error(f"No IMDb ID found for matched title: {best_match['title']}")
            return {
                'success': False,
                'message': f"No IMDb ID found for matched title: {best_match['title']}"
            }

        # Check if this item already exists in our database
        item_state = get_media_item_presence(imdb_id=imdb_id)
        logging.info(f"Item state: {item_state}")
        if item_state != "Missing":
            # Get the existing item to check if it's the same file
            conn = get_db_connection()
            try:
                if search_type == 'show' and parsed_info.get('season') and parsed_info.get('episodes'):
                    # For TV shows, check specific episode
                    cursor = conn.execute('''
                        SELECT filled_by_file FROM media_items 
                        WHERE imdb_id = ? AND type = 'episode'
                        AND season_number = ? AND episode_number = ?
                    ''', (imdb_id, parsed_info['season'], parsed_info['episodes'][0]))
                else:
                    # For movies, just check the movie
                    cursor = conn.execute('SELECT filled_by_file FROM media_items WHERE imdb_id = ? AND type = "movie"', (imdb_id,))
                
                existing_items = cursor.fetchall()
                logging.info(f"Found {len(existing_items)} existing items with matching IMDB ID")
                
                for existing_item in existing_items:
                    logging.info(f"Comparing existing file '{existing_item['filled_by_file']}' with new file '{filename}'")
                    if existing_item['filled_by_file'] == filename:
                        logging.info(f"Found exact same file already in database: {filename}")
                        return {
                            'success': False,
                            'message': f"File {filename} already exists in database"
                        }
            finally:
                conn.close()
            
        # Get full metadata from DirectAPI
        if search_type == 'movie':
            metadata, source = api.get_movie_metadata(imdb_id)
        else:
            metadata, source = api.get_show_metadata(imdb_id)
            
        if not metadata:
            logging.error(f"Failed to get metadata from DirectAPI for IMDb ID: {imdb_id}")
            return {
                'success': False,
                'message': f"Failed to get full metadata for {best_match['title']}"
            }
            
        logging.info(f"Successfully found metadata for {sanitized_title}")
        logging.info(f"Matched to: {metadata.get('title')} ({metadata.get('year')}) - IMDb ID: {imdb_id}")
        
        # Use reverse parser to determine version from filename
        version = parse_filename_for_version(filename)
        logging.info(f"Determined version from filename: {version}")

        # Get release date using the proper function
        if search_type == 'movie':
            release_date = get_release_date(metadata, imdb_id)
        else:
            release_date = metadata.get('first_aired')
            if release_date and isinstance(release_date, str) and 'T' in release_date:
                # Trim ISO format to just YYYY-MM-DD
                release_date = release_date.split('T')[0]
        logging.info(f"Got release date: {release_date}")
        
        # Parse the date string, handling both full timestamps and simple dates
        try:
            if 'T' in release_date:
                # Parse full ISO timestamp
                first_aired_utc = datetime.strptime(release_date, "%Y-%m-%dT%H:%M:%S.%fZ")
                first_aired_utc = first_aired_utc.replace(tzinfo=timezone.utc)

                # Convert UTC to local timezone
                from metadata.metadata import _get_local_timezone
                local_tz = _get_local_timezone()
                local_dt = first_aired_utc.astimezone(local_tz)
                
                # Format the local date as string
                release_date = local_dt.strftime("%Y-%m-%d")
            else:
                # Already in YYYY-MM-DD format, no conversion needed
                datetime.strptime(release_date, "%Y-%m-%d")  # Validate the date format
        except ValueError as e:
            logging.error(f"Error parsing release date '{release_date}': {str(e)}")
            release_date = None

        # Return the parsed info, metadata, and version information
        return {
            'success': True,
            'message': f"Found metadata for {sanitized_title}",
            'parsed_info': parsed_info,
            'metadata': metadata,
            'source': source,
            'file_path': file_path,
            'filename': filename,
            'version': version,
            'best_match': best_match,  # Include best_match in the return value
            'release_date': release_date  # Include the release date
        }
        
    except Exception as e:
        logging.error(f"Error processing rclone file: {str(e)}")
        return {
            'success': False,
            'message': str(e)
        }