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
    "RealDebrid": {
        "tab": "Required Settings",
        "api_key": {
            "type": "string",
            "description": "Real-Debrid API key",
            "default": "",
            "sensitive": True
        }
    },
    "TMDB": {
        "tab": "Additional Settings",
        "api_key": {
            "type": "string",
            "description": "TMDB Read Access Token",
            "default": "",
            "sensitive": True
        }
    },
    "Metadata Battery": {
        "tab": "Required Settings",
        "url": {
            "type": "string",
            "description": "Metadata Battery URL. Leave as default (http://cli_battery_app:50051) unless you have set up the Metadata Battery in a different location.",
            "default": "http://cli_battery_app:50051"
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
        }
    },
    "Scraping": {
        "tab": "Versions",
        "uncached_content_handling": {
            "type": "string",
            "description": [
                "Uncached content management in the program queue:",
                "None: Only take the best Cached result",
                "Hybrid: Take the first best Cached result, and if no Cached results found, take the best Uncached result",
                "Full: Take the best result, whether it's Cached or Uncached"
            ],
            "default": "None",
            "choices": ["None", "Hybrid", "Full"]
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
            "description": "Override Content Source checking period (in minutes)",
            "default": {},
            "schema": {
                "*": {"type": "integer", "min": 1}
            }
        },
        "checking_queue_period": {
            "type": "integer",
            "description": "Checking queue max period (in minutes) before moving items back to Wanted queue",
            "default": 3600
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
                "chat_id": {"type": "string", "default": ""}
            },
            "Discord": {
                "enabled": {"type": "boolean", "default": False},
                "webhook_url": {"type": "string", "default": "", "sensitive": True}
            },
            "Email": {
                "enabled": {"type": "boolean", "default": False},
                "smtp_server": {"type": "string", "default": ""},
                "smtp_port": {"type": "integer", "default": 587},
                "smtp_username": {"type": "string", "default": ""},
                "smtp_password": {"type": "string", "default": "", "sensitive": True},
                "from_address": {"type": "string", "default": ""},
                "to_address": {"type": "string", "default": ""}
            }
        }
    }
}