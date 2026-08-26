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
    "SSO": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable SSO / OIDC login",
            "default": False
        },
        "provider": {
            "type": "string",
            "description": "OIDC provider type",
            "default": "authentik",
            "choices": ["authentik", "authelia", "generic"]
        },
        "discovery_url": {
            "type": "string",
            "description": "OIDC discovery URL (e.g. https://auth.example.com/application/o/cli-debrid/.well-known/openid-configuration)",
            "default": ""
        },
        "client_id": {
            "type": "string",
            "description": "OIDC client ID",
            "default": ""
        },
        "client_secret": {
            "type": "string",
            "description": "OIDC client secret",
            "default": ""
        },
        "default_role": {
            "type": "string",
            "description": "Default role for new SSO users",
            "default": "user",
            "choices": ["user", "requester", "admin"]
        },
        "auto_provision": {
            "type": "boolean",
            "description": "Automatically create accounts for new SSO users",
            "default": True
        },
        "redirect_uri_base": {
            "type": "string",
            "description": "Public base URL for the OIDC callback (e.g. https://cli.mash2k3.us). Leave blank to auto-detect from request.",
            "default": ""
        },
        "disable_local_auth": {
            "type": "boolean",
            "description": "Disable local username/password login (SSO only)",
            "default": False
        }
    },
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
        },
        "recently_added_limit": {
             "type": "integer",
             "description": "Amount of items displayed for recently added on statistics page",
             "default": 5,
             "min": 5,
             "max": 14
         },
        "recently_upgraded_limit": {
             "type": "integer",
             "description": "Amount of items displayed for recently upgraded on statistics page",
             "default": 5,
             "min": 5,
             "max": 14
         },
        "upcoming_releases_start_limit": {
             "type": "integer",
             "description": "How many days back should be displayed on statistics page, 0 being today and 7 being a week back.",
             "default": 0,
             "min": 0
         },
        "upcoming_releases_end_limit": {
             "type": "integer",
             "description": "How many days forward should be displayed on statistics page",
             "default": 28,
             "min": 0
         },
        "date_format": {
             "type": "string",
             "description": "Set your preferred date format. <a href='#' class='format-help-link' data-modal='dateTimeFormatModal'>Click here</a> to see the parameter list.",
             "default": "%Y-%m-%d"
         },
        "time_format": {
             "type": "string",
             "description": "Set your preferred time format. <a href='#' class='format-help-link' data-modal='dateTimeFormatModal'>Click here</a> to see the parameter list.",
             "default": "%H:%M:%S"
         },
        "enable_caching": {
            "type": "boolean",
            "description": "Enable web caching and compression for faster page loads. This improves performance by caching static assets (CSS, JavaScript, images) and compressing responses. Disable if experiencing issues with updates not appearing.",
            "default": False
        },
        "stats_provider_priority": {
            "type": "string",
            "description": "Choose which provider's stats to display on the Statistics page. Auto uses debrid if configured, otherwise usenet. Select Usenet to always show cli_mount/usenet stats even when a debrid provider is also configured. Select Combined to show the total size and broken count across every debrid provider and usenet together (no subscription days, since that's account-specific).",
            "default": "auto",
            "choices": ["auto", "debrid", "usenet", "combined"]
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
            "description": "Comma-separated list of Plex movie library names or IDs (e.g., 'Movies,4K Movies' or '1,5')",
            "default": "Movies"
        },
        "shows_libraries": {
            "type": "string",
            "description": "Comma-separated list of Plex TV show library names or IDs (e.g., 'TV Shows,Anime' or '2,3')",
            "default": "Shows"
        },
        "update_plex_on_file_discovery": {
            "type": "boolean",
            "description": "Update Plex on file discovery (cli_debrid must be able to access mount at the below location)",
            "default": False
        },
        "mounted_file_location": {
            "type": "string",
            "description": "The single path cli-debrid checks for file presence (in Zurg use the /__all__ folder). When you run more than one provider (e.g. Real-Debrid via zurg AND Usenet via NzbDAV), point this at one combined mount that contains all of them — e.g. a mergerfs union of the providers' mounts — so file checks succeed regardless of which provider holds the item. May differ from the per-provider mount path set under Usenet Provider.",
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
            "choices": ["RealDebrid", "AllDebrid", "Premiumize", "Torbox", "DebridLink"]
        },
        "api_key": {
            "type": "string",
            "description": "API key for the debrid service",
            "default": "demo_key",
            "sensitive": True
        },
        "enable_debrid_naming": {
            "type": "boolean",
            "description": "Name debrid torrent folders in cli_mount's DFS mount using a structured format: {title} ({year}) - {imdb-id} - {version} - (original) for movies and {title} ({year}) - SxxExx - {episode title} - {imdb-id} - {version} - (original) for episodes. Requires cli_mount as the usenet provider (URL configured). Only renames the virtual folder in cli_mount — the actual file on the debrid service is unchanged.",
            "default": False
        },
        "include_version_in_debrid_naming": {
            "type": "boolean",
            "description": "Include the version (e.g. Default, 4K Remux) in the debrid folder name when Debrid File Naming is enabled.",
            "default": True
        },
        "include_content_source_in_debrid_naming": {
            "type": "boolean",
            "description": "Include the content source display name in the debrid folder name when Debrid File Naming is enabled.",
            "default": False
        },
        "ffprobe_all_debrid_additions": {
            "type": "boolean",
            "description": "Before collecting a debrid (Real-Debrid/etc.) file, probe it with ffprobe to confirm it's actually playable, on top of the normal checks. Works in both Symlinked/Local and Plex mode — in Plex mode it runs the moment the file is found on the mount, before cli_debrid tells Plex about it, so a confirmed-broken file is rejected and re-scraped before it ever shows up in Plex. Requires ffprobe to be installed. This will slow down how long it takes items to reach Collected.",
            "default": False
        }
    },
    "Usenet Provider": {
        "tab": "Required Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable usenet as a download source via cli_mount",
            "default": False
        },
        "url": {
            "type": "string",
            "description": "cli_mount URL — must not conflict with Phalanx DB (port 8888). Use the actual cli_mount host/port (e.g. http://192.168.1.x:8888 or http://climount:8888)",
            "default": ""
        },
        "api_token": {
            "type": "string",
            "description": "cli_mount API token (leave empty if auth is disabled)",
            "default": "",
            "sensitive": True
        },
        "download_folder": {
            "type": "string",
            "description": "Download folder category in cli_mount (leave empty for default)",
            "default": ""
        },
        "data_path": {
            "type": "string",
            "description": "Path to cli_mount's data directory inside the container — bind mount your cli_mount host data folder to /climount_data in your docker-compose (e.g. - /mnt/data/appdata/climount:/climount_data) then set this to /climount_data. Required for the cli_mount cleanup tool.",
            "default": "/climount_data"
        },
        "enable_nzb_naming": {
            "type": "boolean",
            "description": "Name NZB jobs submitted to the Usenet provider using a structured format with title, year, IMDb ID, version and original filename. Applies to movies, episodes, season packs and virtual aggregate packs.",
            "default": False
        },
        "include_version_in_nzb_naming": {
            "type": "boolean",
            "description": "Include the version (e.g. Default, 4K Remux) in the NZB job title when NZB File Naming is enabled.",
            "default": True
        },
        "include_content_source_in_nzb_naming": {
            "type": "boolean",
            "description": "Include the content source display name in the NZB job title when NZB File Naming is enabled.",
            "default": False
        },
        "retention_days": {
            "type": "integer",
            "description": "Maximum age of NZB results in days. Results older than this are filtered out before submission. Set to 0 to disable. Applies everywhere NZB indexers are searched.",
            "default": 1500
        },
        "disable_nzb_season_packs": {
            "type": "boolean",
            "description": "Reject NZB season packs entirely (movies are unaffected). A single damaged article in a season pack repairs the whole pack via a fresh grab; with this enabled, only aggregate/single-episode NZB results are considered, so a bad file only affects that one episode.",
            "default": True
        },
        "ffprobe_all_nzbs": {
            "type": "boolean",
            "description": "Before collecting an NZB file, probe it with ffprobe to confirm it's actually playable, on top of the existing missing-article/segment health check. Works in both Symlinked/Local and Plex mode — in Plex mode it runs the moment the file is found on the mount, before cli_debrid tells Plex about it, so a confirmed-broken file is rejected and re-scraped before it ever shows up in Plex. Requires ffprobe to be installed. This will slow down how long it takes NZB items to reach Collected.",
            "default": False
        },
        "provider": {
            "type": "string",
            "description": "Which usenet backend to use: cli_mount or NzbDAV.",
            "default": "climount",
            "choices": ["climount", "nzbdav"]
        },
        "owned_categories": {
            "type": "string",
            "description": "NzbDAV only. Comma-separated list of nzbdav categories the repair/health tool may act on. nzbdav history is shared with other SAB clients (e.g. Lidarr music), and repair can only re-acquire content cli-debrid manages — so it must never touch another app's entries. Leave empty to auto-pick the categories cli-debrid grabs into (movies, shows, movies_1080p_264, shows_1080p_264, plus the download-folder fallback). Set this only if your category names differ.",
            "default": ""
        },
        "exclude_categories": {
            "type": "string",
            "description": "NzbDAV only. Comma-separated nzbdav categories the repair/health tool must ignore, subtracted from the included set. Use this if you point Radarr/Sonarr at the same nzbdav and don't want cli-debrid touching their categories.",
            "default": ""
        },
        "nzbdav_category_map": {
            "type": "string",
            "description": "NzbDAV only. Optional. Choose which categories you want and what they're named on your instance, as comma-separated bucket=name pairs, e.g. `movies=movies, shows=shows, movies_1080p=movies_1080p, shows_1080p=shows_1080p, fallback=__unplayable__`. Detected buckets you omit fall back to their parent (movies_2160p_remux → movies_2160p → movies), so items never land in a category that doesn't exist on your instance. Recognised buckets: movies, shows, movies_1080p, shows_1080p, movies_2160p, shows_2160p, movies_1080p_remux, movies_2160p_remux, anime_movies, anime_shows, music, and 'fallback'. Leave empty to use the full default taxonomy. The setup helper's required-category list and the repair scope follow this map automatically.",
            "default": ""
        },
    },
    "TMDB": {
        "tab": "Required Settings",
        "api_key": {
            "type": "string",
            "description": "TMDB API key - used for poster retrieval and release date supplementation when TVDB is primary metadata source (not 'API Read Access Token')",
            "default": "",
            "sensitive": True
        },
        "certification_region": {
            "type": "select",
            "description": "Content rating region - select which country's certification system to display (e.g., US: R, PG-13 | GB: 15, 12A | CA: 14A, 18A)",
            "default": "US",
            "choices": ["US", "GB", "CA", "AU", "DE", "FR", "ES", "IT", "JP", "KR", "BR", "MX", "IN", "NL", "SE", "NO", "DK", "FI", "NZ", "IE", "AT", "CH", "BE", "PT", "PL", "RU", "TR", "AR", "CL", "CO"]
        }
    },
    "TVDB": {
        "tab": "Required Settings",
        "api_key": {
            "type": "string",
            "description": "TVDB v4 API key - when set, uses TVDB instead of Trakt for metadata lookups. A TMDB API key is also required for full release date support (digital/physical). Get a key at <a href='https://thetvdb.com/api-information' target='_blank'>thetvdb.com/api-information</a>",
            "default": "",
            "sensitive": True
        }
    },
    "MDBList": {
        "tab": "Additional Settings",
        "api_key": {
            "type": "string",
            "description": "MDBList API key - enables curated lists from IMDB, Trakt, Netflix, Disney+, and more on the Discover page, and is required for MDBList content sources that use an API endpoint (watchlist / username+list name / list ID). Get your API key at <a href='https://mdblist.com/preferences/' target='_blank'>mdblist.com/preferences</a>",
            "default": "",
            "sensitive": True
        },
        "cache_duration": {
            "type": "integer",
            "description": "How long to cache MDBList data (in hours). Lower values mean fresher data but more API calls.",
            "default": 24,
            "min": 1,
            "max": 168
        }
    },
    "Trakt": {
        "tab": "Additional Settings",
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
    "Scrob": {
        "tab": "Additional Settings",
        "url": {
            "type": "string",
            "description": "Base URL of your self-hosted Scrob instance, e.g. http://192.168.1.24:7330",
            "default": ""
        },
        "api_key": {
            "type": "string",
            "description": "Scrob API key (Settings → Connections → API Key in the Scrob UI). Shared by all Scrob content sources (Lists, Collection, Special) below. Used for all read operations (syncing Lists, Collection, and Special Lists into cli_debrid).",
            "default": "",
            "sensitive": True
        },
        "username": {
            "type": "string",
            "description": "Optional: your Scrob login username. Only needed if you want cli_debrid to remove items from Scrob Lists/Collection when you delete them from your library — Scrob's API key cannot authorize deletions, only a logged-in session can. Leave blank to skip deletion sync (syncing content in still works fine with just the API key above).",
            "default": ""
        },
        "password": {
            "type": "string",
            "description": "Optional: your Scrob login password, paired with the username above. Only used to obtain a session token for removing items from Scrob when deleted in cli_debrid — never sent anywhere else. Leave blank to skip deletion sync.",
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
        "queue_pool_workers": {
            "type": "integer",
            "description": "Number of concurrent worker threads for queue processing tasks (Adding, Checking, Scraping etc.). Higher values process more items in parallel but use more CPU/memory. Recommended: 1 for slow/older machines, 2 for normal, 3 for fast machines. Takes effect after saving (no restart required).",
            "default": 2,
            "min": 1,
            "max": 3
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
        # NOTE: 'Hybrid' is a real third option but is a VIRTUAL UI value — the UI
        # decomposes it on save into uncached_content_handling='None' + hybrid_mode=True
        # (see static/js/settings.js), so the stored value is never literally 'Hybrid'.
        # The 'Hybrid' branch in main.py is a backward-compat migration for legacy
        # configs that still hold the literal string; do NOT add 'Hybrid' to choices
        # or remove that branch without understanding that design. Cached/uncached is a
        # DEBRID-only concept — usenet/NZB results bypass this gate entirely.
        "uncached_content_handling": {
            "type": "string",
            "description": "DEBRID ONLY (no effect on usenet/NZB results). Uncached content management for debrid torrent results in the program queue. None: only take the best Cached result. Full: take the best result, cached or uncached. (A third 'Hybrid' mode is exposed in the UI and stored via the separate hybrid_mode toggle: try cached first, then fall back to uncached.)",
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
            "type": "string",
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
        "delayed_upgrade_scrape_days": {
            "type": "integer",
            "description": "Number of days to wait before attempting a single upgrade scrape on an item. Set to 0 to disable delayed upgrade scraping.",
            "default": 0,
            "min": 0
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
        "enable_seadex_priority": {
            "type": "boolean",
            "description": "For anime, prefer the release SeaDex (releases.moe) has confirmed as best for that title over cli-debrid's own scoring. Only applies to items detected as anime, and only within versions whose Anime Filter Mode is not 'Non-Anime Only'. Requires an internet connection to releases.moe and api.ani.zip during scraping.",
            "default": False
        },
        "trakt_early_releases": {
            "type": "boolean",
            "description": "Check Trakt for early releases",
            "default": False
        },
        "trakt_rate_limit_enabled": {
            "type": "boolean",
            "description": "Enable Trakt API rate limiting to prevent 429 errors. Automatically detects VIP/Free tier and adjusts limits. Recommended: enabled.",
            "default": True
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
                },
                "enable_scraper_priorities": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable version-specific scraper priority scoring"
                },
                "scraper_priorities": {
                    "type": "dict",
                    "default": {},
                    "description": "Per-scraper priority scores for this version. Higher scores = higher priority."
                },
                "enable_spanish_episode_parsing": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable Spanish content parsing: Cap.XXYY episode format (e.g. Cap.701 → S07E01) and parenthesized alternative title matching (e.g. 'El renacido (The Revenant)' → matches 'The Revenant')."
                },
                "use_alternative_titles": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include alternative title translations/aliases when matching titles. Uses the version's Language Code to filter aliases."
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
                "For multi-episode files, {episode_number} will be formatted as 'E17-E18' instead of a single number.",
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
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "url": {"type": "string", "default": "", "validate": "url"},
                "db_enabled": {"type": "boolean", "default": False, "description": "Connect directly to Zilean PostgreSQL DB for Upgrade Hub scanning"},
                "db_port": {"type": "integer", "default": 5432},
                "db_name": {"type": "string", "default": ""},
                "db_username": {"type": "string", "default": ""},
                "db_password": {"type": "string", "default": "", "sensitive": True}
            },
            "Jackett": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api": {"type": "string", "default": "", "sensitive": True},
                "enabled_indexers": {"type": "string", "default": ""}
            },
            "Prowlarr": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
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
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "opts": {"type": "string", "default": ""}
            },
            "Nyaa": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"}
            },
            "OldNyaa": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"}
            },
            "MediaFusion": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "url": {"type": "string", "default": "", "validate": "url"},
            },
            "AIOStreams": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "url": {"type": "string", "default": "", "validate": "url"}
            },
            "AIOStreams-API": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "base_url": {"type": "string", "default": "", "validate": "url"},
                "uuid": {"type": "string", "default": ""},
                "password": {"type": "string", "default": "", "sensitive": True}
            },
            "Newznab": {
                "enabled": {"type": "boolean", "default": False},
                "priority": {"type": "integer", "default": 0, "description": "Scraper priority score (higher = better priority)"},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api_key": {"type": "string", "default": "", "sensitive": True},
                "subscription_expiry_date": {
                    "type": "string",
                    "default": "",
                    "description": "Optional: date this Newznab subscription expires (YYYY-MM-DD format). Leave empty if not applicable."
                },
                "auto_renew": {
                    "type": "boolean",
                    "default": False,
                    "description": "Optional: whether this Newznab subscription auto-renews."
                }
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
                "source_mode": {
                    "type": "string",
                    "description": "How this source fetches its items. 'json_url' appends /json to a public MDBList (or compatible) list URL and needs no API key. The 'api_*' modes call the MDBList API directly and require an MDBList API key (Additional Settings -> MDBList).",
                    "default": "json_url",
                    "choices": ["json_url", "api_watchlist", "api_user_list", "api_list_id"]
                },
                "urls": {
                    "type": "string",
                    "description": "Comma-separated MDBList (or compatible) list URLs. '/json' is appended automatically unless the URL already ends in '.json'. Used when source_mode is 'json_url'.",
                    "default": ""
                },
                "username": {
                    "type": "string",
                    "description": "MDBList username that owns the list. Used when source_mode is 'api_user_list' (GET /lists/{username}/{listname}/items).",
                    "default": ""
                },
                "listname": {
                    "type": "string",
                    "description": "MDBList list name/slug as it appears in the list URL. Used when source_mode is 'api_user_list' (GET /lists/{username}/{listname}/items).",
                    "default": ""
                },
                "list_id": {
                    "type": "string",
                    "description": "Numeric MDBList list ID, or several separated by commas. Used when source_mode is 'api_list_id' (GET /lists/{listid}/items).",
                    "default": ""
                },
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "MDBList"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_collection": {
                    "type": "dict",
                    "description": "Configure a Plex collection that mirrors this source list order",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex collection management for this source",
                            "default": False
                        },
                        "collection_name": {
                            "type": "string",
                            "description": "Collection name. Defaults to the source display name. For mixed (Movies+Shows) lists, ' Movies' and ' Shows' suffixes are added automatically unless overridden below.",
                            "default": ""
                        },
                        "collection_name_movies": {
                            "type": "string",
                            "description": "Override collection name for movies in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "collection_name_shows": {
                            "type": "string",
                            "description": "Override collection name for shows in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "sort_prefix": {
                            "type": "string",
                            "description": "Prefix added to the sort title so the collection sorts to the top in Plex (e.g. '!' gives '!My List'). Leave empty to use the collection name as-is.",
                            "default": "!!!!"
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "Sort order for items in the Plex collection. Default uses the source list order.",
                            "default": "default",
                            "choices": ["default", "title", "year", "release_date", "collected_at", "runtime", "random"]
                        },
                        "sort_how": {
                            "type": "string",
                            "description": "Sort direction: asc = ascending, desc = descending.",
                            "default": "asc",
                            "choices": ["asc", "desc"]
                        },
                        "poster_design": {
                            "type": "integer",
                            "description": "Collection poster design (0 = Plex default, 1-8 = custom designs).",
                            "default": 0
                        },
                        "poster_accent": {
                            "type": "string",
                            "description": "Accent color for the poster (hex, e.g. #E6A800).",
                            "default": "#E6A800"
                        },
                        "poster_eyebrow": {
                            "type": "string",
                            "description": "Optional eyebrow text shown above the collection title on the poster. Leave blank to hide.",
                            "default": ""
                        },
                        "poster_icon": {
                            "type": "string",
                            "description": "Icon path for the poster (relative to overlay assets logos folder). Leave blank to use source default.",
                            "default": ""
                        },
                        "libraries": {
                            "type": "list",
                            "description": "Plex library section keys to sync this collection into. Leave empty to use the first library of each type.",
                            "default": []
                        }
                    }
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the list name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                },
                "plex_collection": {
                    "type": "dict",
                    "description": "Configure a Plex collection that mirrors this source list order",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex collection management for this source",
                            "default": False
                        },
                        "collection_name": {
                            "type": "string",
                            "description": "Collection name. Defaults to the source display name. For mixed (Movies+Shows) lists, ' Movies' and ' Shows' suffixes are added automatically unless overridden below.",
                            "default": ""
                        },
                        "collection_name_movies": {
                            "type": "string",
                            "description": "Override collection name for movies in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "collection_name_shows": {
                            "type": "string",
                            "description": "Override collection name for shows in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "sort_prefix": {
                            "type": "string",
                            "description": "Prefix added to the sort title so the collection sorts to the top in Plex (e.g. '!' gives '!My List'). Leave empty to use the collection name as-is.",
                            "default": "!!!!"
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "Sort order for items in the Plex collection. Sorting is handled server-side by the Trakt API. VIP-only sorts fall back to rank for non-VIP accounts.",
                            "default": "default",
                            "choices": ["default", "rank", "added", "title", "released", "runtime",
                                        "popularity", "percentage", "random", "votes", "my_rating", "watched", "collected",
                                        "imdb_rating", "tmdb_rating", "rt_tomatometer", "rt_audience",
                                        "metascore", "imdb_votes", "tmdb_votes"]
                        },
                        "sort_how": {
                            "type": "string",
                            "description": "Sort direction: asc = ascending, desc = descending.",
                            "default": "asc",
                            "choices": ["asc", "desc"]
                        },
                        "poster_design": {
                            "type": "integer",
                            "description": "Collection poster design (0 = Plex default, 1-8 = custom designs).",
                            "default": 0
                        },
                        "poster_accent": {
                            "type": "string",
                            "description": "Accent color for the poster (hex, e.g. #E6A800).",
                            "default": "#E6A800"
                        },
                        "poster_eyebrow": {
                            "type": "string",
                            "description": "Optional eyebrow text shown above the collection title on the poster. Leave blank to hide.",
                            "default": ""
                        },
                        "poster_icon": {
                            "type": "string",
                            "description": "Icon path for the poster (relative to overlay assets logos folder). Leave blank to use source default.",
                            "default": ""
                        },
                        "libraries": {
                            "type": "list",
                            "description": "Plex library section keys to sync this collection into. Leave empty to use the first library of each type.",
                            "default": []
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                }
            },
            "Scrob Lists": {
                "enabled": {"type": "boolean", "default": False},
                "scrob_list_ids": {
                    "type": "string",
                    "description": "Comma-separated Scrob list IDs (numeric, from Settings → Connections → API Key page or the list's URL in the Scrob UI), e.g. '2,7'. Requires Scrob URL/API Key to be configured under Additional Settings → Scrob.",
                    "default": ""
                },
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Scrob Lists"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                },
                "plex_collection": {
                    "type": "dict",
                    "description": "Configure a Plex collection that mirrors this source list order",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex collection management for this source",
                            "default": False
                        },
                        "collection_name": {
                            "type": "string",
                            "description": "Collection name. Defaults to the source display name. For mixed (Movies+Shows) lists, ' Movies' and ' Shows' suffixes are added automatically unless overridden below.",
                            "default": ""
                        },
                        "collection_name_movies": {
                            "type": "string",
                            "description": "Override collection name for movies in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "collection_name_shows": {
                            "type": "string",
                            "description": "Override collection name for shows in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "sort_prefix": {
                            "type": "string",
                            "description": "Prefix added to the sort title so the collection sorts to the top in Plex (e.g. '!' gives '!My List'). Leave empty to use the collection name as-is.",
                            "default": "!!!!"
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "Sort order for items in the Plex collection. Scrob has no server-side sort/rank API, so ordering follows Scrob's own list display order (added_at ascending).",
                            "default": "default",
                            "choices": ["default", "added", "title", "released", "runtime",
                                        "popularity", "random", "imdb_rating", "tmdb_rating"]
                        },
                        "sort_how": {
                            "type": "string",
                            "description": "Sort direction: asc = ascending, desc = descending.",
                            "default": "asc",
                            "choices": ["asc", "desc"]
                        },
                        "poster_design": {
                            "type": "integer",
                            "description": "Collection poster design (0 = Plex default, 1-8 = custom designs).",
                            "default": 0
                        },
                        "poster_accent": {
                            "type": "string",
                            "description": "Accent color for the poster (hex, e.g. #E6A800).",
                            "default": "#E6A800"
                        },
                        "poster_eyebrow": {
                            "type": "string",
                            "description": "Optional eyebrow text shown above the collection title on the poster. Leave blank to hide.",
                            "default": ""
                        },
                        "poster_icon": {
                            "type": "string",
                            "description": "Icon path for the poster (relative to overlay assets logos folder). Leave blank to use source default.",
                            "default": ""
                        },
                        "libraries": {
                            "type": "list",
                            "description": "Plex library section keys to sync this collection into. Leave empty to use the first library of each type.",
                            "default": []
                        }
                    }
                }
            },
            "Scrob Collection": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Scrob Collection"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                "allowed_requesters": {
                    "type": "list",
                    "description": "List of Overseerr usernames whose requests this source should process. Use ['__all__'] to process all users.",
                    "default": ["__all__"]
                },
                "list_length_limit": {
                    "type": "integer",
                    "description": "Maximum number of items to process from this content source. Leave empty or set to 0 for no limit.",
                    "default": 0
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'requester' uses requester display name automatically, 'fixed' uses a static label you specify",
                            "default": "requester",
                            "choices": ["requester", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                }
            },
            "Agregarr": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Agregarr"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'requester' uses requester display name automatically, 'fixed' uses a static label you specify",
                            "default": "requester",
                            "choices": ["requester", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
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
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                }
            },
            "Special Scrob Lists": {
                "enabled": {"type": "boolean", "default": False},
                "special_list_type": {
                    "type": "list",
                    "default": [],
                    "choices": [
                        "Trending",
                        "Popular",
                        "Top Rated",
                        "Now Playing",
                        "Upcoming",
                        "On Air Today",
                        "On Air This Week",
                        "New Episodes",
                        "Hidden Gems",
                        "For You",
                        "Recently Added"
                    ],
                    "description": "Select the type(s) of special Scrob list. 'Now Playing', 'Upcoming', and 'Hidden Gems' apply to Movies only; 'On Air Today', 'On Air This Week', and 'New Episodes' apply to Shows only. 'For You' requires genre preferences to be set in the Scrob user's profile, or it returns nothing."
                },
                "special_list_genres": {
                    "type": "list",
                    "default": [],
                    "choices": [
                        "Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
                        "Drama", "Family", "Fantasy", "History", "Horror", "Music", "Mystery",
                        "Romance", "Science Fiction", "TV Movie", "Thriller", "War", "Western",
                        "Action & Adventure", "Kids", "News", "Reality", "Sci-Fi & Fantasy",
                        "Soap", "Talk", "War & Politics"
                    ],
                    "description": "Optional: genre-filtered discover lists (e.g. 'Animation' → Animation Movies/Shows), fetched via Scrob's TMDB discover proxy. Combine with special_list_type above, or leave that empty and use only genres."
                },
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {
                    "type": "string",
                    "default": "All",
                    "choices": ["All", "Movies", "Shows"],
                    "description": "Select media type. Note: some special list types are movie-only or show-only (see description above)."
                },
                "display_name": {"type": "string", "default": "Special Scrob Lists"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the source name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                }
            },
            "Adaptive List": {
                "enabled": {"type": "boolean", "default": False},
                "lists": {
                    "type": "list",
                    "description": "List of adaptive filter configurations. Each list uses TMDB discover filters that produce time-sensitive results.",
                    "default": [],
                    "schema": {
                        "name": {"type": "string", "description": "Name for this adaptive list", "default": ""},
                        "media_type": {"type": "string", "description": "Media type: movie or tv", "default": "movie", "choices": ["movie", "tv"]},
                        "filters": {"type": "dict", "description": "TMDB discover filter parameters", "default": {}}
                    }
                },
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Adaptive List"},
                "allow_specials": {
                    "type": "boolean",
                    "description": "Allow processing of Season 0 (Specials) for shows added via this source.",
                    "default": False
                },
                "unblacklist_on_source_run": {
                    "type": "boolean",
                    "description": "When enabled, items in Blacklisted state (not ghostlisted) will be unblacklisted and re-queued as Wanted when this source runs.",
                    "default": False
                },
                "custom_symlink_subfolder": {
                    "type": "string",
                    "description": "Optional: Specify a custom subfolder within the main symlink root directory for items from this source. If set, items will be placed in '[Symlink Root]/[Custom Subfolder]/...' instead of directly in '[Symlink Root]/...'. Leave empty for default behavior.",
                    "default": ""
                },
                "tags": {
                    "type": "list",
                    "description": "Plex mode only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
                    "default": []
                },
                "tags_exclusive": {
                    "type": "boolean",
                    "description": "NzbDAV only: when enabled, items from this source are routed ONLY to the tag category (and not to resolution/type categories). Requires tags to be set.",
                    "default": False
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
                },
                "seasons_per_show": {
                    "type": "integer",
                    "description": "Limit the number of seasons grabbed per TV show from this source. Set to 0 for all seasons.",
                    "default": 0
                },
                "season_grab_order": {
                    "type": "string",
                    "description": "Which seasons to grab when seasons_per_show is limited: first seasons, latest seasons, or most recently aired.",
                    "default": "first",
                    "choices": ["first", "latest", "recent"]
                },
                "plex_labels": {
                    "type": "dict",
                    "description": "Configure Plex labels to be automatically applied to items from this source",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex label application for this source",
                            "default": False
                        },
                        "label_mode": {
                            "type": "string",
                            "description": "Label mode: 'list_name' uses the adaptive list name automatically, 'fixed' uses a static label you specify",
                            "default": "list_name",
                            "choices": ["list_name", "fixed"]
                        },
                        "fixed_label": {
                            "type": "string",
                            "description": "Fixed label(s) to apply (only used when label_mode is 'fixed'). Supports comma-separated values for multiple labels (e.g., 'ufc,ppv')",
                            "default": ""
                        }
                    }
                },
                "plex_collection": {
                    "type": "dict",
                    "description": "Configure a Plex collection that mirrors this source list order",
                    "default": {},
                    "schema": {
                        "enabled": {
                            "type": "boolean",
                            "description": "Enable automatic Plex collection management for this source",
                            "default": False
                        },
                        "collection_name": {
                            "type": "string",
                            "description": "Collection name. Defaults to the source display name. For mixed (Movies+Shows) lists, ' Movies' and ' Shows' suffixes are added automatically unless overridden below.",
                            "default": ""
                        },
                        "collection_name_movies": {
                            "type": "string",
                            "description": "Override collection name for movies in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "collection_name_shows": {
                            "type": "string",
                            "description": "Override collection name for shows in a mixed list. Leave empty to use auto-suffix.",
                            "default": ""
                        },
                        "sort_prefix": {
                            "type": "string",
                            "description": "Prefix added to the sort title so the collection sorts to the top in Plex (e.g. '!' gives '!My List'). Leave empty to use the collection name as-is.",
                            "default": "!!!!"
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "Sort order for items in the Plex collection. Default follows the adaptive list's own TMDB sort setting.",
                            "default": "default",
                            "choices": ["default", "title", "year", "release_date", "collected_at", "runtime", "random"]
                        },
                        "sort_how": {
                            "type": "string",
                            "description": "Sort direction: asc = ascending, desc = descending.",
                            "default": "asc",
                            "choices": ["asc", "desc"]
                        },
                        "poster_design": {
                            "type": "integer",
                            "description": "Collection poster design (0 = Plex default, 1-8 = custom designs).",
                            "default": 0
                        },
                        "poster_accent": {
                            "type": "string",
                            "description": "Accent color for the poster (hex, e.g. #E6A800).",
                            "default": "#E6A800"
                        },
                        "poster_eyebrow": {
                            "type": "string",
                            "description": "Optional eyebrow text shown above the collection title on the poster. Leave blank to hide.",
                            "default": ""
                        },
                        "poster_icon": {
                            "type": "string",
                            "description": "Icon path for the poster (relative to overlay assets logos folder). Leave blank to use source default.",
                            "default": ""
                        },
                        "libraries": {
                            "type": "list",
                            "description": "Plex library section keys to sync this collection into. Leave empty to use the first library of each type.",
                            "default": []
                        }
                    }
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
        "include_ai_translated_subtitles": {
            "type": "boolean",
            "description": "Include AI translated subtitles in search results. These may have lower quality but provide broader language coverage.",
            "default": True
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
        },
        "probe_for_embedded_subtitles": {
            "type": "boolean",
            "description": "Before searching for subtitles, probe the file with ffprobe to detect embedded subtitle tracks. If all configured languages are already embedded, the external search is skipped. Applies to built-in subtitle downloader and Bazarr. Requires ffprobe to be installed. Note: probing dereferences the symlink to the debrid mount — ensure your mount is stable before enabling.",
            "default": False
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
    },
    "Library Manager": {
        "tab": "Additional Settings",
        "primary_artwork_source": {
            "type": "string",
            "description": "Artwork source for posters and backdrops. Uses TMDB online movie database (requires TMDB API key).",
            "default": "TMDB",
            "choices": ["TMDB"]
        },
        "ghostlist_mode": {
            "type": "boolean",
            "description": "Add shows and movies to the ghostlist to prevent them from being re-added. When enabled, deleting content will mark it as ghosted in the database. When disabled, deleted content can be re-added by dynamic content sources.",
            "default": False
        },
        "remove_from_content_sources": {
            "type": "boolean",
            "description": "Remove items from content sources (Trakt, Overseerr, etc.) during deletion. Enable when ghostlist OFF (prevents re-addition, slower). Disable when ghostlist ON (faster, ghostlist already prevents re-addition).",
            "default": True
        },
        "hide_season_zero": {
            "type": "boolean",
            "description": "Hide Season 0 (Specials) tab and its episodes from the TV show page.",
            "default": True
        }
    },
    "Discover Settings": {
        "tab": "Additional Settings",
        "hide_no_rating": {
            "type": "boolean",
            "description": "Hide items without a rating from discover results",
            "default": False
        },
        "hide_no_poster": {
            "type": "boolean",
            "description": "Hide items without a poster image from discover results",
            "default": False
        },
        "only_show_missing": {
            "type": "boolean",
            "description": "Only show items that are not in your library (hide items already in database)",
            "default": False
        },
        "tv_show_episode_view": {
            "type": "string",
            "description": "Where to route when clicking on TV shows not in library (movies always go to Discover details)",
            "default": "discover",
            "choices": ["discover", "add_media"]
        },
        "hide_specials": {
            "type": "boolean",
            "description": "Hide TV show specials (Season 0) from the Discover details page season list",
            "default": True
        }
    },
    "Bazarr Integration": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable Radarr/Sonarr API spoofing for Bazarr subtitle integration. When enabled, cli_debrid will expose API endpoints that allow Bazarr to connect as if this were Radarr/Sonarr.",
            "default": False
        },
        "api_key": {
            "type": "string",
            "description": "API key for Bazarr authentication. Click 'Generate' to create a new key, or enter your own. This key must be used when configuring Bazarr.",
            "default": "",
            "sensitive": True
        },
        "service_type": {
            "type": "string",
            "description": "Which service to emulate. 'Auto' exposes both Radarr (movies) and Sonarr (TV) endpoints.",
            "default": "auto",
            "choices": ["auto", "radarr", "sonarr"]
        },
        "spoofed_version": {
            "type": "string",
            "description": "Version number to report to Bazarr. Change only if Bazarr requires a specific version.",
            "default": "5.14.0.9383"
        },
        "delete_subtitles_on_removal": {
            "type": "boolean",
            "description": "When deleting media, also delete any subtitle files (.srt, .ass, .sub, etc.) left in the symlink folder by Bazarr. Disable this to keep subtitle files after deletion.",
            "default": True
        }
    },
    "AI Assistant": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable the AI Butler chat widget. Requires OpenClaw to be installed and configured.",
            "default": False
        },
        "openclaw_url": {
            "type": "string",
            "description": "URL of your OpenClaw instance (e.g. http://192.168.1.x:18789). OpenClaw handles AI provider selection and session memory.",
            "default": ""
        },
        "openclaw_token": {
            "type": "string",
            "description": "Bearer token for OpenClaw authentication. Leave blank if OpenClaw has no auth configured.",
            "default": "",
            "sensitive": True
        },
        "agent_id": {
            "type": "string",
            "description": "OpenClaw agent ID to use (default: main).",
            "default": "main"
        },
        "health_notifications": {
            "type": "boolean",
            "description": "Send proactive health alerts via your configured notification channels when the AI Butler detects issues (stuck queues, high blacklist rate, errors, etc.).",
            "default": True
        },
        "health_check_interval": {
            "type": "integer",
            "description": "How often (in seconds) the AI Butler runs background health checks. Minimum 300 (5 minutes).",
            "default": 900
        },
        "enable_settings_assistant": {
            "type": "boolean",
            "description": "Phase 2 — Settings Assistant: Allows the AI to suggest and apply setting changes directly from chat. When enabled, the AI can output Apply buttons in its responses that save a setting with one click, and can trigger a program restart after applying changes. When disabled, the AI becomes read-only — it can still explain settings but cannot change them, and the /api/ai/apply_setting and /api/ai/restart endpoints will be blocked.",
            "default": True
        },
        "enable_proactive_notifications": {
            "type": "boolean",
            "description": "Phase 3 — Proactive Notifications: Runs a background health monitor that periodically checks for issues (stuck queues, high blacklist rate, error spikes, stalled upgrades, large DB) and sends plain-English alerts via your configured notification channels (Discord, Telegram, etc.). When disabled, no background health checks run and you will not receive AI-generated alerts. The health_notifications and health_check_interval settings below only apply when this is enabled.",
            "default": True
        },
        "enable_recommendations": {
            "type": "boolean",
            "description": "Phase 4 — Content Recommendations & Add to Library: Allows the AI to suggest movies and shows based on your watch history and ratings, and lets you add them to your library with one click from the chat. When enabled, your watch history (from Plex or Trakt) and your full collected library are included in the AI's context so it can make personalised suggestions and avoid recommending things you already have. When disabled, watch history and the full library list are excluded from the prompt, the ADD_TO_LIBRARY action cards will not appear, and the /api/ai/add_to_library endpoint will be blocked.",
            "default": True
        },
        "enable_habit_tracking": {
            "type": "boolean",
            "description": "Phase 5 — Habit Learning & Automation: Records significant actions you take in cli_debrid (manually triggering content sources, upgrade scans, library adds, program start/stop) to a local ai_habits table. The AI analyses these patterns and proactively suggests automations — for example, if you trigger the same content source every morning it will suggest scheduling it, or if you frequently add items manually it will suggest adding a Trakt list. When disabled, no actions are recorded and the AI will not make automation suggestions. Previously recorded habit data is kept but ignored.",
            "default": True
        },
        "share_full_config": {
            "type": "boolean",
            "description": "Share Full Config with AI: When enabled, the AI receives your complete config.json (with all tokens, passwords and API keys permanently redacted to ***) plus expanded log tails, upgrade hub history, and notification history. This gives the AI much deeper context — it can diagnose configuration problems, explain exactly why a setting is set the way it is, identify conflicting settings across sections, and give more accurate advice. When disabled, the AI only receives a short key-settings summary, which reduces prompt size and limits how much of your configuration is sent to the AI provider but also reduces the AI's ability to help with complex issues.",
            "default": True
        },
        "plex_labels": {
            "type": "object",
            "description": "Plex label settings for items added via AI Butler.",
            "schema": {
                "enabled": {
                    "type": "boolean",
                    "description": "Apply Plex labels to items added to your library via the AI Butler.",
                    "default": False
                },
                "label_mode": {
                    "type": "string",
                    "description": "How to label items: 'fixed' applies the same label to all AI Butler additions.",
                    "default": "fixed",
                    "choices": ["fixed"]
                },
                "fixed_label": {
                    "type": "string",
                    "description": "Label to apply to all items added via AI Butler (e.g. 'AI Butler'). Comma-separated for multiple labels.",
                    "default": "AI Butler"
                }
            }
        }
    },
    "Overlay Settings": {
        "tab": "Additional Settings",
        "overlays_enabled": {
            "type": "boolean",
            "description": "Enable the poster overlay system. When enabled, overlay sync tasks will automatically run and apply media info badges to Plex posters. Requires Plex URL and token to be configured.",
            "default": False
        },
        "textless_posters": {
            "type": "boolean",
            "description": "When enabled, cli_debrid will fetch a language-neutral (textless) poster from TMDB as the base image. Use the Title Logo badge in the layout editor to add the title as a transparent clearlogo PNG. Note: Not all titles have textless posters on TMDB — the standard English poster is used as fallback.",
            "default": False
        },
        "plex_data_path": {
            "type": "string",
            "description": "Path to the Plex Media Server data directory (e.g. /config/Library/Application Support/Plex Media Server). Required for the poster cleanup task to delete old uploaded overlay versions directly from the filesystem.",
            "default": ""
        },
        "overlay_content_check_interval_days": {
            "type": "number",
            "description": "How often (in days) to re-fetch ratings (IMDb/TMDB/Trakt/RT) and show status from the MDBList API to detect changes that should trigger an overlay refresh. Set to 0 to check every sync run. Default is 7 days. Version count changes are always checked every sync regardless of this setting.",
            "default": 7
        },
        "sync_items_per_run": {
            "type": "number",
            "description": "Maximum number of shows/movies to process per overlay sync run. Higher values clear backlogs faster but each run takes longer. Default is 200.",
            "default": 200
        }
    },
    "Plex Smart Collections": {
        "tab": "Additional Settings",
        "poster_design": {"type": "number", "description": "Poster layout design ID (0=Plex Default)", "default": 0},
        "poster_accent": {"type": "string", "description": "Accent color hex. Leave empty for design default.", "default": ""},
        "poster_eyebrow": {"type": "string", "description": "Small text above the collection title.", "default": ""},
        "poster_icon": {"type": "string", "description": "Icon path. Leave empty for source default.", "default": ""},
        "poster_overlay_opacity": {"type": "number", "description": "Card overlay opacity 0-100.", "default": 60},
        "poster_glow_opacity": {"type": "number", "description": "Accent glow opacity 0-100.", "default": 80},
        "poster_glow_radius": {"type": "number", "description": "Accent glow radius 10-200.", "default": 55},
        "collections": {"type": "dict", "description": "Per-collection enabled states.", "default": {}}
    },
    "Tags": {
        "tab": "Additional Settings",
        "tags_list": {
            "type": "list",
            "description": "Global tag list for Plex mode NZB folder routing. Tags are embedded in NZB filenames so cli_mount can route items to virtual folders.",
            "default": []
        }
    },
    "Plex Movie Box Sets": {
        "tab": "Additional Settings",
        "enabled": {"type": "bool", "description": "Enable automatic Plex movie box set collection management.", "default": False},
        "grab_missing": {"type": "bool", "description": "Add missing movies from box sets to the wanted queue.", "default": False},
        "grab_version": {"type": "string", "description": "Version/quality to use when grabbing missing movies.", "default": "Default"},
        "collection_name_pattern": {"type": "string", "description": "Naming pattern for box set collections. {title} = franchise name (e.g. The Godfather).", "default": "{title} Collection"},
        "min_movies": {"type": "number", "description": "Minimum number of owned movies required to create/keep a box set collection.", "default": 2},
        "sort_order": {"type": "string", "description": "Sort order for movies within each box set collection.", "default": "release_date_asc"}
    }
}
