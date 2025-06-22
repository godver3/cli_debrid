import os
from pathlib import Path
import sys

# Add the parent directory to the Python path so we can import settings
project_root_for_settings = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root_for_settings not in sys.path:
    sys.path.insert(0, project_root_for_settings)

try:
    from utilities.settings import get_setting
except ImportError as e:
    raise # Re-raise the exception as this is critical

def reload_settings():
    """Reload all settings and return them as a dictionary."""
    global SUBTITLES_ENABLED, ONLY_CURRENT_FILE, VIDEO_FOLDERS
    global SUBTITLE_LANGUAGES, SUBTITLE_PROVIDERS, SUBLIMINAL_USER_AGENT
    global OPENSUBTITLES_USERNAME, OPENSUBTITLES_PASSWORD

    config = {}
    try:
        config['SUBTITLES_ENABLED'] = get_setting('Subtitle Settings', 'enable_subtitles', False)
        config['ONLY_CURRENT_FILE'] = get_setting('Subtitle Settings', 'only_current_file', False)
        
        config['USER_CONFIG'] = os.environ.get('USER_CONFIG', '/user/config')
        config['USER_LOGS'] = os.environ.get('USER_LOGS', '/user/logs')
        config['USER_DB_CONTENT'] = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        
        config['SYMLINKED_PATH'] = get_setting('File Management', 'symlinked_files_path')

        config['MOVIES_FOLDER'] = get_setting('Debug', 'movies_folder_name')
        config['TV_SHOWS_FOLDER'] = get_setting('Debug', 'tv_shows_folder_name')
        config['ENABLE_ANIME'] = get_setting('Debug', 'enable_separate_anime_folders', False)
        config['ANIME_MOVIES_FOLDER'] = get_setting('Debug', 'anime_movies_folder_name')
        config['ANIME_TV_SHOWS_FOLDER'] = get_setting('Debug', 'anime_tv_shows_folder_name')
        
        config['APPLY_TO_MOVIES'] = get_setting('Subtitle Settings', 'apply_to_movies', True)
        config['APPLY_TO_TV_SHOWS'] = get_setting('Subtitle Settings', 'apply_to_tv_shows', True)
        config['APPLY_TO_ANIME_MOVIES'] = get_setting('Subtitle Settings', 'apply_to_anime_movies', True)
        config['APPLY_TO_ANIME_TV_SHOWS'] = get_setting('Subtitle Settings', 'apply_to_anime_tv_shows', True)
        
        config['OPENSUBTITLES_USERNAME'] = get_setting('Subtitle Settings', 'opensubtitles_username')
        config['OPENSUBTITLES_PASSWORD'] = get_setting('Subtitle Settings', 'opensubtitles_password')
        config['SUBTITLE_LANGUAGES'] = [
            lang.strip() 
            for lang in get_setting('Subtitle Settings', 'subtitle_languages', 'eng,zho').split(',')
            if lang.strip()
        ]
        config['SUBTITLE_PROVIDERS'] = get_setting('Subtitle Settings', 'subtitle_providers', [
            "opensubtitles",
            "opensubtitlescom",
            "podnapisi",
            "tvsubtitles"
        ])
        config['SUBLIMINAL_USER_AGENT'] = get_setting('Subtitle Settings', 'user_agent', 
                                                   'SubDownloader/1.0 (your-email@example.com)')

        # Fetch symlink_folder_order with default
        config['symlink_folder_order_str'] = get_setting('File Management', 'symlink_folder_order', 'type,version,resolution')

        config['symlink_organize_by_type'] = get_setting('File Management', 'symlink_organize_by_type', True)
        config['symlink_organize_by_resolution'] = get_setting('File Management', 'symlink_organize_by_resolution', False)
        config['symlink_organize_by_version'] = get_setting('File Management', 'symlink_organize_by_version', False)

    except Exception as e_fetch:
        # Initialize VIDEO_FOLDERS to empty and return if critical settings fetch fails
        config['VIDEO_FOLDERS'] = []
        for key, value in config.items(): globals()[key] = value
        return config

    generated_video_folders = []
    symlink_root_path_str = config.get('SYMLINKED_PATH')

    if not symlink_root_path_str:
        config['VIDEO_FOLDERS'] = []
        for key, value in config.items(): globals()[key] = value
        return config

    try:
        symlink_root = Path(symlink_root_path_str)
    except TypeError as e_path:
        config['VIDEO_FOLDERS'] = []
        for key, value in config.items(): globals()[key] = value
        return config

    symlink_folder_order_raw = config.get('symlink_folder_order_str')
    if symlink_folder_order_raw is None: # Should have default from get_setting, but being defensive
        symlink_folder_order_raw = 'type,version,resolution'
    
    order_components = [comp.strip().lower() for comp in symlink_folder_order_raw.split(',')]
    
    active_media_type_folders = []
    if config.get('MOVIES_FOLDER') and config.get('APPLY_TO_MOVIES', True): # Use .get for APPLY_TO flags too
        active_media_type_folders.append(config['MOVIES_FOLDER'])
    if config.get('TV_SHOWS_FOLDER') and config.get('APPLY_TO_TV_SHOWS', True):
        active_media_type_folders.append(config['TV_SHOWS_FOLDER'])
    if config['ENABLE_ANIME']:
        if config['ANIME_MOVIES_FOLDER'] and config['APPLY_TO_ANIME_MOVIES']:
            active_media_type_folders.append(config['ANIME_MOVIES_FOLDER'])
        if config['ANIME_TV_SHOWS_FOLDER'] and config['APPLY_TO_ANIME_TV_SHOWS']:
            active_media_type_folders.append(config['ANIME_TV_SHOWS_FOLDER'])

    first_organizing_component = order_components[0] if order_components else 'type'

    if first_organizing_component == 'type' and config.get('symlink_organize_by_type'):
        for media_folder_name in active_media_type_folders:
            potential_path = symlink_root / media_folder_name
            if os.path.isdir(potential_path):
                generated_video_folders.append(str(potential_path))
    elif first_organizing_component == 'resolution' and config.get('symlink_organize_by_resolution'):
        known_resolutions = ["2160p", "1080p", "720p", "SD"] 
        for res_folder_name in known_resolutions:
            potential_path = symlink_root / res_folder_name
            if os.path.isdir(potential_path):
                generated_video_folders.append(str(potential_path))
    elif first_organizing_component == 'version' and config.get('symlink_organize_by_version'):
        if os.path.isdir(symlink_root): # Check if symlink_root itself is a valid directory
             generated_video_folders.append(str(symlink_root))
    else: # Default or fallback: assume type-based organization at root or unrecognized first component
        for media_folder_name in active_media_type_folders:
            potential_path = symlink_root / media_folder_name
            if os.path.isdir(potential_path):
                generated_video_folders.append(str(potential_path))

    
    config['VIDEO_FOLDERS'] = list(set(p for p in generated_video_folders if p))

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
