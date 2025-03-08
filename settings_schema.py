# settings_schema.py

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
            "description": "Path to the original files (in Zurg use the /__all__ folder). On Windows, this must be on the same drive as the symlinked files path.",
            "default": "/mnt/zurg/__all__"
        },
        "symlinked_files_path": {
            "type": "string",
            "description": "Path to the destination folder (where you want your files linked to). On Windows, this must be on the same drive as the original files path.",
            "default": "/mnt/symlinked"
        },
        "symlink_organize_by_type": {
            "type": "boolean",
            "description": "Organize symlinked files into Movies and TV Shows folders",
            "default": True
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
            "description": "TMDB API key - used for Poster URL retrieval",
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
        "wake_limit": {
            "type": "string",
            "description": "Number of times to wake items before blacklisting",
            "default": "24"
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
            "description": "Number of days after which to automatically remove blacklisted items for a re-scrape",
            "default": "30"
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
        "versions": {
            "type": "dict",
            "description": "Scraping versions configuration",
            "default": {},
            "schema": {
                "enable_hdr": {"type": "boolean", "default": False},
                "max_resolution": {
                    "type": "string",
                    "choices": ["2160p", "1080p", "720p", "SD"],
                    "default": "1080p"
                },
                "resolution_wanted": {
                    "type": "string",
                    "choices": ["<=", "==", ">="],
                    "default": "<="
                },
                "resolution_weight": {"type": "integer", "default": 3, "min": 0},
                "hdr_weight": {"type": "integer", "default": 3, "min": 0},
                "similarity_weight": {"type": "integer", "default": 3, "min": 0},
                "similarity_threshold": {"type": "float", "default": 0.8, "min": 0, "max": 1},
                "similarity_threshold_anime": {"type": "float", "default": 0.35, "min": 0, "max": 1},
                "size_weight": {"type": "integer", "default": 3, "min": 0},
                "bitrate_weight": {"type": "integer", "default": 3, "min": 0},
                "preferred_filter_in": {"type": "list", "default": []},
                "preferred_filter_out": {"type": "list", "default": []},
                "filter_in": {"type": "list", "default": []},
                "filter_out": {"type": "list", "default": []},
                "min_size_gb": {"type": "float", "default": 0.01, "min": 0},
                "max_size_gb": {"type": "float", "default": float('inf'), "min": 0},
                "min_bitrate_mbps": {"type": "float", "default": 0.01, "min": 0},
                "max_bitrate_mbps": {"type": "float", "default": float('inf'), "min": 0},
                "wake_count": {
                    "type": "integer", 
                    "default": None, 
                    "min": -1, 
                    "nullable": True,
                    "description": "Override global wake count limit. -1 disables sleeping queue (only search once), empty uses global setting."
                },
                "require_physical_release": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only mark as Wanted after physical release date is available"
                }
            }
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
            "description": "Remove items from Plex Watchlist when they have been collected",
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
            "description": "List of filenames to filter out from the queue, comma separated",
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
            "default": False
        },
        "enable_unmatched_items_check": {
            "type": "boolean",
            "description": "Enable checking and fixing of unmatched or incorrectly matched items in Plex during collection scans",
            "default": True
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
        "emby_url": {
            "type": "string",
            "description": "Emby server URL for library updates (e.g. http://localhost:8096)",
            "default": "",
            "validate": "url"
        },
        "emby_token": {
            "type": "string",
            "description": "Emby API key/token for authentication",
            "default": "",
            "sensitive": True
        },
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
                "display_name": {"type": "string", "default": "MDBList"}
            },
            "Collected": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Collected"}
            },
            "Trakt Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Watchlist"}
            },
            "Trakt Lists": {
                "enabled": {"type": "boolean", "default": False},
                "trakt_lists": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Lists"}
            },
            "Trakt Collection": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Trakt Collection"}
            },
            "Overseerr": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api_key": {"type": "string", "default": "", "sensitive": True},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Overseerr"}
            },
            "My Plex Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Plex Watchlist"}
            },
            "Other Plex Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "username": {"type": "string", "default": ""},
                "token": {"type": "string", "default": "", "sensitive": True},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Other Plex Watchlist"}
            },
            "My Plex RSS Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Plex RSS Watchlist"}
            },
            "My Friends Plex RSS Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "My Friends Plex RSS Watchlist"}
            },
            "Friends Trakt Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "auth_id": {"type": "string", "default": ""},
                "username": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
                "display_name": {"type": "string", "default": "Friend's Trakt Watchlist"}
            }
        }
    },
    "Notifications": {
        "tab": "Notifications",
        "type": "dict",
        "description": "Notification configurations",
        "default": {},
        "schema": {
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
    }
}
