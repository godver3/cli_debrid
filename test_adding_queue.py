#!/usr/bin/env python3

import logging
from database import update_media_item_state, update_media_item
from queues.adding_queue import AddingQueue
from queues.mock_queue_manager import MockQueueManager
from datetime import datetime
import json

# Configure logging
logging.basicConfig(level=logging.DEBUG)

def main():
    # Big Buck Bunny magnet
    magnet = ('magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny'
             '&tr=udp%3A%2F%2Fexplodie.org%3A6969&tr=udp%3A%2F%2Ftracker.coppersurfer.tk%3A6969'
             '&tr=udp%3A%2F%2Ftracker.empire-js.us%3A1337&tr=udp%3A%2F%2Ftracker.leechers-paradise.org%3A6969'
             '&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337&tr=wss%3A%2F%2Ftracker.btorrent.xyz'
             '&tr=wss%3A%2F%2Ftracker.fastcast.nz&tr=wss%3A%2F%2Ftracker.openwebtorrent.com'
             '&ws=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2F'
             '&xs=https%3A%2F%2Fwebtorrent.io%2Ftorrents%2Fbig-buck-bunny.torrent')
    
    # Create test item
    item_id = 1
    
    # Set item state to Adding
    logging.info("Setting item state to Adding")
    update_media_item_state(item_id, 'Adding')
    
    # Update item details
    logging.info("Updating item details")
    scrape_results = [{'title': 'Big Buck Bunny', 'magnet': magnet}]
    update_media_item(
        item_id,
        type='movie',
        title='Big Buck Bunny',
        release_date='2025-01-01',
        scrape_results=json.dumps(scrape_results)
    )
    
    # Create adding queue and queue manager
    logging.info("Creating adding queue and queue manager")
    adding_queue = AddingQueue()
    queue_manager = MockQueueManager()
    
    # Update and process adding queue
    logging.info("Updating adding queue")
    adding_queue.update()
    
    logging.info("Processing adding queue")
    adding_queue.process(queue_manager)

if __name__ == '__main__':
    main()
