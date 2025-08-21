# settings_schema.py
import os
import re
import glob
from pathlib import Path

def get_available_logos():
    """
    Scan the static directory to find available logo files and categorize them.
    Returns a list of logo options with the format: ["Default", "Plex-Inspired"].
    """
    # Define the static directory path relative to this file
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    
    # Default logo options (always include these if assets exist)
    logo_options = ["Default", "Plex-Inspired", "Color Icon"]
    
    # Pattern matching for Plex-Inspired logo
    plex_pattern = re.compile(r"plex-icon-\d+x\d+\.(png|ico)$")
    
    # Check if the static directory exists
    if os.path.exists(static_dir):
        # Get all files in the static directory
        files = glob.glob(os.path.join(static_dir, "*.*"))
        
        # Check if Plex logo is available
        for file_path in files:
            filename = os.path.basename(file_path)
            if plex_pattern.search(filename):
                if "Plex-Inspired" not in logo_options:
                    logo_options.append("Plex-Inspired")
                break
    
    return logo_options

# Get available logo options dynamically
AVAILABLE_LOGOS = get_available_logos()

SETTINGS_SCHEMA = {
    "UI Settings": {
        "tab": "Additional Settings",
        "enable_user_system": {
            "type": "boolean",
            "description": "Enable user account system",
            "default": True
        },
        "use_24hour_format": {
            "type": "boolean",
            "description": "Use 24-hour time format instead of 12-hour",
            "default": True
        },
        "compact_view": {
            "type": "boolean",
            "description": "Use compact view for statistics page",
            "default": False
        },
        "enable_phalanx_db": {
            "type": "boolean",
            "description": "Enable the phalanx_db service (requires restart)",
            "default": False
        },
        "disable_auto_browser": {
            "type": "boolean",
            "description": "Disable automatic browser launch on Windows systems",
            "default": False
        },
        "program_logo": {
            "type": "string",
            "description": "Select the program logo to display in the UI. Credits to:@mrcuriousny for Plex-Inspired logo",
            "default": "Default",
            "choices": AVAILABLE_LOGOS
        },
        "hide_support_message": {
            "type": "boolean",
            "description": "Hide the Patreon support message in the header",
            "default": False
        }
    },
    "Plex": {
        "tab": "Required Settings",
        "url": {
            "type": "string",
            "description": "Plex server URL",
            "default": "",
            "validate": "url"
        },
        "token": {
            "type": "string",
            "description": "Plex authentication token",
            "default": "",
            "sensitive": True
        },
        "movie_libraries": {
            "type": "string",
            "description": "Comma-separated list of Plex movie library names",
            "default": ""
        },
        "shows_libraries": {
            "type": "string",
            "description": "Comma-separated list of Plex TV show library names",
            "default": ""
        },
        "update_plex_on_file_discovery": {
            "type": "boolean",
            "description": "Update Plex on file discovery (cli_debrid must be able to access mount at the below location)",
            "default": False
        },
        "mounted_file_location": {
            "type": "string",
            "description": "Mounted file location (in Zurg use the /__all__ folder)",
            "default": "/mnt/zurg/__all__"
        },
        "disable_plex_library_checks": {
            "type": "boolean",
            "description": "Disable Plex library checks - if enabled use the mounted_file_location above to confirm file presence for Collection. If no file location indicated, immediately mark as Collected on addition. This setting is essentially a Local-Only mode, to allow for third party symlinking",
            "default": False
        }
    },
    "File Management": {
        "tab": "Required Settings",
        "file_collection_management": {
            "type": "string",
            "description": "Select library management method. Note: On Windows systems, hardlinks will be used instead of symlinks when selecting Symlinked/Local option.",
            "default": "Plex",
            "choices": ["Plex", "Symlinked/Local"]
        },
        "original_files_path": {
            "type": "string",
            "description": "Path to the original files (in Zurg use the /__all__ folder).",
            "default": "/mnt/zurg/__all__"
        },
        "symlinked_files_path": {
            "type": "string",
            "description": "Path to the destination folder (where you want your files linked to).",
            "default": "/mnt/symlinked"
        },
        "symlink_organize_by_type": {
            "type": "boolean",
            "description": "Organize symlinked files into Movies and TV Shows folders",
            "default": True
        },
        "symlink_organize_by_resolution": {
            "type": "boolean",
            "description": "Organize symlinked files by resolution (e.g., 1080p, 2160p) before media type folders",
            "default": False
        },
        "symlink_organize_by_version": {
            "type": "boolean",
            "description": "Organize symlinked files by version (e.g., Remux, WEB-DL) before media type folders",
            "default": False
        },
        "symlink_folder_order": {
            "type": "string",
            "description": "Defines the customizable order of organizational folders for symlinks. Use a comma-separated list containing 'type', 'version', and 'resolution' in your desired order (e.g., 'version,type,resolution'). The individual 'Organize by X' toggles still control if a specific folder component is included in the path.",
            "default": "type,version,resolution"
        },
        "process_non_checking_items": {
            "type": "boolean",
            "description": "Process files in rclone webhook even if they don't match any items in the checking state",
            "default": False
        },
        "plex_url_for_symlink": {
            "type": "string",
            "description": "Plex server URL for symlink updates (optional)",
            "default": "",
            "validate": "url"
        },
        "plex_token_for_symlink": {
            "type": "string",
            "description": "Plex authentication token (optional)",
            "default": "",
            "sensitive": True
        },
        "media_server_type": {
            "type": "string",
            "description": "Media server type to use for symlink updates when using Symlinked/Local file collection management",
            "default": "plex",
            "choices": ["plex", "jellyfin"]
        }
    },
    "Debrid Provider": {
        "tab": "Required Settings",
        "provider": {
            "type": "string",
            "description": "Debrid service provider",
            "default": "RealDebrid",
            "choices": ["RealDebrid"]
        },
        "api_key": {
            "type": "string",
            "description": "API key for the debrid service",
            "default": "demo_key",
            "sensitive": True
        }
    },
    "TMDB": {
        "tab": "Additional Settings",
        "api_key": {
            "type": "string",
            "description": "TMDB API key - used for Poster URL retrieval (not 'API Read Access Token')",
            "default": "",
            "sensitive": True
        }
    },
    "Staleness Threshold": {
        "tab": "Additional Settings",
        "staleness_threshold": {
            "type": "integer",
            "description": "Staleness threshold for metadata (in days)",
            "default": 7
        }
    },
    "Sync Deletions": {
        "tab": "Additional Settings",
        "sync_deletions": {
            "type": "boolean",
            "description": "[DEPRECATED - Defaults to true] Sync deletions from the Database to Plex",
            "default": False
        }
    },
    "Metadata Battery": {
        "tab": "Required Settings",
        "url": {
            "type": "string",
            "description": "Metadata Battery URL. Leave as default unless you have set up the Metadata Battery in a different location.",
            "default": "http://localhost:50051"
        }
    },
    "Queue": {
        "tab": "Additional Settings",
        "queue_sort_order": {
            "type": "string",
            "description": "Sort order for the scraping queue",
            "default": "None",
            "choices": ["None", "Movies First", "Episodes First"]
        },
        "sort_by_release_date_desc": {
            "type": "boolean",
            "description": "Apply secondary sorting by release date (newest first) after primary sort (content source/type). Items with unknown dates appear last.",
            "default": False
        },
        "content_source_priority": {
            "type": "string",
            "description": "Priority order for content sources in the scraping queue (comma-separated list). Content sources not listed will be processed last.",
            "default": ""
        },
        "wake_limit": {
            "type": "string",
            "description": "Number of times to wake items before blacklisting",
            "default": "24"
        },
        "sleep_duration_minutes": {
             "type": "integer",
             "description": "Duration in minutes an item sleeps before the next wake attempt",
             "default": 30,
             "min": 1
         },
        "blacklist_final_scrape_delay_hours": {
            "type": "integer",
            "description": "Hours to wait before performing one final scrape attempt after an item would normally be blacklisted. Set to 0 to disable.",
            "default": 0,
            "min": 0
        },
        "movie_airtime_offset": {
            "type": "string",
            "description": "Hours after midnight to start scraping for new movies",
            "default": "0"
        },
        "episode_airtime_offset": {
            "type": "string",
            "description": "Offset from the show's airtime to start scraping for new episodes. Positive values are to delay scraping, negative values are to scrape early. Requires Trakt login for accurate airtime, otherwise default of 19:00 will be used.",
            "default": "0"
        },
        "blacklist_duration": {
            "type": "string",
            "description": "Number of days after which to automatically remove blacklisted items for a re-scrape, if enabled",
            "default": "30"
        },
        "enable_pause_schedule": {
            "type": "boolean",
            "description": "Enable pausing the queue during a scheduled time frame",
            "default": False
        },
        "pause_start_time": {
            "type": "string",
            "description": "Start time for scheduled queue pause (HH:MM format)",
            "default": "00:00",
            "validate": "time"  # Assuming a validation function for time exists or will be added
        },
        "pause_end_time": {
            "type": "string",
            "description": "End time for scheduled queue pause (HH:MM format)",
            "default": "00:00",
            "validate": "time"  # Assuming a validation function for time exists or will be added
        },
        "main_loop_sleep_seconds": {
            "type": "float",
            "description": "Amount of time (in seconds) to sleep after each task execution to reduce system load. This enforces a minimum delay between tasks. Default: 0.0 (no delay).",
            "default": 0.0,
            "min": 0.0
        },
                "item_process_delay_seconds": {
             "type": "float",
             "description": "Artificial delay (in seconds) after processing each item in Scraping/Adding queues to reduce peak CPU usage. Default: 0.0 (no delay).",
             "default": 0.0,
             "min": 0.0
         },
        "pre_release_scrape_days": {
            "type": "integer",
            "description": "Number of days before release date to start scraping for movies. For example, setting to 3 will start scraping movies 3 days before their release date. Set to 0 to disable pre-release scraping.",
            "default": 0,
            "min": 0
        }
    },
    "Scraping": {
        "tab": "Versions",
        "uncached_content_handling": {
            "type": "string",
            "description": [
                "Uncached content management in the program queue:",
                "None: Only take the best Cached result",
                #"Hybrid: Take the first best Cached result, and if no Cached results found, take the best Uncached result",
                "Full: Take the best result, whether it's Cached or Uncached"
            ],
            "default": "None",
            "choices": ["None", "Full"]
        },
        "filter_trash_releases": {
            "type": "boolean",
            "description": "Filter out releases marked as trash by the parser. These are typically low-quality or badly formatted releases.",
            "default": True
        },
        "minimum_scrape_score": {
            "type": "float",
            "description": "Minimum calculated score for a scraped result to be considered. Scores are calculated based on version weights. Set to 0.0 to disable this filter (accept any score).",
            "default": 0.0
            # Consider adding min/max if score range is known, otherwise leave open.
        },
        "upgrade_similarity_threshold": {
            "type": "float",
            "description": "Threshold for title similarity when upgrading (0.0 to 1.0). Higher values mean titles must be more different to be considered an upgrade. Default 0.95 means 95% similar.",
            "default": 0.95,
            "min": 0.0,
            "max": 1.0
        },
        "hybrid_mode": {
            "type": "boolean",
            "description": "Enable hybrid mode to add best uncached result if no cached results found in 'None' mode",
            "default": False
        },
        "jackett_seeders_only": {
            "type": "boolean",
            "description": "Return only results with seeders in Jackett",
            "default": False
        },
        "ultimate_sort_order": {
            "type": "dropdown",
            "description": "Ultimate sort order for scraped results. Recommend leaving off and using existing versioning logic",
            "default": "None",
            "choices": ["None", "Size: large to small", "Size: small to large"]
        },
        "soft_max_size_gb": {
            "type": "boolean",
            "description": "If enabled, apply the assigned max size to the scraped results, but if no results are returned accept the smallest result available",
            "default": False
        },
        "enable_upgrading": {
            "type": "boolean",
            "description": "Enable upgrading of items in the queue",
            "default": False
        },
        "upgrading_percentage_threshold": {
            "type": "float",
            "description": "Percentage threshold for upgrading (enter as decimal representation of percentage,0.0 to 1.0). Higher values mean an item's score must be higher than the threshold to be upgraded.",
            "default": 0.1,
            "min": 0.0,
            "max": 1.0
        },
        "enable_upgrading_cleanup": {
            "type": "boolean",
            "description": "Enable cleanup of original items after successful upgrade (removes original item from Debrid Provider)",
            "default": False
        },
        "disable_adult": {
            "type": "boolean",
            "description": "Filter out adult content",
            "default": True
        },
        "trakt_early_releases": {
            "type": "boolean",
            "description": "Check Trakt for early releases",
            "default": False
        },
        "scraper_timeout": {
            "type": "integer",
            "description": "Timeout in seconds for scraping process (0 to disable)",
            "default": 5,
            "min": 0
        },
        "versions": {
            "type": "dict",
            "description": "Scraping versions configuration",
            "default": {},
            "schema": {
                "max_resolution": {
                    "type": "string",
                    "choices": ["2160p", "1080p", "720p", "SD"],
                    "default": "1080p"
                },
                "resolution_wanted": {
                    "type": "string",
                    "choices": ["<=", "==", ">="],
                    "default": "=="
                },
                "enable_hdr": {
                    "type": "boolean",
                    "default": False
                },
                "hdr_weight": {
                    "type": "number",
                    "default": 1.0
                },
                "min_size_gb": {
                    "type": "number",
                    "default": 0.0
                },
                "max_size_gb": {
                    "type": "number",
                    "default": float('inf')
                },
                "min_bitrate_mbps": {
                    "type": "number",
                    "default": 0.01
                },
                "max_bitrate_mbps": {
                    "type": "number",
                    "default": float('inf')
                },
                "resolution_weight": {
                    "type": "number",
                    "default": 1.0
                },
                "similarity_weight": {
                    "type": "number",
                    "default": 1.0
                },
                "similarity_threshold": {
                    "type": "number",
                    "default": 0.85
                },
                "similarity_threshold_anime": {
                    "type": "number",
                    "default": 0.80
                },
                "size_weight": {
                    "type": "number",
                    "default": 1.0
                },
                "bitrate_weight": {
                    "type": "number",
                    "default": 1.0
                },
                "year_match_weight": {
                    "type": "number",
                    "default": 3
                },
                "wake_count": {
                    "type": "integer",
                    "default": None,
                    "description": "Override global wake count limit. Leave empty to use global setting. Set to -1 to disable sleeping queue."
                },
                "fallback_version": {
                    "type": "string",
                    "description": "Version to fall back to if the current version fails and the item is blacklisted. Select 'None' to disable fallback.",
                    "default": "None"
                },
                "anime_filter_mode": {
                    "type": "string",
                    "description": "Filter for anime content: 'None' (no filter), 'Anime Only', 'Non-Anime Only'.",
                    "default": "None",
                    "choices": ["None", "Anime Only", "Non-Anime Only"]
                },
                "filter_in": {
                    "type": "list",
                    "default": []
                },
                "filter_out": {
                    "type": "list",
                    "default": []
                },
                "preferred_filter_in": {
                    "type": "list",
                    "default": []
                },
                "preferred_filter_out": {
                    "type": "list",
                    "default": []
                },
                "require_physical_release": {
                    "type": "boolean",
                    "default": False
                },
                "language_code": {
                    "type": "string",
                    "default": "en",
                    "description": "Preferred language code (ISO 639-1) for metadata like titles."
                }
            }
        },
        "accept_uncached_within_hours": {
            "type": "integer",
            "description": "If an item was released within the last X hours, accept uncached releases. Set to 0 to disable.",
            "default": 0,
            "min": 0
        }
    },
    "Trakt": {
        "tab": "Required Settings",
        "client_id": {
            "type": "string",
            "description": "Trakt client ID",
            "default": "",
            "sensitive": True
        },
        "client_secret": {
            "type": "string",
            "description": "Trakt client secret",
            "default": "",
            "sensitive": True
        }
    },
    "Debug": {
        "tab": "Debug Settings",
        "logging_level": {
            "type": "string",
            "description": "Logging level for console output and file logging",
            "default": "DEBUG",
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        },
        "skip_initial_plex_update": {
            "type": "boolean",
            "description": "Skip Plex initial collection scan",
            "default": False
        },
        "disable_unblacklisting": {
            "type": "boolean",
            "description": "Disable automatic unblacklisting of items from the blacklisted queue",
            "default": True
        },
        "auto_run_program": {
            "type": "boolean",
            "description": "Run the program automatically on startup",
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
        "sort_by_uncached_status": {
            "type": "boolean",
            "description": "Sort results by uncached status over cached status",
            "default": False
        },
        "enable_plex_removal_caching": {
            "type": "boolean",
            "description": "Enable caching of Plex removal operations before executing them",
            "default": True
        },
        "content_source_check_period": {
            "type": "dict",
            "description": "Override Content Source checking period (in minutes) - note that a minimum of 5 minutes is recommended",
            "default": {},
            "schema": {
                "*": {"type": "integer", "min": 1}
            }
        },
        "checking_queue_period": {
            "type": "integer",
            "description": "Checking queue max period (in seconds) before moving items back to Wanted queue",
            "default": 3600
        },
        "rescrape_missing_files": {
            "type": "boolean",
            "description": "[DEPRECATED - Handled through library maintenance task] Rescrape items that are missing their associated file (i.e. if Plex Library cleanup is enabled)",
            "default": False
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
        "plex_watchlist_removal": {
            "type": "boolean",
            "description": "Remove items from Plex Watchlist when they have been collected (only works with My Plex Watchlist and Other Plex Watchlist sources)",
            "default": False
        },
        "plex_watchlist_keep_series": {
            "type": "boolean",
            "description": "Keep series in Plex Watchlist when they have been collected, only delete movies",
            "default": False
        },
        "trakt_watchlist_removal": {
            "type": "boolean",
            "description": "Remove items from Trakt Watchlist when they have been collected",
            "default": False
        },
        "trakt_watchlist_keep_series": {
            "type": "boolean",
            "description": "Keep series in Trakt Watchlist when they have been collected, only delete movies",
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
                "Example: {title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})",
            ],
            "default": "{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version} - ({original_filename})"
        },
        "allow_partial_overseerr_requests": {
            "type": "boolean",
            "description": "Allow partial show requests from Overseerr",
            "default": False
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
        "anime_renaming_using_anidb": {
            "type": "boolean",
            "description": "Use AniDB to rename anime episodes instead of Trakt metadata (symlinking only)",
            "default": False
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
        "enable_crash_test": {
            "type": "boolean",
            "description": "Enable crash test",
            "default": False
        },
        "enable_library_maintenance_task": {
            "type": "boolean",
            "description": "Enable library maintenance task to run periodically. This is a destructive process and should be used with caution",
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
        "apply_to_anime_tv_shows": {
            "type": "boolean",
            "description": "Apply subtitle downloads to anime TV show folders (if separate anime folders are enabled)",
            "default": True
        },
        "apply_to_documentary_movies": {
            "type": "boolean",
            "description": "Apply subtitle downloads to documentary movie folders (if separate documentary folders are enabled)",
            "default": True
        },
        "apply_to_documentary_tv_shows": {
            "type": "boolean",
            "description": "Apply subtitle downloads to documentary TV show folders (if separate documentary folders are enabled)",
            "default": True
        },
        "only_current_file": {
            "type": "boolean",
            "description": "Only download subtitles for the current file being processed (instead of scanning all folders)",
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
    "Scrapers": {
        "tab": "Scrapers",
        "type": "dict",
        "description": "Scraper configurations",
        "default": {},
        "schema": {
            "Zilean": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"}
            },
            "Jackett": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api": {"type": "string", "default": "", "sensitive": True},
                "enabled_indexers": {"type": "string", "default": ""}
            },
            "Prowlarr": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api": {"type": "string", "default": "", "sensitive": True},
                "tags": {
                    "type": "string",
                    "default": "",
                    "description": "Comma-separated list of numeric Prowlarr Indexer IDs. If provided, searches through this Prowlarr instance will only use these specified indexers."
                }
            },
            "Torrentio": {
                "enabled": {"type": "boolean", "default": False},
                "opts": {"type": "string", "default": ""}
            },
            "Nyaa": {
                "enabled": {"type": "boolean", "default": False}
            },
            "OldNyaa": {
                "enabled": {"type": "boolean", "default": False}
            },
            "MediaFusion": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
            }
        }
    },
    "Content Sources": {
        "tab": "Content Sources",
        "type": "dict",
        "description": "Content source configurations",
        "default": {},
        "schema": {
            "MDBList": {
                "enabled": {"type": "boolean", "default": False},
                "urls": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "MDBList"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Collected": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Collected"},
                "monitor_mode": {
                    "type": "string",
                    "description": [
                        "Controls which episodes are monitored for collection:",
                        "'Monitor All Episodes' - All episodes are monitored (default, current behavior).",
                        "'Monitor Future Episodes' - Only episodes with a release date after the show is added are monitored.",
                        "'Monitor Recent (90 Days) and Future' - Only episodes released in the last 90 days and all future episodes are monitored."
                    ],
                    "default": "Monitor All Episodes",
                    "choices": [
                        "Monitor All Episodes",
                        "Monitor Future Episodes",
                        "Monitor Recent (90 Days) and Future"
                    ]
                },
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Trakt Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Trakt Lists": {
                "enabled": {"type": "boolean", "default": False},
                "trakt_lists": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Lists"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Trakt Collection": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Collection"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Overseerr": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api_key": {"type": "string", "default": "", "sensitive": True},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Overseerr"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "ignore_tags": {
                    "type": "string",
                    "description": "Comma-separated list of Overseerr/Jellyseerr tags. If an item has any of these tags, it will be ignored.",
                    "default": ""
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "My Plex Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Plex Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Other Plex Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "username": {"type": "string", "default": ""},
                "token": {"type": "string", "default": "", "sensitive": True},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Other Plex Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "My Plex RSS Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Plex RSS Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "My Friends Plex RSS Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Friends Plex RSS Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            },
            "Friends Trakt Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "auth_id": {"type": "string", "default": ""},
                "username": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Friend's Trakt Watchlist"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                }
            },
            "Special Trakt Lists": {
                "enabled": {"type": "boolean", "default": False},
                "special_list_type": {
                    "type": "list",
                    "default": [],
                    "choices": [
                        "Trending", 
                        "Popular", 
                        "Favorited", 
                        "Played", 
                        "Watched", 
                        "Collected", 
                        "Anticipated", 
                        "Box Office"
                    ],
                    "description": "Select the type(s) of special Trakt list. 'Box Office' applies to Movies only."
                },
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {
                    "type": "string", 
                    "default": "All", 
                    "choices": ["All", "Movies", "Shows"],
                    "description": "Select media type. Note: 'Box Office' special list type is only applicable to Movies."
                },
                "display_name": {"type": "string", "default": "Special Trakt Lists"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Only process content with a release date greater than this date (YYYY-MM-DD format) or within the last X days (e.g., '30' for 30 days ago). Leave empty to process all content.",
                    "default": ""
                },
                "exclude_genres": {
                    "type": "list",
                    "description": "List of genres to exclude from this content source. Items with any of these genres will be skipped during content processing.",
                    "default": []
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                }
            }
        }
    },
    "Notifications": {
        "tab": "Notifications",
        "type": "dict",
        "description": "Notification configurations",
        "default": {},
        "schema": {
            "General": {
                "enabled_content_sources": {
                    "type": "string",
                    "description": "Comma-separated list of content sources to include in notifications. Leave empty to include all sources.",
                    "default": ""
                }
            },
            "Telegram": {
                "enabled": {"type": "boolean", "default": False},
                "bot_token": {"type": "string", "default": "", "sensitive": True},
                "chat_id": {"type": "string", "default": ""},
                "notify_on": {
                    "type": "dict",
                    "default": {
                        "collected": True,
                        "wanted": False,
                        "scraping": False,
                        "adding": False,
                        "checking": False,
                        "sleeping": False,
                        "unreleased": False,
                        "blacklisted": False,
                        "pending_uncached": False,
                        "upgrading": False,
                        "program_stop": True,
                        "program_crash": True,
                        "program_start": True,
                        "program_pause": True,
                        "program_resume": True
                    },
                    "description": "Configure which queue state changes trigger notifications"
                }
            },
            "Discord": {
                "enabled": {"type": "boolean", "default": False},
                "webhook_url": {"type": "string", "default": "", "sensitive": True},
                "notify_on": {
                    "type": "dict",
                    "default": {
                        "collected": True,
                        "wanted": False,
                        "scraping": False,
                        "adding": False,
                        "checking": False,
                        "sleeping": False,
                        "unreleased": False,
                        "blacklisted": False,
                        "pending_uncached": False,
                        "upgrading": False,
                        "program_stop": True,
                        "program_crash": True,
                        "program_start": True,
                        "queue_pause": True,
                        "queue_resume": True,
                        "queue_start": True,
                        "queue_stop": True
                    },
                    "description": "Configure which queue state changes trigger notifications"
                }
            },
            "NTFY": {
                "enabled": {"type": "boolean", "default": False},
                "host": {"type": "string", "default": "", "sensitive": True},
                "topic": {"type": "string", "default": "", "sensitive": True},
                "api_key": {"type": "string", "default": ""},
                "priority": {"type": "string", "default": ""},
                "notify_on": {
                    "type": "dict",
                    "default": {
                        "collected": True,
                        "wanted": False,
                        "scraping": False,
                        "adding": False,
                        "checking": False,
                        "sleeping": False,
                        "unreleased": False,
                        "blacklisted": False,
                        "pending_uncached": False,
                        "upgrading": False,
                        "program_stop": True,
                        "program_crash": True,
                        "program_start": True,
                        "queue_pause": True,
                        "queue_resume": True,
                        "queue_start": True,
                        "queue_stop": True
                    },
                    "description": "Configure which queue state changes trigger notifications"
                }
            },
            "Email": {
                "enabled": {"type": "boolean", "default": False},
                "smtp_server": {"type": "string", "default": ""},
                "smtp_port": {"type": "integer", "default": 587},
                "smtp_username": {"type": "string", "default": ""},
                "smtp_password": {"type": "string", "default": "", "sensitive": True},
                "from_address": {"type": "string", "default": ""},
                "to_address": {"type": "string", "default": ""},
                "notify_on": {
                    "type": "dict",
                    "default": {
                        "collected": True,
                        "wanted": False,
                        "scraping": False,
                        "adding": False,
                        "checking": False,
                        "sleeping": False,
                        "unreleased": False,
                        "blacklisted": False,
                        "pending_uncached": False,
                        "upgrading": False,
                        "program_stop": True,
                        "program_crash": True,
                        "program_start": True,
                        "queue_pause": True,
                        "queue_resume": True,
                        "queue_start": True,
                        "queue_stop": True
                    },
                    "description": "Configure which queue state changes trigger notifications"
                }
            }
        }
    },
    "Reverse Parser": {
        "tab": "Reverse Parser",
        "version_terms": {
            "type": "dict",
            "description": "Version terms for reverse parsing",
            "default": {}
        },
        "default_version": {
            "type": "string",
            "description": "Default version for reverse parsing if no other version is selected",
            "default": ""
        }
    },
    "Subtitle Settings": {
        "tab": "Additional Settings",
        "enable_subtitles": {
            "type": "boolean",
            "description": "Enable automatic subtitle downloading for media files using 'downsub'. Works for both movies and TV shows.",
            "default": False
        },
        "apply_to_movies": {
            "type": "boolean",
            "description": "Apply subtitle downloads to movie folders",
            "default": True
        },
        "apply_to_tv_shows": {
            "type": "boolean",
            "description": "Apply subtitle downloads to TV show folders",
            "default": True
        },
        "apply_to_anime_movies": {
            "type": "boolean",
            "description": "Apply subtitle downloads to anime movie folders (if separate anime folders are enabled)",
            "default": True
        },
        "apply_to_anime_tv_shows": {
            "type": "boolean",
            "description": "Apply subtitle downloads to anime TV show folders (if separate anime folders are enabled)",
            "default": True
        },
        "apply_to_documentary_movies": {
            "type": "boolean",
            "description": "Apply subtitle downloads to documentary movie folders (if separate documentary folders are enabled)",
            "default": True
        },
        "apply_to_documentary_tv_shows": {
            "type": "boolean",
            "description": "Apply subtitle downloads to documentary TV show folders (if separate documentary folders are enabled)",
            "default": True
        },
        "only_current_file": {
            "type": "boolean",
            "description": "Only download subtitles for the current file being processed (instead of scanning all folders)",
            "default": False
        },
        "opensubtitles_username": {
            "type": "string",
            "description": "OpenSubtitles username for subtitle downloads",
            "default": "",
            "sensitive": False
        },
        "opensubtitles_password": {
            "type": "string",
            "description": "OpenSubtitles password for subtitle downloads",
            "default": "",
            "sensitive": True
        },
        "subtitle_languages": {
            "type": "string",
            "description": "Comma-separated list of language codes (e.g., eng,zho,spa). Uses ISO-639-3 codes.",
            "default": "eng,zho"
        },
        "subtitle_providers": {
            "type": "list",
            "description": "Select subtitle providers to use",
            "default": ["opensubtitles", "opensubtitlescom", "podnapisi", "tvsubtitles"],
            "choices": ["opensubtitles", "opensubtitlescom", "podnapisi", "tvsubtitles"]
        },
        "user_agent": {
            "type": "string",
            "description": "User agent for subtitle API requests",
            "default": "SubDownloader/1.0 (your-email@example.com)"
        }
    },
    "Custom Post-Processing": {
        "tab": "Additional Settings",
        "enable_custom_script": {
            "type": "boolean",
            "description": "Enable custom post-processing script",
            "default": False
        },
        "custom_script_path": {
            "type": "string",
            "description": "Absolute path to your custom post-processing script",
            "default": ""
        },
        "custom_script_args": {
            "type": "string",
            "description": "Arguments template for the script. Available variables: {title}, {year}, {type}, {imdb_id}, {location_on_disk}, {original_path_for_symlink}, {state}, {version}",
            "default": "{title} {imdb_id}"
        }
    },
    "System Load Regulation": {
        "tab": "Additional Settings",
        "cpu_threshold_percent": {
            "type": "integer",
            "description": "CPU usage percentage threshold to trigger an increase in sleep time.",
            "default": 75,
            "min": 1,
            "max": 100
        },
        "ram_threshold_percent": {
            "type": "integer",
            "description": "RAM usage percentage threshold to trigger an increase in sleep time.",
            "default": 75,
            "min": 1,
            "max": 100
        },
        "regulation_increase_step_seconds": {
            "type": "float",
            "description": "Amount of time (in seconds) to increase the sleep duration by when load is high.",
            "default": 1.0,
            "min": 0.0,
            "step": 0.1
        },
        "regulation_decrease_step_seconds": {
            "type": "float",
            "description": "Amount of time (in seconds) to decrease the sleep duration by when load is normal.",
            "default": 1.0,
            "min": 0.0,
            "step": 0.1
        },
        "regulation_max_sleep_seconds": {
            "type": "float",
            "description": "The maximum sleep time (in seconds) that auto-regulation can set.",
            "default": 60.0,
            "min": 0.0,
            "step": 0.1
        }
    }
}
