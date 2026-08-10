import logging
import json
import os
from typing import Dict, Any
from debrid import get_debrid_provider
from queues.adding_queue import AddingQueue
from database.not_wanted_magnets import add_to_not_wanted, add_to_not_wanted_urls
from debrid.common import extract_hash_from_magnet, download_and_extract_hash
from .torrent_processor import TorrentProcessor
from .media_matcher import MediaMatcher

class PendingUncachedQueue:
    def __init__(self):
        self.items = []
        self.debrid_provider = get_debrid_provider()  # May be None for usenet-only setups
        self.adding_queue = AddingQueue()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        self.media_matcher = MediaMatcher()

    def update(self):
        from database import get_all_media_items
        self.items = [dict(row) for row in get_all_media_items(state="Pending Uncached")]
        self._deserialize_scrape_results()

    def _deserialize_scrape_results(self):
        for item in self.items:
            if 'scrape_results' in item:
                try:
                    if isinstance(item['scrape_results'], str):
                        item['scrape_results'] = json.loads(item['scrape_results'])
                    elif isinstance(item['scrape_results'], list) and all(isinstance(x, str) for x in item['scrape_results']):
                        joined_string = ''.join(item['scrape_results'])
                        item['scrape_results'] = json.loads(joined_string)
                except json.JSONDecodeError:
                    logging.error(f"Failed to parse scrape_results for item {item.get('id')}")
                    item['scrape_results'] = []

    def get_contents(self):
        return self.items

    def add_item(self, item: Dict[str, Any]):
        self.items.append(item)

    def remove_item(self, item: Dict[str, Any]):
        self.items = [i for i in self.items if i['id'] != item['id']]

    def process(self, queue_manager):
        if self.debrid_provider is None:
            return
        logging.debug(f"Processing pending uncached queue. Items: {len(self.items)}")

        try:
            active_downloads, download_limit = self.debrid_provider.get_active_downloads()
        except Exception:
            return
        
        if active_downloads >= download_limit:
            logging.info("Download limit reached. Stopping pending uncached queue processing.")
            return

        for item in self.items:
            item_identifier = queue_manager.generate_identifier(item)
            logging.info(f"Processing pending uncached item: {item_identifier}")
            
            link = item.get('filled_by_magnet')
            if not link:
                logging.error(f"No magnet link found for {item_identifier}")
                continue

            # Process the magnet link first
            processed_magnet, temp_file = self.torrent_processor.process_torrent(link)
            if not processed_magnet and not temp_file:
                logging.error(f"Failed to process magnet link for {item_identifier}")
                self._handle_failed_add(item, queue_manager)
                continue

            # Try to add the processed magnet first
            add_result = self.debrid_provider.add_torrent(
                magnet_link=processed_magnet if processed_magnet else None,
                temp_file_path=temp_file if temp_file else None
            )

            # Add to not wanted after successful addition
            try:
                if link.startswith('http'):
                    logging.debug(f"Adding HTTP link to not wanted: {link}")
                    hash_value = extract_hash_from_magnet(processed_magnet) if processed_magnet else download_and_extract_hash(link)
                    add_to_not_wanted(hash_value)
                    add_to_not_wanted_urls(link)
                    logging.info(f"Added magnet hash {hash_value} and URL to not wanted lists")
                else:
                    hash_value = extract_hash_from_magnet(link)
                    add_to_not_wanted(hash_value)
                    logging.info(f"Added magnet hash {hash_value} to not wanted list")
            except Exception as e:
                logging.error(f"Failed to add to not wanted lists: {str(e)}")

            # Clean up temp file if it exists
            if temp_file:
                try:
                    os.unlink(temp_file)
                except Exception as e:
                    logging.error(f"Failed to clean up temp file: {str(e)}")

            if add_result:
                self._handle_successful_add(item, queue_manager, add_result)
                
                # Check download limit after each successful add
                active_downloads, download_limit = self.debrid_provider.get_active_downloads()
                if active_downloads >= download_limit:
                    logging.info("Download limit reached during processing. Stopping.")
                    break
            else:
                self._handle_failed_add(item, queue_manager)

        logging.debug(f"Pending uncached queue processing complete. Remaining items: {len(self.items)}")

    def _handle_successful_add(self, item: Dict[str, Any], queue_manager, torrent_id):
        item_identifier = queue_manager.generate_identifier(item)
        logging.info(f"Successfully added pending uncached item: {item_identifier}")
        
        # Get the torrent info from the debrid provider
        torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
        if not torrent_info or 'files' not in torrent_info:
            logging.error(f"Failed to get torrent info or files for {item_identifier} with torrent_id {torrent_id}")
            self._handle_failed_add(item, queue_manager)
            return

        files = torrent_info['files']
        
        # Parse torrent files using MediaMatcher's _parse_file_info
        # Note: _parse_file_info is an internal method. Ideally, MediaMatcher would expose a public parser.
        parsed_torrent_files = [
            parsed_file for f in files
            if (parsed_file := self.media_matcher._parse_file_info(f)) is not None
        ]

        if not parsed_torrent_files:
            logging.debug(f"No valid video files found in torrent for {item_identifier} after parsing.")
            self._handle_failed_add(item, queue_manager)
            return

        # Match content using MediaMatcher's find_best_match_from_parsed
        # find_best_match_from_parsed expects parsed_files, item. No XEM mapping passed here.
        match_result = self.media_matcher.find_best_match_from_parsed(parsed_torrent_files, item)
            
        if not match_result:
            logging.debug(f"No matching file found in torrent for {item_identifier} using find_best_match_from_parsed.")
            self._handle_failed_add(item, queue_manager)
            return
                
        # match_result is a tuple: (matching_filepath_basename, item_dict)
        filename = match_result[0] # This is already the basename
        logging.debug(f"Best matching file for {item_identifier}: {filename}")
            
        # Update database and item with matched file
        from database import update_media_item
        update_media_item(item['id'], filled_by_file=filename)
        item['filled_by_file'] = filename
            
        # Move the main item to checking
        queue_manager.move_to_checking(
            item=item,
            from_queue="Pending Uncached",
            title=item.get('title', ''),
            link=item.get('filled_by_magnet', ''),
            filled_by_file=filename,
            torrent_id=torrent_id
        )
        self.remove_item(item)

        # Debrid File Naming: rename cli_mount DFS folder using structured CLI name
        _dbn_hash3 = torrent_info.get('hash', '').lower()
        if _dbn_hash3:
            try:
                from utilities.settings import get_setting as _dbn_gs3
                if _dbn_gs3('Debrid Provider', 'enable_debrid_naming', False):
                    from routes.scraper_routes import _build_debrid_title
                    _dbn_mt3 = 'tv' if item.get('type') == 'episode' else item.get('type', '')
                    _dbn_orig3 = item.get('original_scraped_torrent_title') or item.get('filled_by_title', '')
                    _dbn_title3 = _build_debrid_title(
                        title=item.get('title', ''),
                        year=item.get('year', ''),
                        imdb_id=item.get('imdb_id'),
                        version=item.get('version', ''),
                        original_scraped_torrent_title=_dbn_orig3,
                        media_type=_dbn_mt3,
                        season=item.get('season_number'),
                        episode=item.get('episode_number'),
                        episode_title=item.get('episode_title'),
                        tags=item.get('tags') or None,
                        content_source_display_name=item.get('content_source_detail') or item.get('content_source'),
                    )
                    if _dbn_title3 and _dbn_title3 != _dbn_orig3:
                        import threading as _dbn_t3
                        _dbn_item_id3 = item.get('id')
                        def _do_rename3(h, name, item_id):
                            import time as _t
                            try:
                                from usenet.climount_client import get_climount_client
                                _dc3 = get_climount_client()
                                if not hasattr(_dc3, 'rename_nzb'):
                                    return  # active usenet provider (e.g. nzbdav) has no rename semantics
                                # cli_mount only registers an entry as queryable-by-hash after its
                                # own periodic sync (default ~10 min) — a 404 in the first several
                                # attempts is expected, not proof the entry is gone. Only treat 404
                                # as final once it's persisted for that long (20 attempts x 30s).
                                _consecutive_404_3 = 0
                                for _a3 in range(100):
                                    _renamed3, _not_found3 = _dc3.rename_nzb_with_status(h, name)
                                    if _not_found3:
                                        _consecutive_404_3 += 1
                                        if _consecutive_404_3 >= 20:
                                            logging.warning(f'[DebridNaming] {h!r} not found in cli_mount (404) for {_consecutive_404_3} consecutive attempts — giving up (pending uncached)')
                                            return
                                    else:
                                        _consecutive_404_3 = 0
                                    if _renamed3:
                                        logging.info(f'[DebridNaming] Renamed {h!r} -> {name!r} (pending uncached)')
                                        if item_id:
                                            try:
                                                from database.database_writing import update_media_item as _umi
                                                _umi(item_id, debrid_folder_name=name)
                                            except Exception as _db_err:
                                                logging.debug(f'[DebridNaming] DB update failed (pending uncached): {_db_err}')
                                            _dc3.register_cli_ids_for_item(h, item_id)
                                            _dc3.push_tags_for_item(h, item_id)
                                        return
                                    _t.sleep(30)
                                logging.warning(f'[DebridNaming] Could not rename {h!r} after 100 attempts (pending uncached)')
                            except Exception as _e3:
                                logging.debug(f'[DebridNaming] Rename error (pending uncached): {_e3}')
                        _dbn_t3.Thread(target=_do_rename3, args=(_dbn_hash3, _dbn_title3, _dbn_item_id3), daemon=True).start()
            except Exception as _dbn_ex3:
                logging.debug(f'[DebridNaming] Setup error (pending uncached): {_dbn_ex3}')

        # Check for related items if it's an episode
        if item.get('type') == 'episode':
            logging.debug(f"Checking for related episodes for {item_identifier}")

            # Get items from Scraping and Wanted queues via queue_manager
            scraping_queue = queue_manager.queues.get('Scraping')
            scraping_items = scraping_queue.get_contents() if scraping_queue else []
            
            wanted_queue = queue_manager.queues.get('Wanted')
            wanted_items = wanted_queue.get_contents() if wanted_queue else []
            
            related_item_tuples = self.media_matcher.find_related_items(
                parsed_torrent_files=parsed_torrent_files,
                scraping_items=scraping_items,
                wanted_items=wanted_items,
                original_item=item 
            )
                
            if related_item_tuples:
                logging.debug(f"Found {len(related_item_tuples)} related items for {item_identifier}")
                
            main_item_magnet_link = item.get('filled_by_magnet', '')

            for related_item_dict, related_filename_basename in related_item_tuples:
                related_identifier = f"{related_item_dict.get('title')} S{related_item_dict.get('season_number')}E{related_item_dict.get('episode_number')}"
                logging.debug(f"Processing related item: {related_identifier}")
                
                # find_related_items already determined the match and file.
                logging.debug(f"Moving related item {related_identifier} to checking with file: {related_filename_basename}")
                
                from_queue_state = related_item_dict.get('state', 'Unknown')
                
                queue_manager.move_to_checking(
                    item=related_item_dict,
                    from_queue=from_queue_state,
                    title=related_item_dict.get('title', ''),
                    link=main_item_magnet_link, # Use the main item's magnet link
                    filled_by_file=related_filename_basename,
                    torrent_id=torrent_id
                )
                # These items are from 'Scraping' or 'Wanted' queues, not self.items.
                # Their state transition is handled by move_to_checking.
            
    def _handle_failed_add(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.error(f"Failed to add pending uncached item: {item_identifier}")
        
        # Move back to Wanted state
        from database import update_media_item_state
        update_media_item_state(item['id'], 'Wanted')
        self.remove_item(item)

    def _get_parsed_scrape_results(self, item: Dict[str, Any]):
        scrape_results = item.get('scrape_results', [])
        if isinstance(scrape_results, str):
            try:
                return json.loads(scrape_results)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse scrape_results for {item.get('id')}")
                return []
        elif isinstance(scrape_results, list) and all(isinstance(x, str) for x in scrape_results):
            try:
                joined_string = ''.join(scrape_results)
                return json.loads(joined_string)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse joined scrape_results for {item.get('id')}")
                return []
        elif isinstance(scrape_results, list):
            return scrape_results
        else:
            logging.error(f"Invalid scrape_results format for {item.get('id')}")
            return []

    def contains_item_id(self, item_id):
        """Check if the queue contains an item with the given ID"""
        return any(i['id'] == item_id for i in self.items)

    # Add other methods as needed