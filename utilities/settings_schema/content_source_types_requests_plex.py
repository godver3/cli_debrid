"""Content source types: Overseerr, Agregarr and the Plex watchlists."""

TYPES = {
    "Overseerr": {
        "enabled": {"type": "boolean", "default": False},
        "url": {"type": "string", "default": "", "validate": "url"},
        "api_key": {"type": "string", "default": "", "sensitive": True},
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Overseerr"},
        "allow_partial_requests": {"type": "boolean", "default": False, "description": "Only add the seasons that were requested in Seerr instead of the whole show."},
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
        "remove_when_collected": {"type": "boolean", "default": False, "description": "Remove items from this watchlist once they have been collected. Movies are removed when every wanted version is collected; shows only once the series has ended (a running show stays so new episodes keep being added)."},
        "keep_series": {"type": "boolean", "default": False, "description": "Never remove series from this watchlist, even ended ones; only movies are removed."},
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
        "username": {"type": "string", "default": "", "description": "plex.tv username of the account. For a managed Plex Home user (no plex.tv username), use the profile name."},
        "token": {"type": "string", "default": "", "sensitive": True},
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Other Plex Watchlist"},
        "remove_when_collected": {"type": "boolean", "default": False, "description": "Remove items from this watchlist once they have been collected. Movies are removed when every wanted version is collected; shows only once the series has ended (a running show stays so new episodes keep being added)."},
        "keep_series": {"type": "boolean", "default": False, "description": "Never remove series from this watchlist, even ended ones; only movies are removed."},
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
        "url": {"type": "string", "default": "", "validate": "url", "description": "Note: Plex caps watchlist RSS feeds at the 25 most recent items and may serve them up to ~48h stale; use 'My Plex Watchlist' for the full list."},
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "My Plex RSS Watchlist"},
        "remove_when_collected": {"type": "boolean", "default": False, "description": "RSS feeds are read-only, so collected items are skipped on later runs instead of removed. Movies are skipped when every wanted version is collected; shows only once the series has ended."},
        "keep_series": {"type": "boolean", "default": False, "description": "Never skip series, even ended ones; only movies are skipped."},
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
        "url": {"type": "string", "default": "", "validate": "url", "description": "Note: Plex caps watchlist RSS feeds at the 25 most recent items and may serve them up to ~48h stale; use 'Plex Friends Watchlist' for full, current friends' watchlists."},
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "My Friends Plex RSS Watchlist"},
        "remove_when_collected": {"type": "boolean", "default": False, "description": "RSS feeds are read-only, so collected items are skipped on later runs instead of removed. Movies are skipped when every wanted version is collected; shows only once the series has ended."},
        "keep_series": {"type": "boolean", "default": False, "description": "Never skip series, even ended ones; only movies are skipped."},
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
    "Plex Friends Watchlist": {
        "enabled": {"type": "boolean", "default": False},
        "friends": {"type": "string", "default": "", "description": "Comma-separated Plex usernames or display names of friends whose watchlists to add. Leave empty for all friends. Uses your main Plex token; each friend must allow their watchlist to be shared with friends in their Plex privacy settings."},
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Plex Friends Watchlist"},
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
}
