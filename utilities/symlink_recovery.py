"""Rebuild the library database from an existing symlink folder.

Two flavours: cli_debrid's own symlink layout and Riven's. Each analyzer walks
the symlink root, parses every link back to a title/episode and its target in
the original (mount) root, and reports what could be recovered. Runs as a
background thread; progress per task id lives in analysis_progress /
riven_analysis_progress for the progress streams. Used by Debug Functions ->
"Recover symlinks".
"""

import json
import logging
import os
import re
from pathlib import Path

from cli_battery.app.direct_api import DirectAPI
from utilities.local_library_scan import get_symlink_path
from utilities.reverse_parser import parse_filename_for_version
from utilities.settings import get_all_settings, get_setting


# Progress per task id, read by the /debug/analysis_progress stream.
analysis_progress = {}


# Same for the Riven analyzer (/debug/riven_analysis_progress).
riven_analysis_progress = {}


def parse_symlink(symlink_path: Path):
    """Parses a symlink path based on filename patterns, not templates."""
    filename = symlink_path.name
    parsed_data = {
        'symlink_path': str(symlink_path),
        'original_path_for_symlink': None, # Populated in analyze_symlinks
        'media_type': None, # Determined below
        'imdb_id': None, # Determined below
        'tmdb_id': None, # Populated by get_metadata
        'title': None, # Populated by get_metadata
        'year': None, # Populated by get_metadata
        'season_number': None, # Determined below
        'episode_number': None, # Determined below
        'episode_title': None, # Populated by get_metadata
        'version': None, # Populated by reverse_parser in analyze_symlinks
        'original_filename': None, # Populated in analyze_symlinks
        'is_anime': False # Populated by get_metadata
    }

    # 1. Extract IMDb ID (tt#######) - try filename first, then full path
    imdb_match = re.search(r'(tt\d{7,})', filename, re.IGNORECASE)
    if imdb_match:
        parsed_data['imdb_id'] = imdb_match.group(1)
    else:
        # Try searching in the full path as fallback
        full_path_str = str(symlink_path)
        imdb_match = re.search(r'(tt\d{7,})', full_path_str, re.IGNORECASE)
        if imdb_match:
            parsed_data['imdb_id'] = imdb_match.group(1)
            logging.info(f"Found IMDb ID in full path: {parsed_data['imdb_id']} for {filename}")
        else:
            logging.warning(f"Could not extract IMDb ID from filename or full path: {filename}")
            return None # Cannot proceed without IMDb ID

    # 2. Extract Season and Episode Numbers (S##E## or similar)
    # More robust regex to handle variations like S01E01, S1E1, Season 1 Episode 1, 1x01 etc.
    # Also handles multi-episode formats like S11E17-E18 or S11E17E18
    se_match = re.search(r'[Ss](\d{1,2})[EeXx](\d{1,3}(?:[EeXx]\d{1,3})*)|Season\s?(\d{1,2})\s?Episode\s?(\d{1,3})|(\d{1,2})[Xx](\d{1,3})', filename)
    if se_match:
        parsed_data['media_type'] = 'episode'
        # Extract numbers from the first matching group that isn't None
        if se_match.group(1) is not None and se_match.group(2) is not None:
            parsed_data['season_number'] = int(se_match.group(1))
            episode_match = se_match.group(2)
            # Handle multi-episode format (e.g., "17E18" or "17-18")
            if 'E' in episode_match or '-' in episode_match:
                # Extract all episode numbers and create a range format
                episode_numbers = re.findall(r'\d+', episode_match)
                if len(episode_numbers) > 1:
                    parsed_data['episode_number'] = f"E{episode_numbers[0]}-E{episode_numbers[-1]}"
                else:
                    parsed_data['episode_number'] = int(episode_numbers[0])
            else:
                parsed_data['episode_number'] = int(episode_match)
        elif se_match.group(3) is not None and se_match.group(4) is not None:
             parsed_data['season_number'] = int(se_match.group(3))
             parsed_data['episode_number'] = int(se_match.group(4))
        elif se_match.group(5) is not None and se_match.group(6) is not None:
             parsed_data['season_number'] = int(se_match.group(5))
             parsed_data['episode_number'] = int(se_match.group(6))
        else:
             logging.warning(f"Regex matched S/E pattern but failed to extract numbers for: {filename}")
             # Decide if this is fatal? Maybe still treat as movie?
             parsed_data['media_type'] = 'movie' # Fallback to movie if numbers aren't extracted
    else:
        parsed_data['media_type'] = 'movie'

    logging.debug(f"Parsed initial data from {filename}: IMDb={parsed_data['imdb_id']}, Type={parsed_data['media_type']}, S={parsed_data['season_number']}, E={parsed_data['episode_number']}")
    return parsed_data


def _run_analysis_thread(symlink_root_path_str, original_root_path_str, task_id):
    """The actual analysis logic, run in a background thread."""
    global analysis_progress

    # --- Create a temporary directory for recovery files ---
    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    temp_recovery_dir = os.path.join(db_content_dir, 'tmp_recovery')
    try:
        os.makedirs(temp_recovery_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"Failed to create temporary recovery directory {temp_recovery_dir}: {e}")
        # Handle error: update progress and exit thread? For now, log and continue, recovery will fail later.
        pass 
    # --- End temporary directory creation ---

    # Generate a unique temporary file path for this task
    recovery_file_path = os.path.join(temp_recovery_dir, f"recovery_{task_id}.jsonl")

    analysis_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing analysis...',
        'total_items_scanned': 0,
        'total_symlinks_processed': 0,
        'total_files_processed': 0,
        'items_found': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'recoverable_items_preview': [], # Keep the preview list
        # 'recoverable_items': [], # REMOVED - Will use file instead
        'recovery_file_path': None, # Will be set on completion
        'complete': False
    }

    def update_progress(**kwargs):
        if task_id in analysis_progress:
            analysis_progress[task_id].update(kwargs)
            # Limit the preview list size
            preview = analysis_progress[task_id]['recoverable_items_preview']
            if len(preview) > 5:
                 analysis_progress[task_id]['recoverable_items_preview'] = preview[:5]
        else:
            logging.warning(f"Task ID {task_id} not found in progress dict during update.")

    # Use a try/finally block to ensure file closure and cleanup logic
    recovery_file = None
    try:
        # Open the recovery file in append mode with UTF-8 encoding
        recovery_file = open(recovery_file_path, 'a', encoding='utf-8')

        symlink_root_path = Path(symlink_root_path_str)
        original_root_path = Path(original_root_path_str) if original_root_path_str else None

        if not symlink_root_path.is_dir():
            raise ValueError('Symlink Root Path must be a valid directory.')
        if original_root_path and not original_root_path.is_dir():
            raise ValueError('Original Root Path must be valid if provided.')

        # Read relevant settings
        symlink_folder_order_str = get_setting('File Management', 'symlink_folder_order', 'type,version,resolution')
        organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)
        
        separate_anime = get_setting('Debug', 'enable_separate_anime_folders', False)
        movies_folder_name = get_setting('Debug', 'movies_folder_name', 'Movies')
        tv_shows_folder_name = get_setting('Debug', 'tv_shows_folder_name', 'TV Shows')
        anime_movies_folder_name = get_setting('Debug', 'anime_movies_folder_name', 'Anime Movies')
        anime_tv_shows_folder_name = get_setting('Debug', 'anime_tv_shows_folder_name', 'Anime TV Shows')

        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        
        folder_order_components = [comp.strip() for comp in symlink_folder_order_str.split(',')]
        component_map = {'type': [], 'resolution': [], 'version': []}

        if organize_by_type:
            if movies_folder_name: component_map['type'].append(movies_folder_name)
            if tv_shows_folder_name: component_map['type'].append(tv_shows_folder_name)
            if separate_anime:
                if anime_movies_folder_name: component_map['type'].append(anime_movies_folder_name)
                if anime_tv_shows_folder_name: component_map['type'].append(anime_tv_shows_folder_name)
        
        if organize_by_resolution:
            # Consistent with original code's typical resolution folder names
            component_map['resolution'] = ["2160p", "1080p", "720p", "SD"] 

        if organize_by_version:
            all_settings = get_all_settings() # Use get_all_settings to fetch nested dict
            scraping_settings = all_settings.get('Scraping', {})
            version_configs = scraping_settings.get('versions', {})
            configured_versions = [str(v).strip('*') for v in version_configs.keys() if str(v).strip('*')]
            if configured_versions:
                component_map['version'] = configured_versions
            else:
                component_map['version'].append("Default")


        # Build paths based on current settings
        paths_to_scan_tuples = [(symlink_root_path, symlink_root_path.name)] # (Path, description_for_logging)

        for component_key in folder_order_components:
            folders_for_this_component_type = component_map.get(component_key, [])
            
            is_component_active_for_path_building = False
            if component_key == 'type' and organize_by_type: is_component_active_for_path_building = True
            elif component_key == 'resolution' and organize_by_resolution: is_component_active_for_path_building = True
            elif component_key == 'version' and organize_by_version: is_component_active_for_path_building = True

            if is_component_active_for_path_building and folders_for_this_component_type:
                current_level_new_paths = []
                for base_path_obj, base_desc_str in paths_to_scan_tuples:
                    for folder_segment_name in folders_for_this_component_type:
                        if folder_segment_name: # Ensure folder_segment_name is not None or empty
                            current_level_new_paths.append(
                                (base_path_obj / folder_segment_name, f"{base_desc_str}/{folder_segment_name}")
                            )
                if current_level_new_paths: 
                    paths_to_scan_tuples = current_level_new_paths

        # Add comprehensive fallback scanning - scan the entire symlink root recursively
        # This ensures we don't miss items that don't match the current settings
        comprehensive_scan_path = (symlink_root_path, f"{symlink_root_path.name} (comprehensive)")
        if comprehensive_scan_path not in paths_to_scan_tuples:
            paths_to_scan_tuples.append(comprehensive_scan_path)
        
        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        update_progress(status='scanning', message='Starting directory scan...')
        
        # Log what paths we're going to scan
        scan_paths_info = [f"{desc}: {path}" for path, desc in paths_to_scan_tuples]
        logging.info(f"Analysis will scan the following paths: {scan_paths_info}")

        for current_search_path, scan_target_name in paths_to_scan_tuples:
            if current_search_path.is_dir():
                update_progress(message=f'Scanning {scan_target_name}...')
                try:
                    # Use rglob to scan recursively within the target directory
                    for item_path in current_search_path.rglob('*'):
                        total_items_scanned += 1
                        if total_items_scanned % 100 == 0: # Update progress periodically
                            update_progress(
                                total_items_scanned=total_items_scanned,
                                total_symlinks_processed=total_symlinks_processed,
                                total_files_processed=total_files_processed,
                                items_found=items_found,
                                parser_errors=parser_errors,
                                metadata_errors=metadata_errors,
                                message=f'Scanned {total_items_scanned} items...'
                                )

                        if item_path.suffix.lower() in ignored_extensions:
                            continue

                        if item_path.is_file() or item_path.is_symlink():
                            # Log some items for debugging (but not too many)
                            if total_items_scanned % 1000 == 0:
                                logging.debug(f"Scanning item: {item_path}")
                            if item_path.is_symlink():
                                total_symlinks_processed += 1
                            else:
                                total_files_processed += 1

                            parsed_data = parse_symlink(item_path)
                            if not parsed_data:
                                parser_errors += 1
                                continue # Skip if initial parse fails

                            # --- Determine original path and filename ---
                            original_path_obj = None
                            if item_path.is_symlink():
                                try:
                                    target_path_str = os.readlink(str(item_path))
                                    if not os.path.isabs(target_path_str):
                                        target_path_str = os.path.abspath(os.path.join(item_path.parent, target_path_str))
                                    original_path_obj = Path(target_path_str)
                                except Exception as e:
                                    parsed_data['original_path_for_symlink'] = f"Error: Cannot read link target ({e})"
                                    parsed_data['original_filename'] = item_path.name
                            elif item_path.is_file():
                                original_path_obj = item_path

                            if original_path_obj and original_path_obj.is_file():
                                parsed_data['original_path_for_symlink'] = str(original_path_obj)
                                parsed_data['original_filename'] = original_path_obj.name
                            elif 'original_path_for_symlink' not in parsed_data:
                                if original_path_obj:
                                        parsed_data['original_path_for_symlink'] = f"Error: Target not a file ({original_path_obj})"
                                else:
                                    parsed_data['original_path_for_symlink'] = "Error: Original path unknown"
                                parsed_data['original_filename'] = item_path.name
                            # --- End original path determination ---

                            # --- Get version ---    
                            filename_for_version = parsed_data.get('original_filename')
                            if filename_for_version:
                                try:
                                    version_raw = parse_filename_for_version(filename_for_version)
                                    parsed_data['version'] = version_raw.strip('*') if version_raw else 'Default'
                                except Exception as e:
                                    parsed_data['version'] = 'Default'
                            else:
                                parsed_data['version'] = 'Default'
                            # --- End version --- 
                                
                            # --- Fetch metadata --- 
                            if parsed_data['imdb_id']:
                                metadata_args = {
                                    'imdb_id': parsed_data['imdb_id'],
                                    'item_media_type': parsed_data.get('media_type') # Re-add based on function signature
                                }
                                # Removed conditional adding of season/episode number
                                # get_metadata likely handles this internally based on imdb_id
                                try:
                                    # Pass the original parsed_data as original_item if needed by get_metadata internal logic
                                    metadata_args['original_item'] = parsed_data 
                                    from metadata.metadata import get_metadata
                                    metadata = get_metadata(**metadata_args)
                                    if metadata:
                                        parsed_data['title'] = metadata.get('title')
                                        parsed_data['year'] = metadata.get('year')
                                        # Update tmdb_id if get_metadata found it
                                        parsed_data['tmdb_id'] = metadata.get('tmdb_id') or parsed_data.get('tmdb_id')
                                        parsed_data['release_date'] = metadata.get('release_date')
                                        if parsed_data['media_type'] == 'episode':
                                            parsed_data['episode_title'] = metadata.get('episode_title')
                                        genres = metadata.get('genres', [])
                                        if isinstance(genres, list):
                                            parsed_data['is_anime'] = any(g.lower() in ['animation', 'anime'] for g in genres)
                                        
                                        items_found += 1
                                        
                                        # Log successful item found
                                        if items_found % 100 == 0:  # Log every 100th item to avoid spam
                                            logging.info(f"Found item #{items_found}: {parsed_data.get('title', 'Unknown')} ({parsed_data.get('imdb_id', 'No IMDb')})")
                                        
                                        # --- Write item to recovery file ---
                                        try:
                                            recovery_file.write(json.dumps(parsed_data) + '\n')
                                        except Exception as write_err:
                                            logging.error(f"Error writing item to recovery file {recovery_file_path}: {write_err}")
                                            # Maybe mark the task as failed? For now, log and continue.
                                            # update_progress(status='error', message=f'Error writing recovery file: {write_err}')
                                        # --- End write item ---

                                        # Update preview list for UI feedback during scan
                                        if len(recoverable_items_preview) < 5:
                                                recoverable_items_preview.append(parsed_data)
                                        
                                        # Update progress (only preview list is stored in memory now)
                                        update_progress(items_found=items_found, recoverable_items_preview=recoverable_items_preview)
                                    else:
                                        metadata_errors += 1
                                except Exception as e:
                                    logging.error(f"Metadata error for {metadata_args}: {e}", exc_info=False) # Less verbose logging
                                    metadata_errors += 1
                        else: # No IMDb ID was parsed
                            parser_errors += 1 # This case should be caught by parse_symlink returning None now
                            # Log parser errors but limit frequency to avoid spam
                            if parser_errors % 50 == 0:  # Log every 50th parser error
                                logging.warning(f"Parser error #{parser_errors}: Skipping {item_path.name} as no IMDb ID was parsed")

                except Exception as e_rglob:
                    logging.error(f"Error during rglob scan of {current_search_path}: {e_rglob}", exc_info=True)
            else:
                logging.warning(f"Directory not found or not accessible: {current_search_path} (derived from order: {symlink_folder_order_str})")
                    
        # Analysis complete, update final status and store recovery file path
        update_progress(
            status='complete',
            message='Analysis finished.',
            complete=True,
            # recoverable_items=analysis_progress[task_id]['recoverable_items'], # REMOVED
            recovery_file_path=recovery_file_path if items_found > 0 else None, # Store file path if items were found
            total_items_scanned=total_items_scanned,
            total_symlinks_processed=total_symlinks_processed,
            total_files_processed=total_files_processed,
            items_found=items_found,
            parser_errors=parser_errors,
            metadata_errors=metadata_errors
        )

    except Exception as e:
        logging.error(f"Analysis thread error for task {task_id}: {e}", exc_info=True)
        update_progress(status='error', message=f'Analysis failed: {e}', complete=True)
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"Error closing recovery file {recovery_file_path}: {close_err}")
        pass


def parse_riven_symlink(symlink_path: Path):
    """Parses a Riven symlink path based on filename patterns, not templates."""
    filename = symlink_path.name
    parsed_data = {
        'symlink_path': str(symlink_path),
        'original_path_for_symlink': None, # Populated in analyze_riven_symlinks
        'media_type': None, # Determined below
        'imdb_id': None, # Determined below
        'tmdb_id': None, # Populated by get_metadata
        'title': None, # Populated by get_metadata
        'year': None, # Populated by get_metadata
        'season_number': None, # Determined below
        'episode_number': None, # Determined below
        'episode_title': None, # Populated by get_metadata
        'version': None, # Populated by reverse_parser in analyze_riven_symlinks
        'original_filename': None, # Populated in analyze_riven_symlinks
        'is_anime': False # Populated by get_metadata
    }

    # Robust S/E matching from filename
    # Also handles multi-episode formats like S11E17-E18 or S11E17E18
    se_filename_match = re.search(r'[Ss](\d{1,2})[EeXx](\d{1,3}(?:[EeXx]\d{1,3})*)|Season\s?(\d{1,2})\s?Episode\s?(\d{1,3})|(\d{1,2})[Xx](\d{1,3})', filename)

    parent_dir_name = symlink_path.parent.name if symlink_path.parent else ""
    season_from_parent_match = re.search(r'[Ss](?:eason)?\s?(\d+)', parent_dir_name)

    # Determine if it's an episode
    if se_filename_match:
        parsed_data['media_type'] = 'episode'
        # Extract S/E from filename groups
        if se_filename_match.group(1) is not None and se_filename_match.group(2) is not None: # SxxExx
            parsed_data['season_number'] = int(se_filename_match.group(1))
            episode_match = se_filename_match.group(2)
            # Handle multi-episode format (e.g., "17E18" or "17-18")
            if 'E' in episode_match or '-' in episode_match:
                # Extract all episode numbers and create a range format
                episode_numbers = re.findall(r'\d+', episode_match)
                if len(episode_numbers) > 1:
                    parsed_data['episode_number'] = f"E{episode_numbers[0]}-E{episode_numbers[-1]}"
                else:
                    parsed_data['episode_number'] = int(episode_numbers[0])
            else:
                parsed_data['episode_number'] = int(episode_match)
        elif se_filename_match.group(3) is not None and se_filename_match.group(4) is not None: # Season xx Episode xx
            parsed_data['season_number'] = int(se_filename_match.group(3))
            parsed_data['episode_number'] = int(se_filename_match.group(4))
        elif se_filename_match.group(5) is not None and se_filename_match.group(6) is not None: # xxXx
            parsed_data['season_number'] = int(se_filename_match.group(5))
            parsed_data['episode_number'] = int(se_filename_match.group(6))
    elif season_from_parent_match: # Season in parent folder, check filename for simple episode number
        parsed_data['media_type'] = 'episode'
        parsed_data['season_number'] = int(season_from_parent_match.group(1))
        # Try to get simple episode number from filename, e.g., "01.mkv", "E01.mkv"
        ep_num_match = re.search(r'(?:[Ee](?:pisode)?)?\s?(\d+)\.[^.]+$', filename) # Matches "01.mkv", "E01.mkv", "episode 01.mkv"
        if ep_num_match:
            ep_val = int(ep_num_match.group(1))
            if 1 <= ep_val <= 200: # Sanity check
                parsed_data['episode_number'] = ep_val
    else:
        parsed_data['media_type'] = 'movie'

    # IMDb ID Extraction
    if parsed_data['media_type'] == 'episode':
        if not (symlink_path.parent and symlink_path.parent.parent):
            logging.warning(f"RIVEN (EPISODE): Path '{symlink_path}' too short for IMDb ID from grandfather directory.")
            return None
        grandfather_dir_name = symlink_path.parent.parent.name
        imdb_match = re.search(r'(tt\d{7,})', grandfather_dir_name, re.IGNORECASE)
        if imdb_match:
            parsed_data['imdb_id'] = imdb_match.group(1)
        else:
            # Try searching in the full path as fallback for episodes
            full_path_str = str(symlink_path)
            imdb_match_full = re.search(r'(tt\d{7,})', full_path_str, re.IGNORECASE)
            if imdb_match_full:
                parsed_data['imdb_id'] = imdb_match_full.group(1)
                logging.info(f"RIVEN (EPISODE): Found IMDb ID in full path: {parsed_data['imdb_id']} for {filename}")
            else:
                logging.warning(f"RIVEN (EPISODE): IMDb ID not found in grandfather directory '{grandfather_dir_name}' or full path for episode file '{symlink_path}'.")
                return None
        
        # Final check for S/E numbers for episodes
        if parsed_data.get('season_number') is None or parsed_data.get('episode_number') is None:
            logging.warning(f"RIVEN (EPISODE): Incomplete S/E numbers for '{symlink_path}'. S={parsed_data.get('season_number')}, E={parsed_data.get('episode_number')}. Filename: '{filename}', Parent: '{parent_dir_name}'.")
            return None

    elif parsed_data['media_type'] == 'movie':
        # Try filename first for movie IMDb
        imdb_match_file = re.search(r'(tt\d{7,})', filename, re.IGNORECASE)
        if imdb_match_file:
            parsed_data['imdb_id'] = imdb_match_file.group(1)
        # If not in filename, try immediate parent directory for movie IMDb
        elif symlink_path.parent:
            imdb_match_parent = re.search(r'(tt\d{7,})', parent_dir_name, re.IGNORECASE)
            if imdb_match_parent:
                parsed_data['imdb_id'] = imdb_match_parent.group(1)
            else:
                # Try searching in the full path as final fallback
                full_path_str = str(symlink_path)
                imdb_match_full = re.search(r'(tt\d{7,})', full_path_str, re.IGNORECASE)
                if imdb_match_full:
                    parsed_data['imdb_id'] = imdb_match_full.group(1)
                    logging.info(f"RIVEN (MOVIE): Found IMDb ID in full path: {parsed_data['imdb_id']} for {filename}")
                else:
                    logging.warning(f"RIVEN (MOVIE): IMDb ID not found in filename '{filename}', parent directory '{parent_dir_name}', or full path for movie file '{symlink_path}'.")
                    return None
        else: # No parent, and not in filename
            # Try searching in the full path as final fallback
            full_path_str = str(symlink_path)
            imdb_match_full = re.search(r'(tt\d{7,})', full_path_str, re.IGNORECASE)
            if imdb_match_full:
                parsed_data['imdb_id'] = imdb_match_full.group(1)
                logging.info(f"RIVEN (MOVIE): Found IMDb ID in full path: {parsed_data['imdb_id']} for {filename}")
            else:
                logging.warning(f"RIVEN (MOVIE): IMDb ID not found in filename '{filename}' or full path for movie file '{symlink_path}'.")
                return None
    else: # Should not happen if media_type is always set
        logging.error(f"RIVEN: media_type not determined for {symlink_path}")
        return None

    # Final check for any IMDb ID
    if not parsed_data.get('imdb_id'):
        logging.warning(f"RIVEN: IMDb ID could not be resolved for path '{symlink_path}'.")
        return None

    logging.debug(f"RIVEN: Parsed initial data from {filename}: IMDb={parsed_data['imdb_id']}, Type={parsed_data['media_type']}, S={parsed_data.get('season_number')}, E={parsed_data.get('episode_number')}")
    return parsed_data


def _run_riven_analysis_thread(symlink_root_path_str, original_root_path_str, task_id):
    """The actual Riven analysis logic, run in a background thread."""
    global riven_analysis_progress

    db_content_dir = os.environ.get('USER_DB_CONTENT', '/user/db_content')
    temp_recovery_dir = os.path.join(db_content_dir, 'tmp_riven_recovery')
    try:
        os.makedirs(temp_recovery_dir, exist_ok=True)
    except OSError as e:
        logging.error(f"RIVEN: Failed to create temporary recovery directory {temp_recovery_dir}: {e}")
        pass 
    
    recovery_file_path = os.path.join(temp_recovery_dir, f"riven_recovery_{task_id}.jsonl")

    riven_analysis_progress[task_id] = {
        'status': 'starting',
        'message': 'Initializing Riven analysis...',
        'total_items_scanned': 0,
        'total_symlinks_processed': 0,
        'total_files_processed': 0,
        'items_found': 0,
        'parser_errors': 0,
        'metadata_errors': 0,
        'recoverable_items_preview': [],
        'recovery_file_path': None,
        'complete': False
    }

    def update_progress(**kwargs):
        if task_id in riven_analysis_progress:
            riven_analysis_progress[task_id].update(kwargs)
            preview = riven_analysis_progress[task_id]['recoverable_items_preview']
            if len(preview) > 5:
                 riven_analysis_progress[task_id]['recoverable_items_preview'] = preview[:5]
        else:
            logging.warning(f"RIVEN: Task ID {task_id} not found in progress dict during update.")

    recovery_file = None
    try:
        recovery_file = open(recovery_file_path, 'a', encoding='utf-8')

        symlink_root_path = Path(symlink_root_path_str)
        original_root_path = Path(original_root_path_str) if original_root_path_str else None

        if not symlink_root_path.is_dir():
            raise ValueError('RIVEN: Symlink Root Path must be a valid directory.')
        if original_root_path and not original_root_path.is_dir():
            raise ValueError('RIVEN: Original Root Path must be valid if provided.')

        symlink_organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        ignored_extensions = {'.srt', '.sub', '.idx', '.nfo', '.txt', '.jpg', '.png', '.db', '.partial', '.!qB'}
        riven_type_folders = ["anime_movies", "anime_shows", "movies", "shows"]
        
        # Define actual resolution subfolder names to check if organization is on
        actual_resolution_subfolder_names = ["2160p", "1080p"] 

        total_items_scanned = 0
        total_symlinks_processed = 0
        total_files_processed = 0
        items_found = 0
        parser_errors = 0
        metadata_errors = 0
        recoverable_items_preview = []

        # --- Nested helper function to process items in a directory ---
        def _scan_riven_directory_recursive(path_to_scan: Path, scan_description: str):
            nonlocal total_items_scanned, total_symlinks_processed, total_files_processed, items_found, parser_errors, metadata_errors, recoverable_items_preview
            
            update_progress(message=f'RIVEN: Scanning {scan_description}...')
            try:
                for item_path in path_to_scan.rglob('*'):
                    total_items_scanned += 1
                    if total_items_scanned % 100 == 0:
                        update_progress(
                            total_items_scanned=total_items_scanned,
                            total_symlinks_processed=total_symlinks_processed,
                            total_files_processed=total_files_processed,
                            items_found=items_found,
                            parser_errors=parser_errors,
                            metadata_errors=metadata_errors,
                            message=f'RIVEN: Scanned {total_items_scanned} items...'
                         )

                    if item_path.suffix.lower() in ignored_extensions:
                        continue

                    if item_path.is_file() or item_path.is_symlink():
                        if item_path.is_symlink():
                            total_symlinks_processed += 1
                        else:
                            total_files_processed += 1

                        parsed_data = parse_riven_symlink(item_path)
                        if not parsed_data:
                            parser_errors += 1
                            continue

                        original_path_obj = None
                        if item_path.is_symlink():
                            try:
                                target_path_str = os.readlink(str(item_path))
                                if not os.path.isabs(target_path_str):
                                    target_path_str = os.path.abspath(os.path.join(item_path.parent, target_path_str))
                                original_path_obj = Path(target_path_str)
                            except Exception as e:
                                logging.error(f"RIVEN: Error reading symlink target for {item_path}: {e}")
                                parsed_data['original_path_for_symlink'] = f"Error: Cannot read link target ({e})"
                                parsed_data['original_filename'] = item_path.name
                        elif item_path.is_file():
                            original_path_obj = item_path

                        if original_path_obj and original_path_obj.is_file():
                            parsed_data['original_path_for_symlink'] = str(original_path_obj)
                            parsed_data['original_filename'] = original_path_obj.name
                        elif 'original_path_for_symlink' not in parsed_data :
                            if original_path_obj:
                                 logging.warning(f"RIVEN: Symlink target {original_path_obj} is not a file for {item_path}.")
                                 parsed_data['original_path_for_symlink'] = f"Error: Target not a file ({original_path_obj})"
                            else:
                                logging.warning(f"RIVEN: Could not determine original file for {item_path}.")
                                parsed_data['original_path_for_symlink'] = "Error: Original path unknown"
                            parsed_data['original_filename'] = item_path.name
                        
                        if not parsed_data.get('original_filename') or "Error:" in str(parsed_data.get('original_path_for_symlink')):
                            parser_errors += 1
                            logging.warning(f"RIVEN: Skipping item {item_path.name} due to missing original file information.")
                            continue
                        
                        filename_for_version = parsed_data.get('original_filename')
                        if filename_for_version:
                            try:
                                version_raw = parse_filename_for_version(filename_for_version)
                                parsed_data['version'] = version_raw.strip('*') if version_raw else 'Default'
                            except Exception as e:
                                parsed_data['version'] = 'Default'
                        else:
                            parsed_data['version'] = 'Default'
                            
                        if parsed_data['imdb_id']:
                            metadata_args = {
                                'imdb_id': parsed_data['imdb_id'],
                                'item_media_type': parsed_data.get('media_type')
                                # season_number and episode_number are not directly used by get_metadata for initial fetch
                            }
                            try:
                                metadata_args['original_item'] = parsed_data 
                                from metadata.metadata import get_metadata
                                # This metadata is likely show-level for episodes
                                metadata = get_metadata(**metadata_args) 

                                if metadata:
                                    # Populate base parsed_data from show-level metadata
                                    parsed_data['title'] = metadata.get('title', parsed_data.get('title')) # Prefer metadata title
                                    parsed_data['year'] = metadata.get('year', parsed_data.get('year'))
                                    # Corrected TMDB ID extraction
                                    parsed_data['tmdb_id'] = str(metadata.get('ids', {}).get('tmdb')).strip() if metadata.get('ids', {}).get('tmdb') else parsed_data.get('tmdb_id')
                                    # Use show's release_date as a fallback if episode-specific one isn't found
                                    parsed_data['release_date'] = metadata.get('release_date') 
                                    genres = metadata.get('genres', []) # Use genres from show metadata
                                    if isinstance(genres, str): # Ensure genres is a list
                                        try: genres = json.loads(genres)
                                        except json.JSONDecodeError: genres = [g.strip() for g in genres.split(',') if g.strip()]
                                    if not isinstance(genres, list): genres = [str(genres)]
                                    parsed_data['is_anime'] = any('anime' in genre.lower() for genre in genres)

                                    # If it's an episode, try to get specific episode title and air date
                                    if parsed_data['media_type'] == 'episode' and parsed_data.get('season_number') is not None and parsed_data.get('episode_number') is not None:
                                        try:
                                            # Use integer keys for lookup
                                            s_num_int = int(parsed_data['season_number'])
                                            e_num_int = int(parsed_data['episode_number'])
                                            
                                            # Fetch full show details to navigate to episode
                                            direct_api = DirectAPI() # Initialize DirectAPI
                                            # The imdb_id in parsed_data should be the show's IMDb ID
                                            full_show_details, _ = direct_api.get_show_metadata(imdb_id=parsed_data['imdb_id']) 
                                            
                                            if full_show_details:
                                                # Access seasons and episodes using integer keys
                                                season_data = full_show_details.get('seasons', {}).get(s_num_int)
                                                if season_data:
                                                    episode_data = season_data.get('episodes', {}).get(e_num_int)
                                                    if episode_data:
                                                        parsed_data['episode_title'] = episode_data.get('title')
                                                        # Prefer episode's air_date if available
                                                        episode_air_date = episode_data.get('first_aired')
                                                        if episode_air_date:
                                                            parsed_data['release_date'] = episode_air_date
                                                        logging.debug(f"RIVEN: Fetched episode title '{parsed_data['episode_title']}' and air_date '{parsed_data['release_date']}' for S{s_num_int}E{e_num_int}")
                                                    else:
                                                        logging.warning(f"RIVEN: Episode S{s_num_int}E{e_num_int} not found in details for {parsed_data['imdb_id']}.")
                                                else:
                                                    logging.warning(f"RIVEN: Season {s_num_int} not found in details for {parsed_data['imdb_id']}.")
                                            else:
                                                logging.warning(f"RIVEN: Could not fetch full show details via DirectAPI for {parsed_data['imdb_id']} to get episode title.")
                                        except ValueError: # Handles case where season_number or episode_number can't be int
                                            logging.error(f"RIVEN: Invalid non-integer season/episode number for {parsed_data['imdb_id']}: S='{parsed_data.get('season_number')}', E='{parsed_data.get('episode_number')}'. Cannot fetch episode details.")
                                        except Exception as ep_fetch_exc:
                                            logging.error(f"RIVEN: Error fetching specific episode details for {parsed_data['imdb_id']} S{parsed_data.get('season_number')}E{parsed_data.get('episode_number')}: {ep_fetch_exc}", exc_info=False)
                                    
                                    # Ensure 'genres' key exists in parsed_data for prospective_db_item
                                    parsed_data['genres'] = genres # Store the list of genres

                                    # --- Calculate new symlink path ---
                                    current_original_filename_with_ext = parsed_data.get('original_filename')
                                    prospective_db_item = {
                                        'title': parsed_data.get('title'), 
                                        'year': parsed_data.get('year'),
                                        'type': parsed_data.get('media_type'), 
                                        'imdb_id': parsed_data.get('imdb_id'),
                                        'tmdb_id': parsed_data.get('tmdb_id'), 
                                        'season_number': parsed_data.get('season_number'),
                                        'episode_number': parsed_data.get('episode_number'), 
                                        'version': parsed_data.get('version', 'Default'),
                                        'is_anime': parsed_data.get('is_anime', False), 
                                        'episode_title': parsed_data.get('episode_title'), # Now this should be populated
                                        'release_date': parsed_data.get('release_date'), # And this might be more specific
                                        'filled_by_file': current_original_filename_with_ext,
                                        'genres': parsed_data.get('genres') # Pass genres to get_symlink_path
                                    }
                                    prospective_db_item_filtered = {k: v for k, v in prospective_db_item.items() if v is not None}

                                    try:
                                        new_symlink_location = get_symlink_path(
                                            prospective_db_item_filtered, # This now contains 'filled_by_file'
                                            parsed_data.get('original_filename'), # This is the second argument 'original_file'
                                            skip_jikan_lookup=True
                                        )
                                        if not new_symlink_location:
                                            raise ValueError("get_symlink_path returned None or empty.")
                                        parsed_data['newly_calculated_symlink_path'] = new_symlink_location
                                    except Exception as e_sym_path:
                                        logging.error(f"RIVEN: Error calculating new symlink path for {parsed_data.get('title')}: {e_sym_path}")
                                        metadata_errors += 1
                                        continue

                                    items_found += 1
                                    try:
                                        recovery_file.write(json.dumps(parsed_data) + '\n')
                                    except Exception as write_err:
                                        logging.error(f"RIVEN: Error writing item to recovery file {recovery_file_path}: {write_err}")
                                    
                                    if len(recoverable_items_preview) < 5:
                                         recoverable_items_preview.append(parsed_data)
                                    update_progress(items_found=items_found, recoverable_items_preview=recoverable_items_preview)
                                else: # metadata fetch failed
                                    metadata_errors += 1
                                    logging.warning(f"RIVEN: Metadata fetch failed for IMDb {parsed_data['imdb_id']} ({item_path.name}).")
                            except Exception as e: # Error during metadata processing block
                                logging.error(f"RIVEN: Metadata processing error for {parsed_data.get('imdb_id', 'Unknown IMDb')} ({item_path.name}): {e}", exc_info=False)
                                metadata_errors += 1
                        else: # No IMDb ID was parsed
                            parser_errors += 1 # This case should be caught by parse_riven_symlink returning None now
                            logging.warning(f"RIVEN: Skipping {item_path.name} as no IMDb ID was parsed (should have been caught earlier).")

            except Exception as e_rglob:
                logging.error(f"RIVEN: Error during rglob scan of {path_to_scan}: {e_rglob}", exc_info=True)
        # --- End of nested helper function ---

        update_progress(status='scanning', message='Starting Riven directory scan...')

        # Iterate through the Riven-specific type folders
        for type_folder_name in riven_type_folders:
            # Path 1: Scan directly within the type folder (e.g., /mnt/zurg-symlinked/movies)
            base_type_path = symlink_root_path / type_folder_name
            if base_type_path.is_dir():
                _scan_riven_directory_recursive(base_type_path, type_folder_name)
            else:
                logging.warning(f"RIVEN: Base directory not found or not accessible: {base_type_path}")

            # Path 2: If resolution organization is enabled, scan within resolution subfolders 
            # (e.g., /mnt/zurg-symlinked/movies/2160p)
            if symlink_organize_by_resolution:
                for res_subfolder_name in actual_resolution_subfolder_names:
                    resolution_specific_path = base_type_path / res_subfolder_name
                    scan_target_description = f'{type_folder_name}/{res_subfolder_name}'
                    if resolution_specific_path.is_dir():
                        _scan_riven_directory_recursive(resolution_specific_path, scan_target_description)
                    else:
                        # This is not necessarily an error, could just be that this resolution isn't used for this type
                        logging.debug(f"RIVEN: Optional resolution directory not found, skipping: {resolution_specific_path}")
                    
        update_progress(
            status='complete',
            message='Riven analysis finished.',
            complete=True,
            recovery_file_path=recovery_file_path if items_found > 0 else None,
            total_items_scanned=total_items_scanned,
            total_symlinks_processed=total_symlinks_processed,
            total_files_processed=total_files_processed,
            items_found=items_found,
            parser_errors=parser_errors,
            metadata_errors=metadata_errors
        )

    except Exception as e:
        logging.error(f"RIVEN: Analysis thread error for task {task_id}: {e}", exc_info=True)
        update_progress(status='error', message=f'RIVEN: Analysis failed: {e}', complete=True)
    finally:
        if recovery_file:
            try:
                recovery_file.close()
            except Exception as close_err:
                 logging.error(f"RIVEN: Error closing recovery file {recovery_file_path}: {close_err}")
        pass
