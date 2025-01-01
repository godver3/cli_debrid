# Real-Debrid Module Documentation

This document provides documentation for the Real-Debrid module functionality and its API integration.

## Utility Functions

### `get_api_key()`
- **Description**: Retrieves the Real-Debrid API key from settings
- **Returns**: String containing the API key
- **Raises**: ValueError if API key is not found in settings

### `timed_lru_cache(seconds: int, maxsize: int = 128)`
- **Description**: Decorator that provides a time-based LRU cache
- **Parameters**:
  - `seconds`: Cache lifetime in seconds
  - `maxsize`: Maximum size of the cache (default: 128)
- **Returns**: Cached function result if within time limit

### `is_video_file(filename)`
- **Description**: Checks if a file is a video based on its extension
- **Parameters**: `filename` - Name of the file to check
- **Returns**: Boolean indicating if file is a video

### `is_unwanted_file(filename)`
- **Description**: Checks if a file is unwanted (e.g., sample files)
- **Parameters**: `filename` - Name of the file to check
- **Returns**: Boolean indicating if file is unwanted

### `extract_hash_from_magnet(magnet_link)`
- **Description**: Extracts hash from a magnet link
- **Parameters**: `magnet_link` - Magnet URL string
- **Returns**: Hash string from magnet link

### `is_cached_on_rd(hashes)`
- **Description**: Checks if hash(es) are cached on Real-Debrid
- **Parameters**: `hashes` - Single hash string or list of hashes
- **Returns**: Dictionary mapping hashes to their cache status

### `get_cached_files(hash_)`
- **Description**: Retrieves cached files for a specific hash
- **Parameters**: `hash_` - Hash string to check
- **Returns**: List of cached files

### `get_active_downloads(check=False)`
- **Description**: Gets number of active downloads and concurrent download limit
- **Parameters**: `check` - Whether to check against limits
- **Returns**: Tuple of (active downloads, download limit)
- **Raises**: RealDebridTooManyDownloadsError if limits exceeded

### `get_user_limits()`
- **Description**: Retrieves user account limits from Real-Debrid API
- **Returns**: Dictionary containing user limits

### `check_daily_usage()`
- **Description**: Checks daily API usage statistics
- **Returns**: Dictionary containing usage statistics

### `get_user_traffic()`
- **Description**: Retrieves user traffic information
- **Returns**: Dictionary containing traffic data

## Main Class: RealDebridProvider

### Methods

#### `__init__(self)`
- **Description**: Initializes RealDebrid provider with rate limiting
- **Fields**:
  - `API_BASE_URL`: Base URL for Real-Debrid API
  - `MAX_DOWNLOADS`: Maximum concurrent downloads (25)

#### `add_torrent(self, magnet_link, temp_file_path=None)`
- **Description**: Adds a torrent/magnet to Real-Debrid
- **Parameters**:
  - `magnet_link`: Magnet URL or hash
  - `temp_file_path`: Optional path to torrent file
- **Returns**: Torrent ID if successful, None if failed

#### `list_torrents(self)`
- **Description**: Lists all torrents in Real-Debrid account
- **Returns**: List of torrent information

#### `get_torrent_info(self, torrent_id: str)`
- **Description**: Gets detailed information about a specific torrent
- **Parameters**: `torrent_id` - ID of the torrent
- **Returns**: Dictionary containing torrent information

#### `remove_torrent(self, torrent_id: str)`
- **Description**: Removes a torrent from Real-Debrid
- **Parameters**: `torrent_id` - ID of the torrent to remove
- **Returns**: Boolean indicating success

## Error Classes

### `RealDebridUnavailableError`
- Raised when Real-Debrid service is unavailable

### `RealDebridTooManyDownloadsError`
- Raised when download limits are exceeded

## Rate Limiting

The module implements rate limiting to prevent API abuse:
- Default rate: 0.5 calls per second
- Automatic retry mechanism for failed requests
- Exponential backoff for retries

## Caching

The module implements several caching mechanisms:
- LRU cache with time expiration for API responses
- In-memory cache for frequently accessed data
- Cached responses for hash checks and file information
