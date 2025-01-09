# settings_schema.py

SETTINGS_SCHEMA = {
    "UI Settings": {
        "tab": "Additional Settings",
        "enable_user_system": {
            "type": "boolean",
            "description": "Enable user account system",
            "default": True
        },
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
        }
    },
    "File Management": {
        "tab": "Required Settings",
        "file_collection_management": {
            "type": "string",
            "description": "Select library management method.",
            "default": "Plex",
            "choices": ["Plex", "Symlinked/Local"]
        },
        "original_files_path": {
            "type": "string",
            "description": "Path to the original files (in Zurg use the /__all__ folder)",
            "default": "/mnt/zurg/__all__"
        },
        "symlinked_files_path": {
            "type": "string",
            "description": "Path to the destination folder (where you want your files symlinked to)",
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
            #"choices": ["Torbox", "RealDebrid"]
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
            "description": "Sync deletions from the Database to Plex",
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
        "wake_limit": {
            "type": "string",
            "description": "Number of times to wake items before blacklisting",
            "default": "3"
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
                "size_weight": {"type": "integer", "default": 3, "min": 0},
                "bitrate_weight": {"type": "integer", "default": 3, "min": 0},
                "preferred_filter_in": {"type": "list", "default": []},
                "preferred_filter_out": {"type": "list", "default": []},
                "filter_in": {"type": "list", "default": []},
                "filter_out": {"type": "list", "default": []},
                "min_size_gb": {"type": "float", "default": 0.01, "min": 0},
                "max_size_gb": {"type": "float", "default": float('inf'), "min": 0}
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
        "console_logging_level": {
            "type": "string",
            "description": "Console logging level",
            "default": "INFO",
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        },
        "skip_initial_plex_update": {
            "type": "boolean",
            "description": "Skip Plex initial collection scan",
            "default": False
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
            "description": "Rescrape items that are missing their associated file (i.e. if Plex Library cleanup is enabled)",
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
                "Example: {title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} -{imdb_id} - {version} - ({original_filename})",
            ],
            "default": "{title} ({year})/Season {season_number:02d}/{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} -{imdb_id} - {version} - ({original_filename})"
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
            "Comet": {
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
                "display_name": {"type": "string", "default": "Trakt Watchlist"}
            },
            "Trakt Lists": {
                "enabled": {"type": "boolean", "default": False},
                "trakt_lists": {"type": "string", "default": ""},
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Trakt Lists"}
            },
            "Overseerr": {
                "enabled": {"type": "boolean", "default": False},
                "url": {"type": "string", "default": "", "validate": "url"},
                "api_key": {"type": "string", "default": "", "sensitive": True},
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Overseerr"}
            },
            "Plex Watchlist": {
                "enabled": {"type": "boolean", "default": False},
                "versions": {"type": "dict", "default": {"Default": True}},
                "display_name": {"type": "string", "default": "Plex Watchlist"}
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
                        "upgrading": False
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
                        "upgrading": False
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
                        "upgrading": False
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
                        "upgrading": False
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
    }
}