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
    raise  # Re-raise the exception as this is critical

class Config:
    """
    Configuration class that fetches settings once upon initialization.
    This improves performance by avoiding repeated disk access for settings.
    """
    def __init__(self):
        # Constants that don't need frequent reloading
        self.USER_LOGS = os.environ.get('USER_LOGS', '/user/logs')
        self.USER_DB_CONTENT = os.environ.get('USER_DB_CONTENT', '/user/db_content')
        
        self.CACHE_DIR = os.path.join(self.USER_DB_CONTENT, 'subtitle_cache')
        self.SCAN_CACHE_FILE = os.path.join(self.CACHE_DIR, 'scan_cache.json')
        self.DIR_CACHE_FILE = os.path.join(self.CACHE_DIR, 'dir_cache.json')
        self.LOG_FILE = os.path.join(self.USER_LOGS, 'subtitle_downloader.log')
        
        self.LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
        self.LOG_LEVEL = "INFO"  # Logging level is typically set at startup
        self.VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov")

        os.makedirs(self.CACHE_DIR, exist_ok=True)

        # Load settings initially
        self._load_settings()

    def _load_settings(self):
        """Load all dynamic settings from disk. Called during init and reload."""
        self.SUBTITLES_ENABLED = get_setting('Subtitle Settings', 'enable_subtitles', False)
        self.ONLY_CURRENT_FILE = get_setting('Subtitle Settings', 'only_current_file', False)
        self.SYMLINKED_PATH = get_setting('File Management', 'symlinked_files_path')
        self.MOVIES_FOLDER = get_setting('Debug', 'movies_folder_name')
        self.TV_SHOWS_FOLDER = get_setting('Debug', 'tv_shows_folder_name')
        self.ENABLE_ANIME = get_setting('Debug', 'enable_separate_anime_folders', False)
        self.ANIME_MOVIES_FOLDER = get_setting('Debug', 'anime_movies_folder_name')
        self.ANIME_TV_SHOWS_FOLDER = get_setting('Debug', 'anime_tv_shows_folder_name')
        self.APPLY_TO_MOVIES = get_setting('Subtitle Settings', 'apply_to_movies', True)
        self.APPLY_TO_TV_SHOWS = get_setting('Subtitle Settings', 'apply_to_tv_shows', True)
        self.APPLY_TO_ANIME_MOVIES = get_setting('Subtitle Settings', 'apply_to_anime_movies', True)
        self.APPLY_TO_ANIME_TV_SHOWS = get_setting('Subtitle Settings', 'apply_to_anime_tv_shows', True)
        self.OPENSUBTITLES_USERNAME = get_setting('Subtitle Settings', 'opensubtitles_username')
        self.OPENSUBTITLES_PASSWORD = get_setting('Subtitle Settings', 'opensubtitles_password')

        languages_str = get_setting('Subtitle Settings', 'subtitle_languages', 'eng,zho')
        self.SUBTITLE_LANGUAGES = [lang.strip() for lang in languages_str.split(',') if lang.strip()]

        self.SUBTITLE_PROVIDERS = get_setting('Subtitle Settings', 'subtitle_providers', ["opensubtitles", "opensubtitlescom", "podnapisi", "tvsubtitles"])
        self.SUBLIMINAL_USER_AGENT = get_setting('Subtitle Settings', 'user_agent', 'SubDownloader/1.0 (your-email@example.com)')

        self.symlink_folder_order_str = get_setting('File Management', 'symlink_folder_order', 'type,version,resolution')
        self.symlink_organize_by_type = get_setting('File Management', 'symlink_organize_by_type', True)
        self.symlink_organize_by_resolution = get_setting('File Management', 'symlink_organize_by_resolution', False)
        self.symlink_organize_by_version = get_setting('File Management', 'symlink_organize_by_version', False)

        self.VIDEO_FOLDERS = self._generate_video_folders()

    def reload(self):
        """Reload all settings from disk. Call this to pick up configuration changes."""
        self._load_settings()

    def _generate_video_folders(self):
        """Generates the list of video folders based on current settings."""
        generated_video_folders = []
        symlink_root_path_str = self.SYMLINKED_PATH

        if not symlink_root_path_str:
            return []

        try:
            symlink_root = Path(symlink_root_path_str)
        except TypeError:
            return []

        symlink_folder_order_raw = self.symlink_folder_order_str or 'type,version,resolution'
        order_components = [comp.strip().lower() for comp in symlink_folder_order_raw.split(',')]
        
        active_media_type_folders = []
        if self.MOVIES_FOLDER and self.APPLY_TO_MOVIES:
            active_media_type_folders.append(self.MOVIES_FOLDER)
        if self.TV_SHOWS_FOLDER and self.APPLY_TO_TV_SHOWS:
            active_media_type_folders.append(self.TV_SHOWS_FOLDER)
        if self.ENABLE_ANIME:
            if self.ANIME_MOVIES_FOLDER and self.APPLY_TO_ANIME_MOVIES:
                active_media_type_folders.append(self.ANIME_MOVIES_FOLDER)
            if self.ANIME_TV_SHOWS_FOLDER and self.APPLY_TO_ANIME_TV_SHOWS:
                active_media_type_folders.append(self.ANIME_TV_SHOWS_FOLDER)

        first_organizing_component = order_components[0] if order_components else 'type'

        if first_organizing_component == 'type' and self.symlink_organize_by_type:
            for media_folder_name in active_media_type_folders:
                potential_path = symlink_root / media_folder_name
                if os.path.isdir(potential_path):
                    generated_video_folders.append(str(potential_path))
        elif first_organizing_component == 'resolution' and self.symlink_organize_by_resolution:
            known_resolutions = ["2160p", "1080p", "720p", "SD"]
            for res_folder_name in known_resolutions:
                potential_path = symlink_root / res_folder_name
                if os.path.isdir(potential_path):
                    generated_video_folders.append(str(potential_path))
        elif first_organizing_component == 'version' and self.symlink_organize_by_version:
            if os.path.isdir(symlink_root):
                generated_video_folders.append(str(symlink_root))
        else:
            for media_folder_name in active_media_type_folders:
                potential_path = symlink_root / media_folder_name
                if os.path.isdir(potential_path):
                    generated_video_folders.append(str(potential_path))
        
        return list(set(p for p in generated_video_folders if p))

config = Config()