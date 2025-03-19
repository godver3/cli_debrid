import os
from pathlib import Path
import sys

# Add the parent directory to the Python path so we can import settings
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from utilities.settings import get_setting

def reload_settings():
    """Reload all settings and return them as a dictionary."""
    global SUBTITLES_ENABLED, ONLY_CURRENT_FILE, VIDEO_FOLDERS
    global SUBTITLE_LANGUAGES, SUBTITLE_PROVIDERS, SUBLIMINAL_USER_AGENT
    global OPENSUBTITLES_USERNAME, OPENSUBTITLES_PASSWORD

    config = {
        'SUBTITLES_ENABLED': get_setting('Subtitle Settings', 'enable_subtitles', False),
        'ONLY_CURRENT_FILE': get_setting('Subtitle Settings', 'only_current_file', False),
        
        # Get paths from environment variables with fallbacks
        'USER_CONFIG': os.environ.get('USER_CONFIG', '/user/config'),
        'USER_LOGS': os.environ.get('USER_LOGS', '/user/logs'),
        'USER_DB_CONTENT': os.environ.get('USER_DB_CONTENT', '/user/db_content'),
        
        # Get folder settings
        'SYMLINKED_PATH': get_setting('File Management', 'symlinked_files_path'),
        'MOVIES_FOLDER': get_setting('Debug', 'movies_folder_name'),
        'TV_SHOWS_FOLDER': get_setting('Debug', 'tv_shows_folder_name'),
        'ENABLE_ANIME': get_setting('Debug', 'enable_separate_anime_folders', False),
        'ANIME_MOVIES_FOLDER': get_setting('Debug', 'anime_movies_folder_name'),
        'ANIME_TV_SHOWS_FOLDER': get_setting('Debug', 'anime_tv_shows_folder_name'),
        
        # Get folder application settings
        'APPLY_TO_MOVIES': get_setting('Subtitle Settings', 'apply_to_movies', True),
        'APPLY_TO_TV_SHOWS': get_setting('Subtitle Settings', 'apply_to_tv_shows', True),
        'APPLY_TO_ANIME_MOVIES': get_setting('Subtitle Settings', 'apply_to_anime_movies', True),
        'APPLY_TO_ANIME_TV_SHOWS': get_setting('Subtitle Settings', 'apply_to_anime_tv_shows', True),
        
        # Subtitle settings
        'OPENSUBTITLES_USERNAME': get_setting('Subtitle Settings', 'opensubtitles_username'),
        'OPENSUBTITLES_PASSWORD': get_setting('Subtitle Settings', 'opensubtitles_password'),
        'SUBTITLE_LANGUAGES': [
            lang.strip() 
            for lang in get_setting('Subtitle Settings', 'subtitle_languages', 'eng,zho').split(',')
            if lang.strip()
        ],
        'SUBTITLE_PROVIDERS': get_setting('Subtitle Settings', 'subtitle_providers', [
            "opensubtitles",
            "opensubtitlescom",
            "podnapisi",
            "tvsubtitles"
        ]),
        'SUBLIMINAL_USER_AGENT': get_setting('Subtitle Settings', 'user_agent', 
                                           'SubDownloader/1.0 (your-email@example.com)'),
    }

    # Update VIDEO_FOLDERS based on current settings
    video_folders = []
    if config['SYMLINKED_PATH'] is not None:
        if config['MOVIES_FOLDER'] and config['APPLY_TO_MOVIES']:
            video_folders.append(os.path.join(config['SYMLINKED_PATH'], config['MOVIES_FOLDER']))
        
        if config['TV_SHOWS_FOLDER'] and config['APPLY_TO_TV_SHOWS']:
            video_folders.append(os.path.join(config['SYMLINKED_PATH'], config['TV_SHOWS_FOLDER']))
        
        if config['ENABLE_ANIME']:
            if config['ANIME_MOVIES_FOLDER'] and config['APPLY_TO_ANIME_MOVIES']:
                video_folders.append(os.path.join(config['SYMLINKED_PATH'], config['ANIME_MOVIES_FOLDER']))
            
            if config['ANIME_TV_SHOWS_FOLDER'] and config['APPLY_TO_ANIME_TV_SHOWS']:
                video_folders.append(os.path.join(config['SYMLINKED_PATH'], config['ANIME_TV_SHOWS_FOLDER']))
    
    config['VIDEO_FOLDERS'] = video_folders

    # Update all global variables
    for key, value in config.items():
        globals()[key] = value

    return config

# Constants that don't need reloading
CACHE_DIR = os.path.join(os.environ.get('USER_DB_CONTENT', '/user/db_content'), 'subtitle_cache')
SCAN_CACHE_FILE = os.path.join(CACHE_DIR, 'scan_cache.json')
DIR_CACHE_FILE = os.path.join(CACHE_DIR, 'dir_cache.json')
LOG_FILE = os.path.join(os.environ.get('USER_LOGS', '/user/logs'), 'subtitle_downloader.log')
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_LEVEL = "INFO"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov")

# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

# Initial load of settings
reload_settings() 