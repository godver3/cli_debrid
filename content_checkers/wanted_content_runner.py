"""Run a content source by hand: fetch its wanted items and add them to the database.

Used by Debug Functions -> "Get wanted content" and by queues.initialization
for the startup pass over enabled sources.
"""

import logging

from content_checkers.blocked_items import drop_blocked_items
from content_checkers.content_cache_management import (
    load_source_cache, save_source_cache,
    update_cache_for_item,
    normalize_enabled_versions, metadata_output_ids, item_has_metadata_output, UNRESOLVED_RETRY_HOURS
)
from content_checkers.source_run_report import SourceRunReport
from utilities.settings import get_all_settings, get_setting


def async_get_wanted_content(source):
    results = {}
    try:
        if source == 'all':
            # Get all enabled sources
            content_sources = get_all_settings().get('Content Sources', {})
            enabled_sources = {source_id: data for source_id, data in content_sources.items() if data.get('enabled', False)}
            
            total_added = 0
            total_processed = 0
            total_cache_skipped = 0
            total_media_type_skipped = 0
            all_errors = []

            for source_id in enabled_sources:
                logging.info(f"Processing source {source_id} as part of 'all'...")
                result = get_and_add_wanted_content(source_id)
                total_added += result.get('added', 0)
                total_processed += result.get('processed', 0)
                total_cache_skipped += result.get('cache_skipped', 0)
                total_media_type_skipped += result.get('media_type_skipped', 0)
                if result.get('error'):
                    all_errors.append(f"{source_id}: {result['error']}")
            
            message_parts = [f"All Sources: Added {total_added} items"]
            if total_processed > 0: message_parts.append(f"Processed {total_processed}")
            if total_cache_skipped > 0: message_parts.append(f"Skipped {total_cache_skipped} (cache)")
            if total_media_type_skipped > 0: message_parts.append(f"Skipped {total_media_type_skipped} (media type)")
            message = ", ".join(message_parts) + "."
            
            results = {'success': True, 'message': message}
            if all_errors:
                results['error'] = "Errors: " + "; ".join(all_errors)
                results['success'] = False # Mark as failure if any source had errors

        else:
            # Get the display name for the single content source
            content_sources = get_all_settings().get('Content Sources', {})
            source_config = content_sources.get(source, {})
            if isinstance(source_config, dict) and source_config.get('display_name'):
                display_name = source_config['display_name']
            else:
                display_name = ' '.join(word.capitalize() for word in source.split('_'))
            
            # Process the single source
            result = get_and_add_wanted_content(source)
            
            added = result.get('added', 0)
            processed = result.get('processed', 0)
            cache_skipped = result.get('cache_skipped', 0)
            media_type_skipped = result.get('media_type_skipped', 0)
            error = result.get('error')
            
            message_parts = [f"{display_name}: Added {added} items"]
            if processed > 0: message_parts.append(f"Processed {processed}")
            if cache_skipped > 0: message_parts.append(f"Skipped {cache_skipped} (cache)")
            if media_type_skipped > 0: message_parts.append(f"Skipped {media_type_skipped} (media type)")
            message = ", ".join(message_parts) + "."

            results = {'success': error is None, 'message': message}
            if error:
                results['error'] = str(error)
        
        return results
    except Exception as e:
        logging.error(f"Error in async_get_wanted_content for source '{source}': {e}", exc_info=True)
        return {'success': False, 'error': f"Unexpected error processing source {source}: {str(e)}"}


def get_and_add_wanted_content(source_id):
    from content_checkers.overseerr import get_wanted_from_overseerr
    from content_checkers.collected import get_wanted_from_collected
    from content_checkers.plex_watchlist import get_wanted_from_plex_watchlist, get_wanted_from_other_plex_watchlist, get_wanted_from_plex_friends_watchlist
    from content_checkers.plex_rss_watchlist import get_wanted_from_plex_rss, get_wanted_from_friends_plex_rss
    from content_checkers.trakt import get_wanted_from_trakt_lists, get_wanted_from_trakt_watchlist, get_wanted_from_trakt_collection, get_wanted_from_friend_trakt_watchlist, get_wanted_from_special_trakt_lists
    from content_checkers.scrob import get_wanted_from_scrob_lists, get_wanted_from_scrob_collection, get_wanted_from_scrob_special
    from content_checkers.mdb_list import get_wanted_from_mdblist_source
    from content_checkers.adaptive_list import get_wanted_from_adaptive_list
    from content_checkers.content_source_detail import append_content_source_detail
    from metadata.metadata import process_metadata
    from datetime import datetime, timedelta # Add this import

    content_sources = get_all_settings().get('Content Sources', {})
    source_data = content_sources.get(source_id) # Use .get for safety
    if not source_data:
        logging.error(f"Source ID {source_id} not found in settings.")
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Source {source_id} not found"}

    source_type = source_id.split('_')[0]
    versions_from_config = source_data.get('versions', []) # Default to empty list if missing
    source_media_type = source_data.get('media_type', 'All')
    raw_cutoff_date = source_data.get('cutoff_date', '')
    exclude_genres = source_data.get('exclude_genres', []) # Get exclude_genres setting
    try:
        list_length_limit = int(source_data.get('list_length_limit', 0)) # Get list_length_limit setting and convert to int
    except (ValueError, TypeError):
        logging.warning(f"Invalid list_length_limit value for source {source_id}: {source_data.get('list_length_limit')}. Using default value 0.")
        list_length_limit = 0
    unblacklist_on_source_run = bool(source_data.get('unblacklist_on_source_run', False))
    granular_versions = get_setting('Debug', 'enable_granular_version_additions', False)
    logging.info(
        f"Starting content source run: source={source_id}, unblacklist={unblacklist_on_source_run}, "
        f"granular={granular_versions}, bypass_cache=True"
    )
    parsed_cutoff_date = None

    if raw_cutoff_date:
        try:
            # Try to interpret as number of days ago
            days_ago = int(raw_cutoff_date)
            parsed_cutoff_date = (datetime.now() - timedelta(days=days_ago)).date()
            logging.debug(f"Cutoff date for {source_id} set to {days_ago} days ago: {parsed_cutoff_date}")
        except ValueError:
            # If not an int, try to interpret as YYYY-MM-DD
            try:
                parsed_cutoff_date = datetime.strptime(raw_cutoff_date, '%Y-%m-%d').date()
                logging.debug(f"Cutoff date for {source_id} set to specific date: {parsed_cutoff_date}")
            except (ValueError, TypeError):
                logging.warning(f"Invalid cutoff_date format in source {source_id}. Expected YYYY-MM-DD or number of days, got '{raw_cutoff_date}'. No cutoff will be applied.")
                parsed_cutoff_date = None
    
    cutoff_date = parsed_cutoff_date # Use the parsed_cutoff_date

    # Same version handling as run_program.process_content_source: only enabled versions count,
    # and a source with none cannot be processed.
    versions_dict = normalize_enabled_versions(versions_from_config)
    if not versions_dict:
        logging.error(
            f"Content source {source_id} has no enabled versions in current settings. "
            "Skipping this run before metadata processing; select and save at least one version."
        )
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Source {source_id} has no enabled versions"}

    logging.info(f"Processing source: {source_id}")
    logging.debug(f"Source type: {source_type}, media type: {source_media_type}, versions (as dict): {versions_from_config}")
    
    source_cache = load_source_cache(source_id)
    logging.debug(f"Initial cache state for {source_id}: {len(source_cache)} entries")
    cache_skipped = 0
    items_processed = 0
    total_items_added = 0 # Renamed for clarity
    media_type_skipped = 0
    genre_skipped = 0
    cutoff_date_skipped = 0
    report = SourceRunReport(source_id, source_type)  # no-op except for Plex watchlist sources

    wanted_content = []
    try: # Add try block for source fetching
        if source_type == 'Overseerr':
            wanted_content = get_wanted_from_overseerr(versions_from_config)
        elif source_type == 'My Plex Watchlist':
            wanted_content = get_wanted_from_plex_watchlist(versions_from_config)
        elif source_type == 'My Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_plex_rss(plex_rss_url, versions_from_config)
        elif source_type == 'My Friends Plex RSS Watchlist':
            plex_rss_url = source_data.get('url', '')
            if not plex_rss_url:
                logging.error(f"Missing URL for source: {source_id}")
                return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Missing URL for {source_id}"}
            wanted_content = get_wanted_from_friends_plex_rss(plex_rss_url, versions_from_config)
        elif source_type == 'Plex Friends Watchlist':
            wanted_content = get_wanted_from_plex_friends_watchlist(source_data, versions_from_config)
        elif source_type == 'Other Plex Watchlist':
            wanted_content = get_wanted_from_other_plex_watchlist(
                username=source_data.get('username', ''),
                token=source_data.get('token', ''),
                versions=versions_from_config
            )
        elif source_type == 'MDBList':
            wanted_content = get_wanted_from_mdblist_source(source_data, versions_from_config)
        elif source_type == 'Special Trakt Lists':
            wanted_content = get_wanted_from_special_trakt_lists(source_data, versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Trakt Watchlist':
            wanted_content = get_wanted_from_trakt_watchlist(versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Trakt Lists':
            trakt_lists = source_data.get('trakt_lists', '').split(',')
            for trakt_list in trakt_lists:
                trakt_list = trakt_list.strip()
                if trakt_list: # Check if list name is not empty
                    wanted_content.extend(get_wanted_from_trakt_lists(trakt_list, versions_from_config, unblacklist=unblacklist_on_source_run))
        elif source_type == 'Friends Trakt Watchlist':
            wanted_content = get_wanted_from_friend_trakt_watchlist(source_data, versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Trakt Collection':
            wanted_content = get_wanted_from_trakt_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Scrob Lists':
            wanted_content = get_wanted_from_scrob_lists(source_data.get('scrob_list_ids', ''), versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Scrob Collection':
            wanted_content = get_wanted_from_scrob_collection(versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Special Scrob Lists':
            wanted_content = get_wanted_from_scrob_special(source_data, versions_from_config, unblacklist=unblacklist_on_source_run)
        elif source_type == 'Collected':
            wanted_content = get_wanted_from_collected()
        elif source_type == 'Adaptive List':
            # Adaptive List - each content source is one list with filters stored directly
            wanted_content = get_wanted_from_adaptive_list(source_data, versions_from_config)
        else:
            logging.warning(f"Unknown source type: {source_type}")
            # Optionally return an error or empty result here
            return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Unknown source type {source_type}"}

    except Exception as fetch_error:
        logging.error(f"Error fetching content from {source_id}: {fetch_error}", exc_info=True)
        return {'added': 0, 'processed': 0, 'cache_skipped': 0, 'media_type_skipped': 0, 'error': f"Error fetching from {source_id}: {str(fetch_error)}"}

    logging.debug(f"Fetched {len(wanted_content)} raw items for {source_id}")

    # Apply list length limit if set
    if list_length_limit > 0:
        if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
            # For tuple format, limit each batch
            limited_wanted_content = []
            total_items_limited = 0
            for items, item_versions_from_source_tuple in wanted_content:
                if total_items_limited >= list_length_limit:
                    logging.info(f"List length limit reached for {source_id} ({list_length_limit} items), skipping remaining batches")
                    break
                remaining_limit = list_length_limit - total_items_limited
                if len(items) > remaining_limit:
                    items = items[:remaining_limit]
                    logging.info(f"Limited batch for {source_id} to {remaining_limit} items due to list length limit")
                limited_wanted_content.append((items, item_versions_from_source_tuple))
                total_items_limited += len(items)
            wanted_content = limited_wanted_content
            logging.info(f"Applied list length limit to {source_id}: processed {total_items_limited} items (limit: {list_length_limit})")
        else:
            # For single list format, limit the list
            original_length = len(wanted_content)
            if original_length > list_length_limit:
                wanted_content = wanted_content[:list_length_limit]
                logging.info(f"Applied list length limit to {source_id}: limited to {list_length_limit} items from {original_length}")

    report.fetched(wanted_content)
    wanted_content = drop_blocked_items(wanted_content, source_id, source_type, unblacklist_on_source_run, granular_versions, report)
    if not wanted_content:
        report.log()  # fetcher returned nothing: say so explicitly
    if wanted_content:
        try: # Add try block for processing
            if isinstance(wanted_content, list) and len(wanted_content) > 0 and isinstance(wanted_content[0], tuple):
                # Handle list of tuples
                for items, item_versions_from_source_tuple in wanted_content: # Renamed item_versions to avoid conflict
                    batch_items_processed = 0
                    batch_total_items_added = 0
                    batch_cache_skipped = 0
                    batch_media_type_skipped = 0
                    batch_genre_skipped = 0
                    batch_cutoff_date_skipped = 0

                    try:
                        logging.debug(f"Processing batch of {len(items)} items from {source_id}")

                        original_count = len(items)
                        # Note: Media type and genre filtering moved to after metadata processing

                        # Filter by cache
                        items_to_process_raw = list(items)
                        logging.debug(f"Batch {source_id}: Cache bypassed for manual run, {len(items_to_process_raw)} to process")

                        if items_to_process_raw:
                            batch_items_processed += len(items_to_process_raw)
                            
                            # Normalise versions from the fetcher tuple (only enabled ones)
                            versions_to_inject = normalize_enabled_versions(item_versions_from_source_tuple)
                            if not versions_to_inject:
                                logging.warning(f"Unexpected or empty versions in tuple for {source_id}. Using main source versions dict.")
                                versions_to_inject = versions_dict # Fallback to the converted source versions

                            # Inject the CONVERTED versions dictionary into each item
                            items_for_metadata = []
                            for item_dict_raw in items_to_process_raw:
                                item_dict_processed = item_dict_raw.copy()
                                item_dict_processed['versions'] = versions_to_inject # Inject the dict
                                item_dict_processed['content_source'] = source_id  # Needed by metadata.py for per-source allow_specials/seasons_per_show
                                items_for_metadata.append(item_dict_processed)

                            processed_items_meta = process_metadata(items_for_metadata)
                            if not processed_items_meta:
                                report.metadata_failed(items_to_process_raw)
                            if processed_items_meta:
                                all_items_meta_processed_batch = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', []) + processed_items_meta.get('anime', [])
                                output_ids = metadata_output_ids(all_items_meta_processed_batch)
                                report.metadata_done(items_to_process_raw, output_ids, all_items_meta_processed_batch, UNRESOLVED_RETRY_HOURS)
                                for item in all_items_meta_processed_batch:
                                    item['content_source'] = source_id
                                    item = append_content_source_detail(item, source_type=source_type)

                                # Filter by media type after metadata processing
                                # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                                if source_media_type != 'All' and not source_type.startswith('Collected'):
                                    items_filtered_type = []
                                    for item in all_items_meta_processed_batch:
                                        item_media_type = item.get('media_type')
                                        # Check for traditional format OR Adaptive List format
                                        is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                        is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                        if is_movie_match or is_show_match:
                                            items_filtered_type.append(item)
                                        else:
                                            batch_media_type_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")
                                    
                                    all_items_meta_processed_batch = items_filtered_type
                                    if batch_media_type_skipped > 0:
                                        logging.debug(f"Batch {source_id}: Skipped {batch_media_type_skipped} items due to media type mismatch")

                                # Filter by excluded genres after metadata processing
                                if exclude_genres:
                                    items_filtered_genre = []
                                    for item in all_items_meta_processed_batch:
                                        item_genres = item.get('genres', [])
                                        if isinstance(item_genres, str):
                                            # Handle comma-separated string format
                                            item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                        
                                        # Check if any of the item's genres are in the exclude list
                                        excluded_genre_found = any(genre.lower() in [g.lower() for g in exclude_genres] for genre in item_genres)
                                        if not excluded_genre_found:
                                            items_filtered_genre.append(item)
                                        else:
                                            batch_genre_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g in exclude_genres]}")
                                    
                                    all_items_meta_processed_batch = items_filtered_genre
                                    if batch_genre_skipped > 0:
                                        logging.debug(f"Batch {source_id}: Skipped {batch_genre_skipped} items due to excluded genres")

                                final_items_for_db_batch = []
                                current_batch_cutoff_skipped = 0 # Local counter for this batch iteration's date skips
                                if cutoff_date:
                                    for item in all_items_meta_processed_batch:
                                        # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                        if item.get('media_type') == 'movie':
                                            release_date = item.get('theatrical_release_date') or item.get('release_date')
                                        else:
                                            release_date = item.get('release_date')
                                        
                                        if not release_date or release_date.lower() == 'unknown':
                                            # A cutoff is set, so an unknown release date is dropped (as in the scheduled run)
                                            current_batch_cutoff_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                            continue
                                        try:
                                            item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                            if item_date >= cutoff_date:
                                                final_items_for_db_batch.append(item)
                                            else:
                                                current_batch_cutoff_skipped += 1
                                                logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date}")
                                        except ValueError:
                                            current_batch_cutoff_skipped += 1
                                            logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                                else:
                                    # No cutoff date, so all processed items are candidates for DB for this batch
                                    final_items_for_db_batch = all_items_meta_processed_batch
                                
                                batch_cutoff_date_skipped += current_batch_cutoff_skipped # Add to the specific batch counter

                                if current_batch_cutoff_skipped > 0: # Log if items were skipped in this batch
                                    logging.debug(f"Batch {source_id}: Skipped {current_batch_cutoff_skipped} items due to cutoff date (pre-DB add)")
                                
                                report.filters_done(final_items_for_db_batch)
                                if final_items_for_db_batch:
                                    from database import add_wanted_items
                                    added_count = add_wanted_items(final_items_for_db_batch, versions_to_inject or versions_dict, unblacklist=unblacklist_on_source_run)
                                    report.added(len(final_items_for_db_batch), added_count)
                                    batch_total_items_added += added_count or 0
                                    
                                    # Update cache for all items that were processed (regardless of whether they made it through filtering)
                                    # This prevents reprocessing the same items repeatedly
                                    for item_original in items_to_process_raw:
                                        update_cache_for_item(item_original, source_id, source_cache,
                                                              retry_after_hours=None if item_has_metadata_output(item_original, output_ids) else UNRESOLVED_RETRY_HOURS)

                    except Exception as batch_error:
                        logging.error(f"Error processing batch from {source_id}: {str(batch_error)}", exc_info=True)
                        # Continue to next batch

                    # Aggregate results from batch
                    items_processed += batch_items_processed
                    total_items_added += batch_total_items_added
                    cache_skipped += batch_cache_skipped
                    media_type_skipped += batch_media_type_skipped
                    genre_skipped += batch_genre_skipped
                    cutoff_date_skipped += batch_cutoff_date_skipped

            else: # Handle single list of items (assuming this path is less common based on previous logic)
                original_count = len(wanted_content)
                # Note: Media type and genre filtering moved to after metadata processing

                items_to_process_raw = list(wanted_content)
                logging.debug(f"{source_id}: Cache bypassed for manual run, {len(items_to_process_raw)} to process")

                if items_to_process_raw:
                    items_processed += len(items_to_process_raw)

                    # Convert the CONVERTED versions dictionary into each item
                    items_for_metadata = []
                    for item_dict_raw in items_to_process_raw:
                        item_dict_processed = item_dict_raw.copy()
                        # Use the CONVERTED source-level versions_dict here
                        item_dict_processed['versions'] = versions_dict
                        item_dict_processed['content_source'] = source_id  # Needed by metadata.py for per-source allow_specials/seasons_per_show
                        items_for_metadata.append(item_dict_processed)
                        
                    processed_items_meta = process_metadata(items_for_metadata)
                    if not processed_items_meta:
                        report.metadata_failed(items_to_process_raw)
                    if processed_items_meta:
                        all_items_meta_processed_non_batch = processed_items_meta.get('movies', []) + processed_items_meta.get('episodes', []) + processed_items_meta.get('anime', [])
                        output_ids = metadata_output_ids(all_items_meta_processed_non_batch)
                        report.metadata_done(items_to_process_raw, output_ids, all_items_meta_processed_non_batch, UNRESOLVED_RETRY_HOURS)
                        for item in all_items_meta_processed_non_batch:
                            item['content_source'] = source_id
                            item = append_content_source_detail(item, source_type=source_type)

                        # Filter by media type after metadata processing
                        # Handle both traditional format ('Movies'/'Shows') and Adaptive List format ('movie'/'tv')
                        if source_media_type != 'All' and not source_type.startswith('Collected'):
                            items_filtered_type = []
                            for item in all_items_meta_processed_non_batch:
                                item_media_type = item.get('media_type')
                                # Check for traditional format OR Adaptive List format
                                is_movie_match = (source_media_type == 'Movies' or source_media_type == 'movie') and item_media_type == 'movie'
                                is_show_match = (source_media_type == 'Shows' or source_media_type == 'tv') and item_media_type in ['tv', 'episode']
                                if is_movie_match or is_show_match:
                                    items_filtered_type.append(item)
                                else:
                                    media_type_skipped += 1
                                    logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to media type mismatch: {item.get('media_type')} != {source_media_type}")

                            all_items_meta_processed_non_batch = items_filtered_type
                            if media_type_skipped > 0:
                                logging.debug(f"{source_id}: Skipped {media_type_skipped} items due to media type mismatch")

                        # Filter by excluded genres after metadata processing
                        if exclude_genres:
                            items_filtered_genre = []
                            for item in all_items_meta_processed_non_batch:
                                item_genres = item.get('genres', [])
                                if isinstance(item_genres, str):
                                    # Handle comma-separated string format
                                    item_genres = [genre.strip() for genre in item_genres.split(',') if genre.strip()]
                                
                                # Check if any of the item's genres are in the exclude list
                                excluded_genre_found = any(genre in exclude_genres for genre in item_genres)
                                if not excluded_genre_found:
                                    items_filtered_genre.append(item)
                                else:
                                    genre_skipped += 1
                                    logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to excluded genre(s): {[g for g in item_genres if g in exclude_genres]}")
                            
                            all_items_meta_processed_non_batch = items_filtered_genre
                            if genre_skipped > 0:
                                logging.debug(f"{source_id}: Skipped {genre_skipped} items due to excluded genres")

                        # Determine the final list of items to add to the database after date filtering
                        final_items_for_db_non_batch = []
                        current_non_batch_cutoff_skipped = 0 # Local counter for this section's date skips
                        if cutoff_date:
                            for item in all_items_meta_processed_non_batch:
                                # For movies, use theatrical_release_date if available, otherwise fall back to release_date
                                if item.get('media_type') == 'movie':
                                    release_date = item.get('theatrical_release_date') or item.get('release_date')
                                else:
                                    release_date = item.get('release_date')
                                
                                if not release_date or release_date.lower() == 'unknown':
                                    current_non_batch_cutoff_skipped += 1
                                    logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to unknown release date (cutoff date is set)")
                                    continue
                                try:
                                    item_date = datetime.strptime(release_date, '%Y-%m-%d').date()
                                    if item_date >= cutoff_date:
                                        final_items_for_db_non_batch.append(item)
                                    else:
                                        current_non_batch_cutoff_skipped += 1
                                        logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to cutoff date: {release_date} < {cutoff_date} (pre-DB add for non-batch)")
                                except ValueError:
                                    current_non_batch_cutoff_skipped += 1
                                    logging.debug(f"Item {item.get('title', 'Unknown')} skipped due to invalid date format: {release_date} (cutoff date is set)")
                        else:
                            # If no cutoff_date, all items processed from metadata are candidates for DB
                            final_items_for_db_non_batch = all_items_meta_processed_non_batch
                        
                        cutoff_date_skipped += current_non_batch_cutoff_skipped # Add to the main function-wide counter

                        if current_non_batch_cutoff_skipped > 0: # Log if items were skipped by date in this non-batch section
                             logging.debug(f"{source_id}: Skipped {current_non_batch_cutoff_skipped} items due to cutoff date (pre-DB add for non-batch)")

                        # Add only the date-filtered items to the database
                        report.filters_done(final_items_for_db_non_batch)
                        if final_items_for_db_non_batch:
                            from database import add_wanted_items # Already imported at your line 1077
                            added_count = add_wanted_items(final_items_for_db_non_batch, versions_dict, unblacklist=unblacklist_on_source_run)
                            report.added(len(final_items_for_db_non_batch), added_count)
                            total_items_added += added_count or 0
                            
                            # Update cache for all items that were processed (regardless of whether they made it through filtering)
                            # This prevents reprocessing the same items repeatedly
                            for item_original in items_to_process_raw:
                                update_cache_for_item(item_original, source_id, source_cache,
                                                      retry_after_hours=None if item_has_metadata_output(item_original, output_ids) else UNRESOLVED_RETRY_HOURS)

            # Save the updated cache
            save_source_cache(source_id, source_cache)
            logging.debug(f"Final cache state for {source_id}: {len(source_cache)} entries")

            stats_msg = f"Source {source_id}: Added {total_items_added} items"
            if items_processed > 0: stats_msg += f" (Processed {items_processed} items)"
            if cache_skipped > 0: stats_msg += f", Skipped {cache_skipped} (cache)"
            if media_type_skipped > 0: stats_msg += f", Skipped {media_type_skipped} (media type)"
            if genre_skipped > 0: stats_msg += f", Skipped {genre_skipped} (excluded genres)"
            if cutoff_date_skipped > 0: stats_msg += f", Skipped {cutoff_date_skipped} (cutoff date)"
            if list_length_limit > 0: stats_msg += f", list length limited to {list_length_limit}"
            logging.info(stats_msg)
            report.log(cache_skipped=cache_skipped)

        except Exception as process_error:
            logging.error(f"Error processing items from {source_id}: {str(process_error)}", exc_info=True)
            # Return counts accumulated so far, plus the error
            return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped, 'genre_skipped': genre_skipped, 'cutoff_date_skipped': cutoff_date_skipped, 'error': f"Error processing items: {str(process_error)}"}

    else:
        logging.info(f"No wanted content retrieved from {source_id}")

    # Return the final counts
    return {'added': total_items_added, 'processed': items_processed, 'cache_skipped': cache_skipped, 'media_type_skipped': media_type_skipped, 'genre_skipped': genre_skipped, 'cutoff_date_skipped': cutoff_date_skipped}
