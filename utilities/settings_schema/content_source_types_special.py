"""Content source types: friends Trakt, special Trakt/Scrob lists and Adaptive List."""

TYPES = {
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
    },
}
