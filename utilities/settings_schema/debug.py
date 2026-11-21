"""Debug / advanced settings."""

SECTIONS = {
    "Debug": {
        "tab": "Debug Settings",
        "logging_level": {
            "type": "string",
            "description": "Logging level for console output and file logging",
            "default": "DEBUG",
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        },
        "disable_unblacklisting": {
            "type": "boolean",
            "description": "Disable automatic unblacklisting of items from the blacklisted queue",
            "default": True
        },
        "auto_run_program": {
            "type": "boolean",
            "description": "Run the program automatically on startup. With the built-in mount in use, this also starts the mount, even if it was stopped on the Mount page.",
            "default": False
        },
        "disable_initialization": {
            "type": "boolean",
            "description": "Disable initialization tasks",
            "default": False
        },
        "use_symlinks_on_windows": {
            "type": "boolean",
            "description": "Allow the use of symlinks on Windows. WARNING: Creating symlinks on Windows requires administrator privileges or Developer Mode to be enabled.",
            "default": False
        },
        "enable_plex_removal_caching": {
            "type": "boolean",
            "description": "Enable caching of Plex removal operations before executing them",
            "default": True
        },
        "checking_queue_period": {
            "type": "integer",
            "description": "Checking queue max period (in seconds) before moving items back to Wanted queue",
            "default": 3600
        },
        "enable_reverse_order_scraping": {
            "type": "boolean",
            "description": "Enable reverse order scraping",
            "default": False
        },
        "disable_not_wanted_check": {
            "type": "boolean",
            "description": "Disable the not wanted check for items in the queue",
            "default": False
        },
        "symlink_movie_template": {
            "type": "string",
            "description": [
                "Template for movie symlink names. Available variables: {title}, {year}, {imdb_id}, {tmdb_id}, {quality}, {original_filename}",
                "Example: {title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})",
            ],
            "default": "{title} ({year})/{title} ({year}) - {imdb_id} - {version} - ({original_filename})"
        },
        "symlink_episode_template": {
            "type": "string",
            "description": [
                "Template for episode symlink names. Available variables: {title}, {year}, {imdb_id}, {tmdb_id}, {season_number}, {episode_number}, {episode_title}, {version}, {original_filename}",
                "For multi-episode files, {episode_number} will be formatted as 'E17-E18' instead of a single number.",
                "Example: {title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})",
            ],
            "default": "{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})"
        },
        "timezone_override": {
            "type": "string",
            "description": "Override system timezone (e.g. 'America/New_York', 'Europe/London'). Leave empty to use system timezone.",
            "default": ""
        },
        "filename_filter_out_list": {
            "type": "string",
            "description": "List of filenames or folder names to filter out, comma separated",
            "default": ""
        },
        "enable_separate_anime_folders": {
            "type": "boolean",
            "description": "Create separate anime folders for anime content when organizing symlinks",
            "default": False
        },
        "enable_separate_documentary_folders": {
            "type": "boolean",
            "description": "Create separate documentary folders for documentary content when organizing symlinks",
            "default": False
        },
        "movies_folder_name": {
            "type": "string",
            "description": "Custom name for the Movies folder (default: 'Movies')",
            "default": "Movies"
        },
        "tv_shows_folder_name": {
            "type": "string",
            "description": "Custom name for the TV Shows folder (default: 'TV Shows')",
            "default": "TV Shows"
        },
        "anime_movies_folder_name": {
            "type": "string",
            "description": "Custom name for the Anime Movies folder (default: 'Anime Movies')",
            "default": "Anime Movies"
        },
        "anime_tv_shows_folder_name": {
            "type": "string",
            "description": "Custom name for the Anime TV Shows folder (default: 'Anime TV Shows')",
            "default": "Anime TV Shows"
        },
        "documentary_movies_folder_name": {
            "type": "string",
            "description": "Custom name for the Documentary Movies folder (default: 'Documentary Movies')",
            "default": "Documentary Movies"
        },
        "documentary_tv_shows_folder_name": {
            "type": "string",
            "description": "Custom name for the Documentary TV Shows folder (default: 'Documentary TV Shows')",
            "default": "Documentary TV Shows"
        },
        "check_for_updates": {
            "type": "boolean",
            "description": "Check for updates and display update indicator in header",
            "default": True
        },
        "disable_content_source_caching": {
            "type": "boolean",
            "description": "Disable content source caching",
            "default": False
        },
        "do_not_add_plex_watch_history_items_to_queue": {
            "type": "boolean",
            "description": "Do not add Plex watch history items to queue",
            "default": False
        },
        "enable_detailed_notification_information": {
            "type": "boolean",
            "description": "Enable detailed information in notifications including content source and content source details",
            "default": False
        },
        "enable_granular_version_additions": {
            "type": "boolean",
            "description": "Enable granular version additions for Wanted items",
            "default": True
        },
        "enable_unmatched_items_check": {
            "type": "boolean",
            "description": "Enable checking and fixing of unmatched or incorrectly matched items in Plex during collection scans",
            "default": True
        },
        "nas_paths": {
            "type": "list",
            "description": "List of path prefixes that identify NAS or network drive locations (e.g. /MySamsungNAS_Movies/). Used to detect and optionally filter NAS items in Debrid Manager and other tools. Falls back to smart detection if not configured.",
            "default": []
        },
        "ignore_wanted_queue_throttling": {
            "type": "boolean",
            "description": "Ignore Wanted Queue throttling limits (WANTED_THROTTLE_SCRAPING_SIZE and SCRAPING_QUEUE_MAX_SIZE). Allows Wanted queue to move all eligible items to Scraping regardless of Scraping queue size. USE WITH CAUTION.",
            "default": False
        },
        "upgrade_queue_duration_hours": {
            "type": "integer",
            "description": "Duration in hours to keep items in the upgrade queue before moving them to Collected state (default: 24)",
            "default": 24,
            "min": 1
        },
        "cinesync_path": {
            "type": "string",
            "description": "Absolute path to your CineSync MediaHub main.py file (e.g. /path/to/CineSync/MediaHub/main.py)",
            "default": ""
        },
        "emby_jellyfin_url": {
            "type": "string",
            "description": "Emby or Jellyfin server URL for library updates (e.g. http://localhost:8096)",
            "default": "",
            "validate": "url"
        },
        "emby_jellyfin_token": {
            "type": "string",
            "description": "Emby or Jellyfin API key/token for authentication",
            "default": "",
            "sensitive": True
        },
        "enable_tracemalloc": {
            "type": "boolean",
            "description": "Enable Python's tracemalloc for detailed memory usage tracking per task. Adds overhead, use only for debugging memory leaks.",
            "default": False
        },
        "tracemalloc_sample_rate": {
            "type": "integer",
            "description": "Sample rate for tracemalloc (1 in X tasks). Lower values give more frequent data but increase overhead significantly. Default: 100.",
            "default": 100,
            "min": 1
        },
        "plex_removal_cache_delay_minutes": {
            "type": "integer",
            "description": "Delay in minutes before processing a cached Plex removal operation. Default: 360 (6 hours).",
            "default": 360,
            "min": 1
        },
        "emphasize_number_of_items_over_quality": {
            "type": "boolean",
            "description": "Emphasize the number of items over quality when ranking results",
            "default": True
        },
        "truncate_episode_notifications": {
            "type": "boolean",
            "description": "Truncate episode notifications to show only the first episode and a summary of the rest.",
            "default": False
        },
        "sanitizer_replacement_character": {
            "type": "string",
            "description": "Character to use when replacing invalid characters in filenames (default: '_'). Must be a valid character for both Windows and Linux filesystems.",
            "default": "_",
            "validate": "filesystem_char"
        },
        "max_upgrading_score": {
            "type": "float",
            "description": "Maximum allowed upgrading score. Upgrades will be disabled once this score is reached. Set to 0 to disable this limit.",
            "default": 0.0
        },
        "delayed_scrape_based_on_score": {
            "type": "boolean",
            "description": "If enabled, only accept results above the minimum scrape score for a limited period before accepting lower scored releases.",
            "default": False
        },
        "delayed_scrape_time_limit": {
            "type": "float",
            "description": "Time limit (in hours) to only accept results above the minimum scrape score before accepting lower scored releases.",
            "default": 6.0,
            "min": 0.1
        },
        "minimum_scrape_score": {
            "type": "float",
            "description": "Minimum scrape score to accept results above.",
            "default": 0.0,
            "min": 0.0
        },
        "scale_final_scores": {
            "type": "boolean",
            "description": "Scale final scores to a range of 0-100",
            "default": False
        },
        "use_alternate_scrape_time_strategy": {
            "type": "boolean",
            "description": "Enable alternate scraping time strategy: Instead of scraping based on queue offsets/airtime/release date, scrape all items with release dates and airtimes within the past 24 hours of the user-identified time each day.",
            "default": False
        },
        "alternate_scrape_time_24h": {
            "type": "string",
            "description": "24-hour time (HH:MM) to use as the daily scrape time for the alternate scraping strategy. Only used if alternate strategy is enabled.",
            "default": "00:00",
            "validate": "time"
        },
        "skip_initial_multi_scrape_for_new_content": {
            "type": "boolean",
            "description": "Skip the initial multi-provider scrape for new content (released within the past 7 days).",
            "default": False
        },
        "unblacklisting_cutoff_date": {
            "type": "string",
            "description": "Only unblacklist items with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all blacklisted items for unblacklisting.",
            "default": ""
        }
    },
}
