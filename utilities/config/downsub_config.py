import os
from pathlib import Path
import sys

# Add the parent directory to the Python path so we can import settings
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from settings import get_setting

# Check if subtitle downloading is enabled
SUBTITLES_ENABLED = get_setting('Subtitle Settings', 'enable_subtitles', False)

# Check if we should only process the current file
ONLY_CURRENT_FILE = get_setting('Subtitle Settings', 'only_current_file', False)

# Get paths from environment variables with fallbacks
USER_CONFIG = os.environ.get('USER_CONFIG', '/user/config')
USER_LOGS = os.environ.get('USER_LOGS', '/user/logs')
USER_DB_CONTENT = os.environ.get('USER_DB_CONTENT', '/user/db_content')

# Cache directory within db_content
CACHE_DIR = os.path.join(USER_DB_CONTENT, 'subtitle_cache')

# Construct video folder paths
SYMLINKED_PATH = get_setting('File Management', 'symlinked_files_path')
MOVIES_FOLDER = get_setting('Debug', 'movies_folder_name')
TV_SHOWS_FOLDER = get_setting('Debug', 'tv_shows_folder_name')
ENABLE_ANIME = get_setting('Debug', 'enable_separate_anime_folders', False)
ANIME_MOVIES_FOLDER = get_setting('Debug', 'anime_movies_folder_name') if ENABLE_ANIME else None
ANIME_TV_SHOWS_FOLDER = get_setting('Debug', 'anime_tv_shows_folder_name') if ENABLE_ANIME else None

# Get folder application settings
APPLY_TO_MOVIES = get_setting('Subtitle Settings', 'apply_to_movies', True)
APPLY_TO_TV_SHOWS = get_setting('Subtitle Settings', 'apply_to_tv_shows', True)
APPLY_TO_ANIME_MOVIES = get_setting('Subtitle Settings', 'apply_to_anime_movies', True)
APPLY_TO_ANIME_TV_SHOWS = get_setting('Subtitle Settings', 'apply_to_anime_tv_shows', True)

# Initialize VIDEO_FOLDERS as an empty list
VIDEO_FOLDERS = []

# Only add paths if SYMLINKED_PATH is not None
if SYMLINKED_PATH is not None:
    if MOVIES_FOLDER and APPLY_TO_MOVIES:
        VIDEO_FOLDERS.append(os.path.join(SYMLINKED_PATH, MOVIES_FOLDER))
    
    if TV_SHOWS_FOLDER and APPLY_TO_TV_SHOWS:
        VIDEO_FOLDERS.append(os.path.join(SYMLINKED_PATH, TV_SHOWS_FOLDER))
    
    if ENABLE_ANIME and ANIME_MOVIES_FOLDER and APPLY_TO_ANIME_MOVIES:
        VIDEO_FOLDERS.append(os.path.join(SYMLINKED_PATH, ANIME_MOVIES_FOLDER))
        
    if ENABLE_ANIME and ANIME_TV_SHOWS_FOLDER and APPLY_TO_ANIME_TV_SHOWS:
        VIDEO_FOLDERS.append(os.path.join(SYMLINKED_PATH, ANIME_TV_SHOWS_FOLDER))

# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

# Cache files
SCAN_CACHE_FILE = os.path.join(CACHE_DIR, 'scan_cache.json')
DIR_CACHE_FILE = os.path.join(CACHE_DIR, 'dir_cache.json')

# Logging
LOG_FILE = os.path.join(USER_LOGS, 'subtitle_downloader.log')
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_LEVEL = "INFO"

# Get settings from settings schema
OPENSUBTITLES_USERNAME = get_setting('Subtitle Settings', 'opensubtitles_username')
OPENSUBTITLES_PASSWORD = get_setting('Subtitle Settings', 'opensubtitles_password')

# Parse subtitle languages from comma-separated string
SUBTITLE_LANGUAGES = [
    lang.strip() 
    for lang in get_setting('Subtitle Settings', 'subtitle_languages', 'eng,zho').split(',')
    if lang.strip()
]

# Get subtitle providers from settings
SUBTITLE_PROVIDERS = get_setting('Subtitle Settings', 'subtitle_providers', [
    "opensubtitles",
    "opensubtitlescom",
    "podnapisi",
    "tvsubtitles"
])

# User agent for API requests
SUBLIMINAL_USER_AGENT = get_setting('Subtitle Settings', 'user_agent', 
                                  'SubDownloader/1.0 (your-email@example.com)')

# Supported video extensions
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov") 