"""Queue and scraping settings."""

SECTIONS = {
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
        "hybrid_mode": {
            "type": "boolean",
            "description": "Enable hybrid mode to add best uncached result if no cached results found in 'None' mode",
            "default": False
        },
        "ultimate_sort_order": {
            "type": "string",
            "description": "Ultimate sort order for scraped results. Recommend leaving off and using existing versioning logic",
            "default": "None",
            "choices": ["None", "Size: large to small", "Size: small to large"]
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
        "upgrade_max_release_age_days": {
            "type": "integer",
            "description": "Only items released this many days ago or less go to the Upgrading queue when collected (default: 7). Edited on the Upgrade Hub.",
            "default": 7,
            "min": 0
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
            "default": True
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
                "foreign_language_penalty": {
                    "type": "number",
                    "default": 30,
                    "description": "Penalty for a release whose name is tagged with an audio language other than the version's language code (e.g. GERMAN for an 'en' version), multiplied by Language Weight. Subtitle tags (SWESUB, VOSTFR, NORDiC) don't count; MULTi, Dual Audio and DL (dual-language) releases are exempt. For a title whose original language isn't the version's (e.g. a Korean show on an 'en' version), releases tagged DUAL or with the version's language get a small tiebreaker bonus instead (this amount × Language Weight, at most 50; half for a bare MULTi/DL). Untagged releases are never penalized. 0 disables both. A cached release is still taken before any uncached one whatever its score, so the penalty alone can't keep out a tagged release that is the only cached match (e.g. a freshly aired episode whose only cached copy is a foreign dub). To drop tagged releases instead, set a large penalty (e.g. 1000) together with a Minimum Scrape Score above 0 (Settings → Scraping; the Delayed Scrape Minimum Score only applies with delayed scrape)."
                },
                "audio_description_penalty": {
                    "type": "number",
                    "default": 10000,
                    "description": "Penalty for an audio-description release (e.g. 'with Audio Description'), whose audio is narrated for blind viewers. The default 10000 ranks one below every other release, so it is only picked when nothing else is available; with a Minimum Scrape Score set (Settings → Scraping), it falls below that and is dropped instead. Use a negative number to prefer them instead, or 0 to treat them like any other release."
                },
                "wake_count": {
                    "type": "integer",
                    "default": None,
                    "description": "Override global wake count limit. Leave empty to use global setting. Set to -1 to disable sleeping queue."
                },
                "fallback_version": {
                    "type": "string",
                    "description": "Version to try instead once this version has exhausted its retries and the item is blacklisted. Select 'None' to disable fallback.",
                    "default": "None"
                },
                "fallback_at_scrape_time": {
                    "type": "boolean",
                    "default": False,
                    "description": "When a scrape finds nothing, try the Fallback Version right away instead of waiting for this version's sleep/blacklist cycle. If the fallback finds results, this item is blacklisted and the fallback version is requested; if not, this item keeps retrying as usual."
                },
                "fallback_after_attempts": {
                    "type": "integer",
                    "default": 0,
                    "description": "With Fallback at Scrape Time: number of failed scrapes before the fallback is tried. 0 or 1 = on the first failure."
                },
                "anime_filter_mode": {
                    "type": "string",
                    "description": "Filter for anime content: 'None' (no filter), 'Anime Only', 'Non-Anime Only'. A show counts as anime when its metadata says so (or it is Japanese animation); Show options on the show's library page can force it either way.",
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
}
