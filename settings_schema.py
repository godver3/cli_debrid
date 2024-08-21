# settings_schema.py

SETTINGS_SCHEMA = {
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
    "Overseerr": {
        "tab": "Required Settings",
        "url": {
            "type": "string",
            "description": "Overseerr server URL",
            "default": "",
            "validate": "url"
        },
        "api_key": {
            "type": "string",
            "description": "Overseerr API key",
            "default": "",
            "sensitive": True
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
    "Torrentio": {
        "tab": "Required Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable Torrentio scraper",
            "default": False
        }
    },
    "TMDB": {
        "tab": "Additional Settings",
        "api_key": {
            "type": "string",
            "description": "TMDB API key",
            "default": "",
            "sensitive": True
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
            "default": "19"
        },
        "episode_airtime_offset": {
            "type": "string",
            "description": "Offset from the show's airtime to start scraping for new episodes. Positive values are to delay scraping, negative values are to scrape early.",
            "default": "19"
        }
    },
    "Scraping": {
        "tab": "Versions",
        "uncached_content_handling": {
            "type": "string",
            "description": "How to handle uncached content",
            "default": "None",
            "choices": ["None", "Hybrid", "Full"]
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
                "min_size_gb": {"type": "float", "default": 0.01, "min": 0}
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
        },
        "user_watchlist_enabled": {
            "type": "boolean",
            "description": "Enable user watchlist",
            "default": True
        },
        "trakt_lists": {
            "type": "string",
            "description": "Comma-separated list of Trakt list URLs",
            "default": ""
        }
    },
    "Debug": {
        "tab": "Debug Settings",
        "logging_level": {
            "type": "string",
            "description": "Logging level",
            "default": "INFO",
            "choices": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        },
        "skip_initial_plex_update": {
            "type": "boolean",
            "description": "Skip Plex initial collection scan",
            "default": False
        },
        "skip_menu": {
            "type": "boolean",
            "description": "Skip menu",
            "default": False
        },
        "disable_initialization": {
            "type": "boolean",
            "description": "Disable initialization tasks",
            "default": False
        },
        "jackett_seeders_only": {
            "type": "boolean",
            "description": "Return only results with seeders in Jackett",
            "default": False
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
    }
}