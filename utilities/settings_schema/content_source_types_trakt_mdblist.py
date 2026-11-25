"""Content source types: MDBList, Collected, Trakt watchlist/lists/collection."""

TYPES = {
    "MDBList": {
        "enabled": {"type": "boolean", "default": False},
        "source_mode": {
            "type": "string",
            "description": "How this source fetches its items. 'json_url' appends /json to a public MDBList (or compatible) list URL and needs no API key. The 'api_*' modes call the MDBList API directly and require an MDBList API key (Settings -> Tracking accounts -> MDBList).",
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
            "description": "Direct Mount only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
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
            "description": "Direct Mount only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
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
        "remove_when_collected": {"type": "boolean", "default": False, "description": "Remove items from this Trakt watchlist once they have been collected."},
        "keep_series": {"type": "boolean", "default": False, "description": "Keep series on this Trakt watchlist when collected; only movies are removed."},
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
            "description": "Direct Mount only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
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
            "description": "Direct Mount only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
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
            "description": "Direct Mount only: Tags to embed in NZB filenames for items from this source. Requires NZB file naming to be enabled. Format: {tags-Tag1,Tag2} inserted between {imdb-...} and version.",
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
