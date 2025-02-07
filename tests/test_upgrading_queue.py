import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from queues.upgrading_queue import UpgradingQueue
from queues.adding_queue import AddingQueue
from queues.torrent_processor import TorrentProcessor
import logging
import requests
import urllib3
from urllib3.response import HTTPResponse
from io import BytesIO
import json
from debrid.common import extract_hash_from_magnet
from debrid.real_debrid.client import RealDebridProvider
import bencodepy
import pickle
import os
import pathlib
from pathlib import Path

# Add api_tracker import
from api_tracker import setup_api_logging, api

class TestUpgradingQueueDuplicateAddition(unittest.TestCase):
    def setUp(self):
        # Set up logging
        logging.basicConfig(level=logging.DEBUG)
        
        # Set up API logging
        setup_api_logging()
        
        # Create mock response data for different API endpoints
        self.torrent_info_response = {
            'id': '2DVCAIECUWUQU',
            'filename': 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY',
            'status': 'downloaded',
            'hash': 'e39c91a7c884b31dab39e2a84f69dce53a43b856',
            'bytes': 5368709120,  # 5GB
            'original_bytes': 5368709120,
            'files': [{'path': 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY.mkv', 'bytes': 5368709120}]
        }
        
        self.add_magnet_response = {
            'id': '2DVCAIECUWUQU',
            'uri': 'https://api.real-debrid.com/rest/1.0/torrents/info/2DVCAIECUWUQU'
        }
        
        # Create our mock item
        self.test_item = {
            'id': '81479',
            'title': 'Bogota: City of the Lost',
            'type': 'movie',
            'imdb_id': 'tt22507374',
            'version': '1080p',
            'filled_by_file': 'Bogota City of the Lost (2024) 1080p H264 iTA EnG Kor AC3 Sub iTA EnG Kor - MIRCrew.mkv',
            'filled_by_torrent_id': 'original_torrent_id',
            'state': 'Upgrading',
            'original_scraped_torrent_title': 'Bogota City of the Lost 2024 1080p H264 iTA EnG Kor AC3 Sub iTA EnG Kor - MIRCrew',
            'filled_by_title': 'Bogota City of the Lost 2024 1080p H264 iTA EnG Kor AC3 Sub iTA EnG Kor - MIRCrew',
            'disable_not_wanted_check': True,
            'original_collected_at': datetime(2024, 2, 7, 9, 58, 48, 999413)
        }
        
        # Mock the debrid provider
        self.mock_debrid = Mock()
        # Configure is_cached to return proper format
        self.mock_debrid.is_cached.side_effect = [True, True]  # Both results will be considered cached
        self.mock_debrid.add_torrent.side_effect = ['2DVCAIECUWUQU', '6SGHVKRJG7ZL4']  # Will return two different torrent IDs
        self.mock_debrid.get_torrent_info.return_value = {
            'id': '2DVCAIECUWUQU',
            'filename': 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY',
            'status': 'downloaded',
            'files': [{'path': 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY.mkv'}]
        }
        
        # Configure additional provider methods
        self.mock_debrid._load_api_key.return_value = "test_api_key"
        self.mock_debrid.verify_torrent_presence.return_value = True
        self.mock_debrid.get_cached_torrent_id.return_value = '2DVCAIECUWUQU'
        self.mock_debrid.get_cached_torrent_title.return_value = 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY'
        self.mock_debrid._cached_torrent_ids = {}
        self.mock_debrid._cached_torrent_titles = {}
        
        # Create a better result that should trigger the upgrade
        self.better_result = {
            'title': 'Bogota City of the Lost 2024 1080p NF WEB-DL DUAL DDP5 1 H 264-MARKY',
            'magnet': 'magnet:?xt=urn:btih:e39c91a7c884b31dab39e2a84f69dce53a43b856&dn=Bogota+City+of+the+Lost+2024+1080p+NF+WEB-DL+DUAL+DDP5+1+H+264-MARKY',
            'size': 5.72,
            'seeders': 10,
            'resolution': '1080p',
            'score_breakdown': {
                'total_score': 326.3,  # Higher score
                'similarity_score': 24.78,
                'resolution_score': 75.0,
                'size_score': 75.0,
                'bitrate_score': 75.0
            },
            'original_scraped_torrent_title': 'Bogota City of the Lost 2024 1080p NF WEB-DL DUAL DDP5 1 H 264-MARKY',
            'parsed_info': {
                'title': 'Bogota City of the Lost',
                'year': 2024,
                'resolution': '1080p',
                'source': 'WEB-DL',
                'audio': 'DDP5.1',
                'codec': 'H.264'
            }
        }
        
        # Update current result to match format
        self.current_result = {
            'title': self.test_item['original_scraped_torrent_title'],  # Match exactly
            'magnet': 'magnet:?xt=urn:btih:02e97e168f5b207c36848e1e0c61b6f50daadf91',
            'size': 4.72,
            'seeders': 4,
            'resolution': '1080p',
            'score_breakdown': {
                'total_score': 226.3,
                'similarity_score': 24.78,
                'resolution_score': 75.0,
                'size_score': 50.0,
                'bitrate_score': 50.0
            },
            'original_scraped_torrent_title': self.test_item['original_scraped_torrent_title'],  # Match exactly
            'parsed_info': {
                'title': 'Bogota City of the Lost',
                'year': 2024,
                'resolution': '1080p',
                'source': 'HDTV',
                'audio': 'AC3',
                'codec': 'H.264'
            }
        }

    @patch('requests.Session', new_callable=MagicMock)
    @patch('not_wanted_magnets.is_url_not_wanted')
    @patch('not_wanted_magnets.is_magnet_not_wanted')
    @patch('settings.get_setting')
    @patch('database.update_media_item')
    @patch('database.update_media_item_state')
    @patch('database.get_media_item_by_id')
    @patch('notifications._send_notifications')
    @patch('debrid.real_debrid.api.get_api_key')
    @patch('logging.getLogger')
    @patch('api_tracker.api')
    @patch('debrid.real_debrid.api.make_request')
    @patch('queues.torrent_processor.requests.Session', new_callable=MagicMock)
    @patch('queues.torrent_processor.bencodepy.decode')
    @patch('queues.torrent_processor.download_and_extract_hash')
    @patch('queues.torrent_processor.extract_hash_from_magnet')
    @patch('queues.torrent_processor.extract_hash_from_file')
    @patch('pickle.load')
    @patch('pickle.dump')
    @patch('os.environ.get')
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.open')
    @patch('scraper.scraper.scrape')
    @patch('wake_count_manager.wake_count_manager')
    @patch('debrid.get_debrid_provider')
    @patch('queues.media_matcher.MediaMatcher')
    @patch('urllib3.connectionpool.HTTPSConnectionPool')
    @patch('urllib3.connectionpool.HTTPConnectionPool')
    @patch('debrid.real_debrid.client.RealDebridProvider', autospec=True)
    def test_duplicate_torrent_addition(self, mock_provider_class, mock_http_pool, mock_https_pool, mock_media_matcher_class, 
                                      mock_get_debrid_provider, mock_wake_manager, mock_scrape, mock_path_open, mock_path_exists, 
                                      mock_environ_get, mock_pickle_dump, mock_pickle_load, mock_extract_hash_file, 
                                      mock_extract_hash_magnet, mock_download_hash, mock_bencode_decode, 
                                      mock_torrent_processor_session, mock_make_request, mock_api, mock_logger, 
                                      mock_get_api_key, mock_send_notification, mock_get_item, mock_update_state, 
                                      mock_update_item, mock_get_setting, mock_is_magnet_not_wanted, 
                                      mock_is_url_not_wanted, mock_client_session):
        # Configure API key mock
        mock_get_api_key.return_value = "test_api_key"
        
        # Configure urllib3 connection pool mocks to prevent real HTTP connections
        mock_pool_instance = MagicMock()
        mock_pool_instance.request.side_effect = lambda method, url, **kwargs: mock_request(method, url, **kwargs)
        mock_http_pool.return_value = mock_pool_instance
        mock_https_pool.return_value = mock_pool_instance
        
        # Configure hash extraction mocks
        mock_extract_hash_magnet.side_effect = extract_hash_from_magnet  # Use real function
        mock_extract_hash_file.return_value = "e39c91a7c884b31dab39e2a84f69dce53a43b856"  # Return test hash
        mock_download_hash.return_value = "e39c91a7c884b31dab39e2a84f69dce53a43b856"  # Return test hash
        mock_bencode_decode.return_value = {b'info_hash': b'e39c91a7c884b31dab39e2a84f69dce53a43b856'}
        
        # Configure pickle mocks
        mock_pickle_load.return_value = {}  # Return empty dict for all pickle loads
        mock_pickle_dump.return_value = None  # Do nothing for pickle dumps
        
        # Configure environment and path mocks
        mock_environ_get.return_value = "/user/db_content"
        mock_path_exists.return_value = False  # Files don't exist initially
        mock_path_open.return_value = MagicMock(__enter__=MagicMock(), __exit__=MagicMock())  # Mock context manager
        
        # Configure scraping mocks
        mock_scrape.return_value = [self.better_result, self.current_result]  # Return both results
        mock_wake_manager.get_wake_count.return_value = 0
        mock_wake_manager.increment_wake_count.return_value = None
        
        # Configure debrid provider mocks
        mock_provider_instance = mock_provider_class.return_value
        mock_provider_instance.is_cached.side_effect = [True, True]  # Both results will be considered cached
        mock_provider_instance.add_torrent.side_effect = ['2DVCAIECUWUQU', '6SGHVKRJG7ZL4']  # Will return two different torrent IDs
        mock_provider_instance.get_torrent_info.return_value = self.torrent_info_response
        mock_provider_instance._load_api_key.return_value = "test_api_key"
        mock_provider_instance.verify_torrent_presence.return_value = True
        mock_provider_instance.get_cached_torrent_id.return_value = '2DVCAIECUWUQU'
        mock_provider_instance.get_cached_torrent_title.return_value = 'Bogota.City.of.the.Lost.2024.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-MARKY'
        mock_provider_instance._cached_torrent_ids = {}
        mock_provider_instance._cached_torrent_titles = {}
        mock_get_debrid_provider.return_value = mock_provider_instance
        
        # Configure media matcher mock
        mock_media_matcher_instance = mock_media_matcher_class.return_value
        mock_media_matcher_instance.match_media.return_value = True
        
        # Configure mock request handler
        def mock_request(method, url, **kwargs):
            response = MagicMock()
            response.status_code = 200
            response.raise_for_status = MagicMock()
            
            # Ensure proper authorization header is present
            headers = kwargs.get('headers', {})
            auth_header = headers.get('Authorization', '')
            if not auth_header.startswith('Bearer test_api_key'):
                response.status_code = 401
                response.raise_for_status.side_effect = requests.exceptions.HTTPError("401 Client Error: Unauthorized")
                return response
            
            # Handle different API endpoints
            if url.endswith('/torrents/addMagnet'):
                # Check if magnet link is provided in data
                data = kwargs.get('data', {})
                if 'magnet' in data:
                    response.json = lambda: self.add_magnet_response
                else:
                    response.status_code = 400
                    response.raise_for_status.side_effect = requests.exceptions.HTTPError("400 Client Error: Bad Request - Missing magnet link")
            elif url.endswith('/torrents/info/2DVCAIECUWUQU'):
                response.json = lambda: self.torrent_info_response
            elif url.endswith('/torrents'):
                response.json = lambda: [self.torrent_info_response]
            elif '/torrents/info/' in url:
                response.json = lambda: self.torrent_info_response
            else:
                response.status_code = 404
                response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Client Error: Not Found")
            
            # Add debug logging for request
            logging.debug(f"\n=== Mock Request ===")
            logging.debug(f"Method: {method}")
            logging.debug(f"URL: {url}")
            logging.debug(f"Headers: {headers}")
            logging.debug(f"Data: {kwargs.get('data')}")
            logging.debug(f"Response status: {response.status_code}")
            try:
                logging.debug(f"Response data: {response.json()}")
            except:
                logging.debug("Response data: <error response>")
            
            return response
        
        # Configure API mock
        mock_api.rate_limiter = Mock()
        mock_api.rate_limiter.check_limits.return_value = True
        
        # Create a mock session instance with proper request methods
        mock_session = MagicMock(spec=requests.Session)
        
        def create_request_method(method):
            def request_method(url, **kwargs):
                return mock_request(method, url, **kwargs)
            return request_method
            
        mock_session.get = create_request_method('GET')
        mock_session.post = create_request_method('POST')
        mock_session.put = create_request_method('PUT')
        mock_session.delete = create_request_method('DELETE')
        mock_session.request = lambda method, url, **kwargs: mock_request(method, url, **kwargs)
        
        # Configure the API mock
        mock_api.session = mock_session
        mock_api.get = mock_session.get
        mock_api.post = mock_session.post
        mock_api.put = mock_session.put
        mock_api.delete = mock_session.delete
        mock_api.exceptions = requests.exceptions
        mock_api.utils = requests.utils
        mock_api.Session = type('MockSession', (), {'__call__': lambda self: mock_session})()  # Create a callable that returns our mock session
        
        # Configure make_request mock to use our mock_request function
        def mock_make_request_func(method, endpoint, api_key, data=None, files=None, **kwargs):
            if not kwargs.get('headers'):
                kwargs['headers'] = {}
            kwargs['headers']['Authorization'] = f'Bearer {api_key}'
            
            # Add debug logging for make_request
            logging.debug(f"\n=== Mock Make Request ===")
            logging.debug(f"Method: {method}")
            logging.debug(f"Endpoint: {endpoint}")
            logging.debug(f"API Key: {api_key}")
            logging.debug(f"Data: {data}")
            logging.debug(f"Files: {files}")
            logging.debug(f"Extra kwargs: {kwargs}")
            
            response = mock_request(
                method,
                f"https://api.real-debrid.com/rest/1.0{endpoint}",
                data=data,
                files=files,
                **kwargs
            )
            
            if response.status_code >= 400:
                response.raise_for_status()
            
            return response.json()
            
        mock_make_request.side_effect = mock_make_request_func
        
        # Configure settings mock to enable hybrid mode and disable not wanted check
        mock_get_setting.side_effect = lambda section, key, default=None: {
            ('Scraping', 'hybrid_mode'): True,
            ('Scraping', 'uncached_content_handling'): 'None',
            ('Debug', 'disable_not_wanted_check'): True,
            ('Scraping', 'upgrading_percentage_threshold'): '0.1',
            ('Debrid Provider', 'api_key'): 'test_api_key',
            ('Scraping', 'upgrade_similarity_threshold'): 0.80,  # Lower threshold to allow upgrades
            ('Scraping', 'enable_upgrading'): True,
            ('Scraping', 'enable_upgrading_cleanup'): False,
            ('Scraping', 'source_priority'): {
                'WEB-DL': 100,
                'HDTV': 50
            },
            ('Scraping', 'audio_priority'): {
                'DDP5.1': 100,
                'AC3': 50
            },
            ('Scraping', 'versions'): {
                'resolution_weight': 3,
                'similarity_weight': 3,
                'size_weight': 3,
                'bitrate_weight': 3,
                'similarity_threshold': 0.8,
                'min_size_gb': 0.01,
                'max_size_gb': float('inf')
            }
        }.get((section, key), default)
        
        # Configure not wanted checks to return False
        mock_is_magnet_not_wanted.return_value = False
        mock_is_url_not_wanted.return_value = False
        
        # Disable notifications
        mock_send_notification.return_value = None

        # Mock database functions
        def mock_get_item_func(item_id):
            if item_id == self.test_item['id']:
                return self.test_item
            return None
        mock_get_item.side_effect = mock_get_item_func
        
        def mock_update_state_func(item_id, state, **kwargs):
            if item_id == self.test_item['id']:
                self.test_item['state'] = state
                if kwargs:
                    self.test_item.update(kwargs)
                return True
            return False
        mock_update_state.side_effect = mock_update_state_func
        
        def mock_update_item_func(item_id, **kwargs):
            if item_id == self.test_item['id']:
                self.test_item.update(kwargs)
                return True
            return False
        mock_update_item.side_effect = mock_update_item_func
        
        # Create our queues
        upgrading_queue = UpgradingQueue()
        adding_queue = AddingQueue()
        
        # Create torrent processor with our mock debrid provider
        torrent_processor = TorrentProcessor(mock_provider_instance)
        adding_queue.torrent_processor = torrent_processor
        
        # Add debug logging for torrent processor setup
        logging.debug("=== Torrent Processor Setup ===")
        logging.debug(f"Torrent Processor debrid provider: {torrent_processor.debrid_provider}")
        logging.debug(f"Is mock provider? {isinstance(torrent_processor.debrid_provider, Mock)}")
        logging.debug(f"Provider is_cached side_effect: {mock_provider_instance.is_cached.side_effect}")
        logging.debug(f"Provider add_torrent side_effect: {mock_provider_instance.add_torrent.side_effect}")
        
        # Add our test item to the upgrading queue
        upgrading_queue.add_item(self.test_item)
        
        # Mock failed upgrades to return empty set
        upgrading_queue.failed_upgrades = {}
        
        # Mock scraping functions - ensure both results are returned
        mock_scraping_queue = Mock()
        mock_scraping_queue.scrape_with_fallback.return_value = ([self.better_result, self.current_result], [])
        upgrading_queue.scraping_queue = mock_scraping_queue
        
        # Create a mock queue manager
        mock_queue_manager = Mock()
        mock_queue_manager.generate_identifier.return_value = f"movie_{self.test_item['title']}_{self.test_item['imdb_id']}_{self.test_item['version']}"
        mock_queue_manager.queues = {"Adding": adding_queue}
        mock_queue_manager.move_to_adding = lambda item, from_state, title, results: adding_queue.add_item(item, results)
        
        # Add debug logging before processing
        logging.debug("\n=== Test Setup State ===")
        logging.debug(f"Mock API Key: {mock_get_api_key.return_value}")
        logging.debug(f"Mock Debrid Provider is_cached call count: {self.mock_debrid.is_cached.call_count}")
        logging.debug(f"Mock Debrid Provider is_cached call args: {[call.args for call in self.mock_debrid.is_cached.call_args_list]}")
        logging.debug(f"Mock Debrid Provider add_torrent call count: {self.mock_debrid.add_torrent.call_count}")
        logging.debug(f"Mock Debrid Provider add_torrent call args: {[call.args for call in self.mock_debrid.add_torrent.call_args_list]}")
        logging.debug(f"Test Item State: {self.test_item['state']}")
        logging.debug(f"Better Result Hash: {extract_hash_from_magnet(self.better_result['magnet'])}")
        logging.debug(f"Current Result Hash: {extract_hash_from_magnet(self.current_result['magnet'])}")
        
        # Add debug logging for mock settings
        logging.debug("\n=== Mock Settings ===")
        logging.debug(f"API Key from settings: {mock_get_setting('Debrid Provider', 'api_key')}")
        logging.debug(f"Hybrid mode: {mock_get_setting('Scraping', 'hybrid_mode')}")
        logging.debug(f"Upgrading enabled: {mock_get_setting('Scraping', 'enable_upgrading')}")
        
        # Process the upgrade
        upgrading_queue.hourly_scrape(self.test_item, mock_queue_manager)
        
        # Add debug logging after processing
        logging.debug("\n=== Test Results ===")
        logging.debug(f"Final Item State: {self.test_item['state']}")
        logging.debug(f"Mock Provider is_cached call count: {mock_provider_instance.is_cached.call_count}")
        logging.debug(f"Mock Provider is_cached call args: {[call.args for call in mock_provider_instance.is_cached.call_args_list]}")
        logging.debug(f"Mock Provider add_torrent call count: {mock_provider_instance.add_torrent.call_count}")
        logging.debug(f"Mock Provider add_torrent call args: {[call.args for call in mock_provider_instance.add_torrent.call_args_list]}")
        logging.debug(f"Mock make_request call count: {mock_make_request.call_count}")
        logging.debug(f"Mock make_request call args: {[call.args for call in mock_make_request.call_args_list]}")
        
        # Add debug logging for mock API calls
        logging.debug("\n=== Mock API Calls ===")
        logging.debug(f"Mock API get call count: {mock_api.get.call_count}")
        logging.debug(f"Mock API get call args: {[call.args for call in mock_api.get.call_args_list]}")
        logging.debug(f"Mock API post call count: {mock_api.post.call_count}")
        logging.debug(f"Mock API post call args: {[call.args for call in mock_api.post.call_args_list]}")
        
        # Add assertions to verify the test behavior
        self.assertTrue(mock_scraping_queue.scrape_with_fallback.called, "Scraping function was not called")
        self.assertTrue(mock_provider_instance.is_cached.called, "is_cached was not called")
        self.assertEqual(mock_provider_instance.is_cached.call_count, 2, "is_cached should be called twice")
        self.assertTrue(mock_provider_instance.add_torrent.called, "add_torrent was not called")
        self.assertEqual(mock_provider_instance.add_torrent.call_count, 2, "add_torrent should be called twice")
        
        # Verify the final state
        self.assertEqual(self.test_item['state'], 'Checking', "Item should end in Checking state")

if __name__ == '__main__':
    unittest.main() 