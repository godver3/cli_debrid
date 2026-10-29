"""Content source runs: fetching wanted items from each source.

Methods of ProgramRunner (queues/run_program.py), kept here to keep that file manageable.
"""
from utilities.settings import get_all_settings
import logging
from content_checkers.source_run_report import SourceRunReport
from content_checkers.content_cache_management import UNRESOLVED_RETRY_HOURS
from routes.api_tracker import api
from content_checkers.content_source_detail import append_content_source_detail
from content_checkers.blocked_items import drop_blocked_items
from utilities.settings import get_setting
from content_checkers.collected import get_wanted_from_collected
from content_checkers.trakt import get_wanted_from_friend_trakt_watchlist
from content_checkers.plex_rss_watchlist import get_wanted_from_friends_plex_rss
from content_checkers.mdb_list import get_wanted_from_mdblist_source
from content_checkers.overseerr import get_wanted_from_overseerr
from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss
from content_checkers.scrob import get_wanted_from_scrob_collection
from content_checkers.scrob import get_wanted_from_scrob_lists
from content_checkers.scrob import get_wanted_from_scrob_special
from content_checkers.trakt import get_wanted_from_special_trakt_lists
from content_checkers.trakt import get_wanted_from_trakt_collection
from content_checkers.trakt import get_wanted_from_trakt_lists
from content_checkers.trakt import get_wanted_from_trakt_watchlist
from content_checkers.content_cache_management import item_has_metadata_output
from content_checkers.content_cache_management import load_live_content_source_config
from content_checkers.content_cache_management import load_source_cache
from content_checkers.content_cache_management import metadata_output_ids
from content_checkers.content_cache_management import normalize_enabled_versions
from content_checkers.content_cache_management import save_source_cache
from content_checkers.content_cache_management import should_process_item
import traceback
from content_checkers.content_cache_management import update_cache_for_item


class ContentSourceTasks:
    def get_content_sources(self, force_refresh=False):
        if self.content_sources is None or force_refresh:
            settings = get_all_settings()
            self.content_sources = settings.get('Content Sources', {})
            debug_settings = settings.get('Debug', {})
            custom_check_periods = debug_settings.get('content_source_check_period', {})
            
            default_intervals = {
                'Overseerr': 900,
                'MDBList': 900,
                'Collected': 86400,
                'Trakt Watchlist': 900,
                'Trakt Lists': 900,
                'Trakt Collection': 900,
                'My Plex Watchlist': 900,
                'Other Plex Watchlist': 900,
                'My Plex RSS Watchlist': 900,
                'My Friends Plex RSS Watchlist': 900,
                'Plex Friends Watchlist': 900,
                'My Friends Trakt Watchlist': 900,
                'Special Trakt Lists': 900,
                'Scrob Lists': 900,
                'Scrob Collection': 900,
                'Special Scrob Lists': 900,
                'Adaptive List': 900  # TMDB discover-based adaptive lists
            }
            
            log_intervals_message = ["Content source intervals being applied to effective set:"] # Prepare log message
            
            for source, data in self.content_sources.items():
                if isinstance(data, str):
                    data = {'enabled': data.lower() == 'true'}
                
                if not isinstance(data, dict):
                    logging.error(f"Unexpected data type for content source {source}: {type(data)}")
                    continue
                
                source_type = source.split('_')[0]

                # Use custom check period if present, otherwise use default
                custom_interval = custom_check_periods.get(source)
                final_interval = 0 # Initialize
                if custom_interval is not None:
                    try:
                        final_interval = int(float(custom_interval) * 60)
                        data['interval'] = final_interval
                    except ValueError:
                         logging.error(f"Invalid custom interval '{custom_interval}' for source {source}. Using default.")
                         final_interval = int(data.get('interval', default_intervals.get(source_type, 3600)))
                         data['interval'] = final_interval
                else:
                    final_interval = int(data.get('interval', default_intervals.get(source_type, 3600)))
                    data['interval'] = final_interval

                task_name = f'task_{source}_wanted'
                
                # Store the calculated default interval (before any custom overrides)
                calculated_default_interval = final_interval
                
                # Check if a custom interval was already loaded for this task (from user's saved preferences)
                if task_name in self.task_intervals:
                    # A custom interval exists, preserve it instead of overwriting with defaults
                    existing_custom_interval = self.task_intervals[task_name]
                    logging.info(f"Preserving custom interval for content source '{task_name}': {existing_custom_interval}s (would have been {calculated_default_interval}s from source defaults)")
                    # Update final_interval so the log message below shows the preserved value
                    final_interval = existing_custom_interval
                else:
                    # No custom interval exists, use the calculated default interval
                    self.task_intervals[task_name] = final_interval
                
                # Add to original_task_intervals with the calculated default (not the custom value)
                # This ensures that resets will go back to the true calculated default
                if task_name not in self.original_task_intervals:
                    self.original_task_intervals[task_name] = calculated_default_interval

                log_intervals_message.append(f"  {task_name}: {final_interval} seconds")
                
                if isinstance(data.get('enabled'), str):
                    data['enabled'] = data['enabled'].lower() == 'true'
                
                # Add content source tasks by default; toggles will disable later if set to False
                if task_name not in self.enabled_tasks:
                    self.enabled_tasks.add(task_name)
                    logging.info(f"Enabled content source task by default (ignoring config flag): {task_name}")
                # Do not remove here based on config; rely solely on saved toggles applied later

            # Log the intervals once after processing all sources
            logging.info("\n".join(log_intervals_message))

        return self.content_sources

    def process_content_source(self, source, data, bypass_cache=False):
        from datetime import datetime, timedelta # Add this import
        source_type = source.split('_')[0]

        # Scheduled and manual APScheduler jobs retain the arguments they were
        # created with. Reload the source here so version changes made in the UI
        # are honored even when a job was registered before the settings save.
        live_source_data = load_live_content_source_config(source)
        if live_source_data is None:
            logging.warning(f"Content source {source} no longer exists in current settings. Skipping stale scheduled task.")
            return
        if live_source_data != data:
            logging.debug(f"Reloaded current configuration for content source {source} before processing.")
        data = live_source_data

        versions_from_config = data.get('versions', []) # Default to empty list if missing
        source_media_type = data.get('media_type', 'All')
        raw_cutoff_date = data.get('cutoff_date', '')
        exclude_genres = data.get('exclude_genres', []) # Get exclude_genres setting
        try:
            list_length_limit = int(data.get('list_length_limit', 0)) # Get list_length_limit setting and convert to int
        except (ValueError, TypeError):
            logging.warning(f"Invalid list_length_limit value for source {source}: {data.get('list_length_limit')}. Using default value 0.")
            list_length_limit = 0
        unblacklist_on_source_run = bool(data.get('unblacklist_on_source_run', False))
        granular_versions = get_setting('Debug', 'enable_granular_version_additions', False)
        logging.info(
            f"Starting content source run: source={source}, unblacklist={unblacklist_on_source_run}, "
            f"granular={granular_versions}, bypass_cache={bypass_cache}"
        )
        parsed_cutoff_date = None

        if raw_cutoff_date:
            try:
                # Try to interpret as number of days ago
                days_ago = int(raw_cutoff_date)
                parsed_cutoff_date = (datetime.now() - timedelta(days=days_ago)).date()
                logging.debug(f"Cutoff date for {source} set to {days_ago} days ago: {parsed_cutoff_date}")
            except ValueError:
                # If not an int, try to interpret as YYYY-MM-DD
                try:
                    parsed_cutoff_date = datetime.strptime(raw_cutoff_date, '%Y-%m-%d').date()
                    logging.debug(f"Cutoff date for {source} set to specific date: {parsed_cutoff_date}")
                except (ValueError, TypeError):
                    logging.warning(f"Invalid cutoff_date format for source {source}. Expected YYYY-MM-DD or number of days, got '{raw_cutoff_date}'. No cutoff will be applied.")
                    parsed_cutoff_date = None
        
        cutoff_date = parsed_cutoff_date # Use the parsed_cutoff_date

        # Convert versions_from_config to the expected dictionary format
        versions_dict = normalize_enabled_versions(versions_from_config)
        if not isinstance(versions_from_config, (list, tuple, set, dict)):
            logging.warning(f"Unexpected format for versions in source {source} (type: {type(versions_from_config)}). Defaulting to empty versions.")

        if not versions_dict:
            logging.error(
                f"Content source {source} has no enabled versions in current settings. "
                "Skipping this run before metadata processing; select and save at least one version."
            )
            return

        logging.debug(f"Processing content source: {source} (type: {source_type}, media_type: {source_media_type}, versions (as dict): {versions_dict})")

        try:
            # Load cache for this source
            source_cache = load_source_cache(source)
            logging.debug(f"Initial cache state for {source}: {len(source_cache)} entries")
            cache_skipped = 0
            items_processed = 0
            total_items = 0
            report = SourceRunReport(source, source_type)  # no-op except for Plex watchlist sources
            media_type_skipped = 0
            genre_skipped = 0
            cutoff_date_skipped = 0
            list_length_limited = 0

            wanted_content = []
            # Pass the original versions_from_config to fetchers, assuming they expect list/dict as per config
            if source_type == 'Overseerr':
                wanted_content = get_wanted_from_overseerr(versions_from_config)
            elif source_type == 'MDBList':
                wanted_content = get_wanted_from_mdblist_source(data, versions_from_config)
            elif source_type == 'Trakt Watchlist':
                try:
                    wanted_content = get_wanted_from_trakt_watchlist(versions_from_config, unblacklist=unblacklist_on_source_run)
                except (ValueError, api.exceptions.RequestException) as e:
                    logging.error(f"Failed to fetch Trakt watchlist: {str(e)}")
                    return
            elif source_type == 'Trakt Lists':
                trakt_lists = data.get('trakt_lists', '').split(',')
                for trakt_list in trakt_lists:
                    trakt_list = trakt_list.strip()
                    if trakt_list: # Ensure not empty
                        try:
                            wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions_from_config, unblacklist=unblacklist_on_source_run))
                        except (ValueError, api.exceptions.RequestException) as e:
                            logging.error(f"Failed to fetch Trakt list {trakt_list}: {str(e)}")
                            continue
            elif source_type == 'Trakt Collection':
                wanted_content = get_wanted_from_trakt_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Friends Trakt Watchlist':
                # This function takes data (source_config) and versions
                wanted_content = get_wanted_from_friend_trakt_watchlist(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Special Trakt Lists': # New elif block
                # 'data' is the source_config, 'versions_dict' is the resolved simple versions map
                wanted_content = get_wanted_from_special_trakt_lists(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Scrob Lists':
                wanted_content = get_wanted_from_scrob_lists(data.get('scrob_list_ids', ''), versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Scrob Collection':
                wanted_content = get_wanted_from_scrob_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Special Scrob Lists':
                wanted_content = get_wanted_from_scrob_special(data, versions_from_config, unblacklist=unblacklist_on_source_run)
            elif source_type == 'Collected':
                wanted_content = get_wanted_from_collected() # Doesn't take versions arg
            elif source_type == 'My Plex Watchlist':
                from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist
                wanted_content = get_wanted_from_plex_watchlist(versions_from_config)
            elif source_type == 'My Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions_from_config)
            elif source_type == 'My Friends Plex RSS Watchlist':
                plex_rss_url = data.get('url', '')
                wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions_from_config)
            elif source_type == 'Plex Friends Watchlist':
                from content_checkers.plex_watchlist import get_wanted_from_plex_friends_watchlist
                wanted_content = get_wanted_from_plex_friends_watchlist(data, versions_from_config)
            elif source_type == 'Other Plex Watchlist':
                # Import the function here
                from content_checkers.plex_watchlist import get_wanted_from_other_plex_watchlist

                # Process only the specific source being called, not all Other Plex Watchlist sources
                # The 'data' parameter contains the configuration for this specific source
                username = data.get('username', '')
                token = data.get('token', '')

                if username and token:
                    try:
                        # Fetch watchlist for this specific user only
                        wanted_content = get_wanted_from_other_plex_watchlist(
                            username=username,
                            token=token,
                            versions=versions_from_config
                        )
                        logging.info(f"Successfully fetched watchlist for {username} from source {source}")
                    except Exception as e:
                        logging.error(f"Failed to fetch Other Plex watchlist for {username} (source: {source}): {str(e)}")
                        wanted_content = [([], versions_from_config)]
                else:
                    logging.warning(f"Other Plex Watchlist source {source} is missing username or token")
                    wanted_content = [([], versions_from_config)]
            elif source_type == 'Adaptive List':
                from content_checkers.adaptive_list import get_wanted_from_adaptive_list
                # Get the list configurations from the source data
                wanted_content = get_wanted_from_adaptive_list(data, versions_from_config)
            else:
                logging.warning(f"Unknown source type: {source_type}")
                return

            if not wanted_content:
                report.log()  # fetcher returned nothing: say so explicitly
            if wanted_content:
                # Apply list length limit if set
                if list_length_limit > 0:
                    if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                        # For tuple format, limit each batch
                        limited_wanted_content = []
                        total_items_limited = 0
                        for items, item_versions_from_source_tuple in wanted_content:
                            if total_items_limited >= list_length_limit:
                                logging.info(f"List length limit reached for {source} ({list_length_limit} items), skipping remaining batches")
                                break
                            remaining_limit = list_length_limit - total_items_limited
                            if len(items) > remaining_limit:
                                items = items[:remaining_limit]
                                logging.info(f"Limited batch for {source} to {remaining_limit} items due to list length limit")
                            limited_wanted_content.append((items, item_versions_from_source_tuple))
                            total_items_limited += len(items)
                        wanted_content = limited_wanted_content
                        logging.info(f"Applied list length limit to {source}: processed {total_items_limited} items (limit: {list_length_limit})")
                    else:
                        # For single list format, limit the list
                        original_length = len(wanted_content)
                        if original_length > list_length_limit:
                            wanted_content = wanted_content[:list_length_limit]
                            logging.info(f"Applied list length limit to {source}: limited to {list_length_limit} items from {original_length}")
                
                report.fetched(wanted_content)
                wanted_content = drop_blocked_items(wanted_content, source, source_type, unblacklist_on_source_run, granular_versions, report)
                if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                    # Handle list of tuples
                    for items, item_versions_from_source_tuple in wanted_content:
                        logging.debug(f"Processing batch of {len(items)} items from {source}")

                        # Normalise versions from the fetcher tuple (only enabled ones). A dict such as
                        # {'1080p': False} used to be injected as-is, giving items no enabled version.
                        versions_to_inject = normalize_enabled_versions(item_versions_from_source_tuple)
                        if not versions_to_inject:
                            logging.warning(f"Unexpected or empty versions in tuple for {source}. Using main source versions dict.")
                            versions_to_inject = versions_dict # Fallback to the converted source versions

                        # Track genre filtering stats for this batch
                        batch_genre_skipped = 0

                        # Note: Media type and genre filtering moved to after metadata processing
                        
                        # Then filter items based on cache
                        items_to_process_raw = [
                            item for item in items
                            if bypass_cache or should_process_item(item, source, source_cache)
                        ]
                        items_skipped = len(items) - len(items_to_process_raw)
                        cache_skipped += items_skipped
                        
                        if items_to_process_raw:
                            # Inject CONVERTED versions into each item before metadata processing
                            items_to_process = []
                            for item_dict_raw in items_to_process_raw:
                                item_dict_processed = item_dict_raw.copy()
                                item_dict_processed['versions'] = versions_to_inject # Use the converted dict
                                item_dict_processed['content_source'] = source  # Needed by metadata.py for seasons_per_show lookup
                                items_to_process.append(item_dict_processed)

                            from metadata.metadata import process_metadata
                            processed_items = process_metadata(items_to_process)
                            if not processed_items:
                                report.metadata_failed(items_to_process_raw)
                            if processed_items:
                                all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                                output_ids = metadata_output_ids(all_items)  # before filters: which raw items produced anything
                                report.metadata_done(items_to_process_raw, output_ids, all_items, UNRESOLVED_RETRY_HOURS)
                                
                                # Set content source and detail for each item
                                for item in all_items:
                                    item['content_source'] = source
                                    item = append_content_source_detail(item, source_type=source_type)
                                
                                # Filter by media type after metadata processing
                                # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                                if source_media_type != 'All' and not source_type.startswith('Collected'):
                                    items_filtered_type = []
                                    for item in all_items:
                                        item_media_type = item.get('media_type')
                                        # Check for traditional format OR Adaptive List format
                                        is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                        is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                        if is_movie_match or is_show_match:
                                            items_filtered_type.append(item)
                                        else:
                                            media_type_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")

                                    all_items = items_filtered_type
                                    if media_type_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {media_type_skipped} items due to media type mismatch")

                                # Filter by excluded genres after metadata processing
                                if exclude_genres:
                                    items_filtered_genre = []
                                    for item in all_items:
                                        item_genres = item.get('genres', [])
                                        if isinstance(item_genres, str):
                                            # Handle comma-separated string format
                                            item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                        
                                        # Check if any of the item's genres are in the exclude list (case-insensitive)
                                        _excl_lower = [g.lower() for g in exclude_genres]
                                        excluded_genre_found = any(genre.lower() in _excl_lower for genre in item_genres)
                                        if not excluded_genre_found:
                                            items_filtered_genre.append(item)
                                        else:
                                            batch_genre_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g.lower() in _excl_lower]}")
                                    
                                    all_items = items_filtered_genre
                                    if batch_genre_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {batch_genre_skipped} items due to excluded genres")
                                
                                # Filter by cutoff date after metadata processing
                                if cutoff_date:
                                    items_filtered_date = []
                                    for item in all_items:
                                        # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                        if item.get('media_type') == 'movie':
                                            release_date = item.get('theatrical_release_date') or item.get('release_date')
                                        else:
                                            release_date = item.get('release_date')
                                        
                                        if not release_date or release_date.lower() == 'unknown':
                                            # Skip items with unknown release dates when cutoff date is set
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                            continue
                                        try:
                                            item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                            if item_date >= cutoff_date:
                                                items_filtered_date.append(item)
                                            else:
                                                cutoff_date_skipped += 1
                                                logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                        except ValueError:
                                            # If we can't parse the date, skip the item when cutoff date is set
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                                    all_items = items_filtered_date
                                    if cutoff_date_skipped > 0:
                                        logging.debug(f"Batch {source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                                from database import add_wanted_items
                                # Pass the CONVERTED versions dict to add_wanted_items
                                report.filters_done(all_items)
                                report.added(len(all_items), add_wanted_items(all_items, versions_to_inject or versions_dict, unblacklist=unblacklist_on_source_run))
                                
                                # Update cache for all items that were processed (regardless of whether they made it through filtering)
                                # This prevents reprocessing the same items repeatedly
                                for item_raw in items_to_process_raw:
                                    resolved = item_has_metadata_output(item_raw, output_ids)
                                    update_cache_for_item(item_raw, source, source_cache,
                                                          retry_after_hours=None if resolved else UNRESOLVED_RETRY_HOURS)
                                
                                total_items += len(all_items)
                                items_processed += len(items_to_process)
                                genre_skipped += batch_genre_skipped
                else:
                    # Handle single list of items
                    logging.debug(f"Processing batch of {len(wanted_content)} items from {source}")
                    
                    # Note: Media type and genre filtering moved to after metadata processing
                    
                    # Then filter items based on cache
                    items_to_process_raw = [
                        item for item in wanted_content
                        if bypass_cache or should_process_item(item, source, source_cache)
                    ]
                    items_skipped = len(wanted_content) - len(items_to_process_raw)
                    cache_skipped += items_skipped
                    
                    if items_to_process_raw:
                        # Inject CONVERTED versions into each item before metadata processing
                        items_to_process = []
                        for item_dict_raw in items_to_process_raw:
                            item_dict_processed = item_dict_raw.copy()
                            # Use the CONVERTED source-level versions_dict here
                            item_dict_processed['versions'] = versions_dict
                            item_dict_processed['content_source'] = source  # Needed by metadata.py for seasons_per_show lookup
                            items_to_process.append(item_dict_processed)

                        from metadata.metadata import process_metadata
                        processed_items = process_metadata(items_to_process)
                        if not processed_items:
                            report.metadata_failed(items_to_process_raw)
                        if processed_items:
                            all_items = processed_items.get('movies', []) + processed_items.get('episodes', []) + processed_items.get('anime', [])
                            output_ids = metadata_output_ids(all_items)  # before filters: which raw items produced anything
                            report.metadata_done(items_to_process_raw, output_ids, all_items, UNRESOLVED_RETRY_HOURS)
                            
                            # Set content source and detail for each item
                            for item in all_items:
                                item['content_source'] = source
                                item = append_content_source_detail(item, source_type=source_type)
                            
                            # Filter by media type after metadata processing
                            # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                            if source_media_type != 'All' and not source_type.startswith('Collected'):
                                items_filtered_type = []
                                for item in all_items:
                                    item_media_type = item.get('media_type')
                                    # Check for traditional format OR Adaptive List format
                                    is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                    is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                    if is_movie_match or is_show_match:
                                        items_filtered_type.append(item)
                                    else:
                                        media_type_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")

                                all_items = items_filtered_type
                                if media_type_skipped > 0:
                                    logging.debug(f"{source}: Skipped {media_type_skipped} items due to media type mismatch")

                            # Filter by excluded genres after metadata processing
                            if exclude_genres:
                                items_filtered_genre = []
                                for item in all_items:
                                    item_genres = item.get('genres', [])
                                    if isinstance(item_genres, str):
                                        # Handle comma-separated string format
                                        item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                    
                                    # Check if any of the item's genres are in the exclude list (case-insensitive)
                                    _excl_lower = [g.lower() for g in exclude_genres]
                                    excluded_genre_found = any(genre.lower() in _excl_lower for genre in item_genres)
                                    if not excluded_genre_found:
                                        items_filtered_genre.append(item)
                                    else:
                                        genre_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g.lower() in _excl_lower]}")
                                
                                all_items = items_filtered_genre
                                if genre_skipped > 0:
                                    logging.debug(f"{source}: Skipped {genre_skipped} items due to excluded genres")
                            
                            # Filter by cutoff date after metadata processing
                            if cutoff_date:
                                items_filtered_date = []
                                for item in all_items:
                                    # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                    if item.get('media_type') == 'movie':
                                        release_date = item.get('theatrical_release_date') or item.get('release_date')
                                    else:
                                        release_date = item.get('release_date')
                                    
                                    if not release_date or release_date.lower() == 'unknown':
                                        # Skip items with unknown release dates when cutoff date is set
                                        cutoff_date_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                        continue
                                    try:
                                        item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                        if item_date >= cutoff_date:
                                            items_filtered_date.append(item)
                                        else:
                                            cutoff_date_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                    except ValueError:
                                        # If we can't parse the date, skip the item when cutoff date is set
                                        cutoff_date_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                                all_items = items_filtered_date
                                if cutoff_date_skipped > 0:
                                    logging.debug(f"{source}: Skipped {cutoff_date_skipped} items due to cutoff date")

                            from database import add_wanted_items
                            # Pass the CONVERTED versions_dict to add_wanted_items
                            report.filters_done(all_items)
                            report.added(len(all_items), add_wanted_items(all_items, versions_dict, unblacklist=unblacklist_on_source_run))
                            
                            # Update cache for all items that were processed (regardless of whether they made it through filtering)
                            # This prevents reprocessing the same items repeatedly
                            for item_raw in items_to_process_raw:
                                resolved = item_has_metadata_output(item_raw, output_ids)
                                update_cache_for_item(item_raw, source, source_cache,
                                                      retry_after_hours=None if resolved else UNRESOLVED_RETRY_HOURS)
                            
                            total_items += len(all_items)
                            items_processed += len(items_to_process)
                
                # Save the updated cache
                save_source_cache(source, source_cache)
                logging.debug(f"Final cache state for {source}: {len(source_cache)} entries")
                
                stats_msg = f"Added {total_items} wanted items from {source} (processed {items_processed} items"
                if cache_skipped > 0:
                    stats_msg += f", skipped {cache_skipped} cached items"
                if media_type_skipped > 0:
                    stats_msg += f", skipped {media_type_skipped} items due to media type mismatch"
                if genre_skipped > 0:
                    stats_msg += f", skipped {genre_skipped} items due to excluded genres"
                if cutoff_date_skipped > 0:
                    stats_msg += f", skipped {cutoff_date_skipped} items due to cutoff date"
                if list_length_limit > 0:
                    stats_msg += f", list length limited to {list_length_limit}"
                stats_msg += ")"
                logging.info(stats_msg)
                report.log(cache_skipped=cache_skipped)

            # ── Plex Collection sync — runs even when all items are cached ────
            # Use config 'type' field for matching — source_type is split on '_' which
            # truncates multi-word types like 'Trakt Lists' to just 'Trakt'
            _source_config_type = data.get('type', source_type)
            if _source_config_type in ('MDBList', 'Trakt Lists', 'Scrob Lists', 'Adaptive List'):
                # Always read live settings — cached source_data may be stale if plex_collection
                # was enabled after startup without a full restart
                try:
                    _live_source_data = get_all_settings().get('Content Sources', {}).get(source, {})
                    _plex_coll_cfg = _live_source_data.get('plex_collection', {})
                except Exception:
                    _plex_coll_cfg = data.get('plex_collection', {})
                if not isinstance(_plex_coll_cfg, dict):
                    _plex_coll_cfg = {}
                if _plex_coll_cfg.get('enabled', False):
                    try:
                        # Extract ordered (imdb_id, tmdb_id) pairs from the raw fetched list
                        # Some sources (Adaptive List) may return None imdb_id with tmdb_id fallback
                        _raw_pairs = []
                        _plex_media_type = data.get('media_type', 'All')
                        if isinstance(wanted_content, list) and wanted_content:
                            if isinstance(wanted_content[0], tuple):
                                for _batch, _ in wanted_content:
                                    for _item in _batch:
                                        _item_mt = _item.get('media_type', '')
                                        if _plex_media_type not in ('All', '') and _item_mt:
                                            if _plex_media_type in ('Movies', 'movie') and _item_mt != 'movie':
                                                continue
                                            if _plex_media_type in ('Shows', 'tv') and _item_mt not in ('tv', 'episode'):
                                                continue
                                        _raw_pairs.append((_item.get('imdb_id'), _item.get('tmdb_id')))
                            else:
                                for _item in wanted_content:
                                    _item_mt = _item.get('media_type', '')
                                    if _plex_media_type not in ('All', '') and _item_mt:
                                        if _plex_media_type in ('Movies', 'movie') and _item_mt != 'movie':
                                            continue
                                        if _plex_media_type in ('Shows', 'tv') and _item_mt not in ('tv', 'episode'):
                                            continue
                                    _raw_pairs.append((_item.get('imdb_id'), _item.get('tmdb_id')))
                        # Deduplicate preserving order, using imdb_id as key when available else tmdb_id
                        _seen_keys = set()
                        ordered_pairs = []
                        for _imdb, _tmdb in _raw_pairs:
                            _key = _imdb or _tmdb
                            if _key and _key not in _seen_keys:
                                _seen_keys.add(_key)
                                ordered_pairs.append((_imdb, _tmdb))
                        ordered_ids = [p[0] for p in ordered_pairs if p[0]]  # IMDb IDs for fingerprint

                        from database.plex_collections import check_and_sync_if_needed
                        import threading as _threading
                        _t = _threading.Thread(
                            target=check_and_sync_if_needed,
                            args=(source, data),
                            kwargs={'ordered_imdb_ids': ordered_ids, 'ordered_pairs': ordered_pairs},
                            daemon=True,
                            name=f'plex-coll-{source}'
                        )
                        _t.start()
                        logging.info(f"[PlexCollections] Sync thread started for {source} ({len(ordered_pairs)} items in list)")
                        try:
                            from database.plex_collections import refresh_saved_person_collections
                            refresh_saved_person_collections()
                        except Exception as _pce_person:
                            logging.debug(f"[PlexCollections] Person collection refresh skipped: {_pce_person}")
                    except Exception as _pce:
                        logging.error(f"[PlexCollections] Failed to start sync for {source}: {_pce}")
            else:
                if not wanted_content:
                    logging.warning(f"No wanted content retrieved from {source}")



        except Exception as e:
            logging.error(f"Error processing content source {source}: {str(e)}")
            logging.error(traceback.format_exc())
