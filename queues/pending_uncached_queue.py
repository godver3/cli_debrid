import logging
import json
import os
from typing import Dict, Any
from database import get_all_media_items, update_media_item_state, update_media_item
from debrid import get_debrid_provider
from queues.adding_queue import AddingQueue
from .torrent_processor import TorrentProcessor
from .media_matcher import MediaMatcher

class PendingUncachedQueue:
    def __init__(self):
        self.items = []
        self.debrid_provider = get_debrid_provider()
        self.adding_queue = AddingQueue()
        self.torrent_processor = TorrentProcessor(self.debrid_provider)
        self.media_matcher = MediaMatcher()

    def update(self):
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
        logging.debug(f"Processing pending uncached queue. Items: {len(self.items)}")
        
        active_downloads, download_limit = self.debrid_provider.get_active_downloads()
        
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
            processed_magnet = self.torrent_processor.process_magnet(link)
            if not processed_magnet:
                logging.error(f"Failed to process magnet link for {item_identifier}")
                self._handle_failed_add(item, queue_manager)
                continue

            # Try to add the processed magnet
            add_result = self.debrid_provider.add_torrent(processed_magnet)
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
        
        scrape_results = self._get_parsed_scrape_results(item)
        logging.debug(f"Parsed scrape_results: {scrape_results[:5]}...")  # Log first 5 items
        
        # Get the torrent info from the debrid provider
        torrent_info = self.debrid_provider.get_torrent_info(torrent_id)
        if torrent_info and 'files' in torrent_info:
            # Match content using MediaMatcher
            files = torrent_info['files']
            matches = self.media_matcher.match_content(files, item)
            
            if not matches:
                logging.debug(f"No matching files found in torrent for {item_identifier}")
                self._handle_failed_add(item, queue_manager)
                return
                
            # Get the best matching file
            matched_file = matches[0][0]  # First match's path
            filename = os.path.basename(matched_file)
            logging.debug(f"Best matching file for {item_identifier}: {filename}")
            
            # Update database and item with matched file
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
            
            # Check for related items if it's an episode
            if item.get('type') == 'episode':
                logging.debug(f"Checking for related episodes for {item_identifier}")
                # Get other items from the pending uncached queue
                pending_items = [i for i in self.items if i['id'] != item['id']]
                related_items = self.media_matcher.find_related_items(files, pending_items, item)
                
                if related_items:
                    logging.debug(f"Found {len(related_items)} related items for {item_identifier}")
                
                # Move related items to checking
                for related in related_items:
                    related_identifier = f"{related.get('title')} S{related.get('season_number')}E{related.get('episode_number')}"
                    logging.debug(f"Processing related item: {related_identifier}")
                    related_matches = self.media_matcher.match_content(files, related)
                    if related_matches:
                        related_file = related_matches[0][0]  # First match's path
                        related_filename = os.path.basename(related_file)
                        logging.debug(f"Moving related item {related_identifier} to checking with file: {related_filename}")
                        queue_manager.move_to_checking(
                            item=related,
                            from_queue="Pending Uncached",
                            title=related.get('title', ''),
                            link=related.get('filled_by_magnet', ''),
                            filled_by_file=related_filename,
                            torrent_id=torrent_id
                        )
                    else:
                        logging.debug(f"No matching files found for related item: {related_identifier}")
            
    def _handle_failed_add(self, item: Dict[str, Any], queue_manager):
        item_identifier = queue_manager.generate_identifier(item)
        logging.error(f"Failed to add pending uncached item: {item_identifier}")
        
        # Move back to Wanted state
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

    # Add other methods as needed