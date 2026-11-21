"""Content source types: Scrob, Floppy and Simkl."""

TYPES = {
    "Scrob Lists": {
        "enabled": {"type": "boolean", "default": False},
        "scrob_list_ids": {
            "type": "string",
            "description": "Comma-separated Scrob list IDs (numeric, from Settings → Connections → API Key page or the list's URL in the Scrob UI), e.g. '2,7'. Requires Scrob URL/API Key to be configured under Settings → Tracking accounts → Scrob.",
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
    "Floppy Watchlist": {
        "enabled": {"type": "boolean", "default": False},
        "floppy_statuses": {
            "type": "list",
            "description": "Which Floppy statuses to add. Planning is Floppy's watchlist. Requires the Floppy URL/API key under Settings → Tracking accounts.",
            "default": ["Planning"],
            "choices": ["Planning", "In progress", "Paused", "Completed", "Dropped"]
        },
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Floppy Watchlist"},
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
    "Floppy Lists": {
        "enabled": {"type": "boolean", "default": False},
        "floppy_list_ids": {
            "type": "string",
            "description": "Comma-separated Floppy list IDs (the number in the list's URL, e.g. /lists/3), e.g. '3,7'. Requires the Floppy URL/API key under Settings → Tracking accounts.",
            "default": ""
        },
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Floppy Lists"},
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
    "Simkl Watchlist": {
        "enabled": {"type": "boolean", "default": False},
        "simkl_statuses": {
            "type": "list",
            "description": "Which Simkl lists to add. Plan to Watch is Simkl's watchlist; Watching and On Hold only apply to shows and anime. Requires Simkl sign-in under Settings → Tracking accounts.",
            "default": ["Plan to Watch"],
            "choices": ["Plan to Watch", "Watching", "On Hold", "Completed", "Dropped"]
        },
        "include_anime": {
            "type": "boolean",
            "description": "Also add anime from your Simkl anime list (Simkl keeps anime separate from shows).",
            "default": True
        },
        "versions": {"type": "dict", "default": {"Default": True}},
        "media_type": {"type": "string", "default": "All", "choices": ["All", "Movies", "Shows"]},
        "display_name": {"type": "string", "default": "Simkl Watchlist"},
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
}
