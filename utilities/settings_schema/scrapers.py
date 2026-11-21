"""Scraper definitions."""

SECTIONS = {
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
                },
                "api_limit_per_day": {
                    "type": "string",
                    "default": "",
                    "description": "Optional: API hits (searches) this indexer allows per day. Leave blank for no limit. When reached, cli_debrid stops searching this indexer until the limit resets. Set it a little under your real limit if other apps use the same API key."
                },
                "grab_limit_per_day": {
                    "type": "string",
                    "default": "",
                    "description": "Optional: NZB downloads (grabs) this indexer allows per day. Leave blank for no limit. When reached, its results are kept and tried again after the limit resets."
                },
                "limit_reset": {
                    "type": "string",
                    "default": "midnight_local",
                    "choices": ["midnight_local", "midnight_utc", "rolling_24h"],
                    "description": "When the daily limits start over: midnight in this server's time zone, midnight UTC, or a rolling 24 hours (the safest if you don't know what your indexer uses). Only used when a limit is set."
                }
            }
        }
    },
}
