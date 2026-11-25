"""Notifications, subtitles, post-processing, load regulation, library, Discover, Bazarr, AI, overlays and Plex collections."""

SECTIONS = {
    "Notifications": {
        "tab": "Notifications",
        "type": "dict",
        "description": "Notification configurations",
        "default": {},
        "schema": {
            "General": {
                "enabled_content_sources": {
                    "type": "string",
                    "description": "Comma-separated list of content sources to include in notifications. Leave empty to include all sources.",
                    "default": ""
                }
            },
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
            "description": "Download subtitles when an item is collected. Symlinked/Local writes a sidecar file; Direct Mount uploads to the Plex item. Movies and TV shows.",
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
            "description": "OpenSubtitles.com username (free account). Needed to download from OpenSubtitles.com, the largest provider; also used for the legacy OpenSubtitles.org provider.",
            "default": "",
            "sensitive": False
        },
        "opensubtitles_password": {
            "type": "string",
            "description": "OpenSubtitles password for subtitle downloads",
            "default": "",
            "sensitive": True
        },
        "opensubtitles_org_username": {
            "type": "string",
            "description": "OpenSubtitles.org username for the legacy 'opensubtitles' provider. Blank uses the OpenSubtitles.com login.",
            "default": "",
            "sensitive": False
        },
        "opensubtitles_org_password": {
            "type": "string",
            "description": "OpenSubtitles.org password for the legacy 'opensubtitles' provider.",
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
            "description": "Subtitle providers to search (none need a captcha). All selected providers are searched; the best match wins, then the closest release name, then the most downloaded. opensubtitlescom needs the OpenSubtitles login above to download; gestdown is an Addic7ed mirror (TV only); subtitulamos is Spanish; opensubtitles is the legacy .org API.",
            "default": ["opensubtitlescom", "gestdown", "tvsubtitles"],
            "choices": ["opensubtitlescom", "subdl", "subsource", "gestdown", "tvsubtitles", "subtitulamos", "bsplayer", "opensubtitles"]
        },
        "subdl_api_key": {
            "type": "string",
            "description": "SubDL API key (free: subdl.com account → API). Large catalog, strong for TV. Add 'subdl' to the providers to use it.",
            "default": "",
            "sensitive": True
        },
        "subsource_api_key": {
            "type": "string",
            "description": "SubSource API key (free: subsource.net profile → API). Large catalog. Add 'subsource' to the providers to use it.",
            "default": "",
            "sensitive": True
        },
        "text_subtitles_only": {
            "type": "boolean",
            "description": "Avoid subtitle formats that make Plex/Jellyfin/Emby transcode. Downloaded styled ASS/SSA subtitles are converted to plain SRT, and with the embedded probe on, an embedded image (PGS/VobSub) or styled track no longer counts as having that language, so an SRT is fetched.",
            "default": False
        },
        "user_agent": {
            "type": "string",
            "description": "User agent for subtitle API requests",
            "default": "SubDownloader/1.0 (your-email@example.com)"
        },
        "ignore_embedded_subtitles": {
            "type": "boolean",
            "description": "Never count subtitles embedded in the video file: every configured language is fetched as a separate subtitle (shown as missing until one is downloaded). Off: an embedded text track in a language (not forced, not commentary, not an image/styled format that makes players transcode) counts as having it.",
            "default": False
        },
        "probe_for_embedded_subtitles": {
            "type": "boolean",
            "description": "Skip languages the file already has as an embedded (non-forced) subtitle track; if all are embedded, the search and Bazarr notification are skipped. Reuses the add-time ffprobe playability check's header read when that's on, otherwise reads the file header once. Requires ffprobe. In Direct Mount only the add-time check's result is used.",
            "default": False
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
    },
    "System Load Regulation": {
        "tab": "Additional Settings",
        "cpu_threshold_percent": {
            "type": "integer",
            "description": "CPU usage percentage threshold to trigger an increase in sleep time.",
            "default": 75,
            "min": 1,
            "max": 100
        },
        "ram_threshold_percent": {
            "type": "integer",
            "description": "RAM usage percentage threshold to trigger an increase in sleep time.",
            "default": 75,
            "min": 1,
            "max": 100
        },
        "regulation_increase_step_seconds": {
            "type": "float",
            "description": "Amount of time (in seconds) to increase the sleep duration by when load is high.",
            "default": 1.0,
            "min": 0.0,
            "step": 0.1
        },
        "regulation_decrease_step_seconds": {
            "type": "float",
            "description": "Amount of time (in seconds) to decrease the sleep duration by when load is normal.",
            "default": 1.0,
            "min": 0.0,
            "step": 0.1
        },
        "regulation_max_sleep_seconds": {
            "type": "float",
            "description": "The maximum sleep time (in seconds) that auto-regulation can set.",
            "default": 60.0,
            "min": 0.0,
            "step": 0.1
        }
    },
    "Library Manager": {
        "tab": "Additional Settings",
        "ghostlist_mode": {
            "type": "boolean",
            "description": "Add shows and movies to the ghostlist to prevent them from being re-added. When enabled, deleting content will mark it as ghosted in the database. When disabled, deleted content can be re-added by dynamic content sources.",
            "default": False
        },
        "remove_from_content_sources": {
            "type": "boolean",
            "description": "Remove items from content sources (Trakt, Overseerr, etc.) during deletion. Enable when ghostlist OFF (prevents re-addition, slower). Disable when ghostlist ON (faster, ghostlist already prevents re-addition).",
            "default": True
        },
        "hide_season_zero": {
            "type": "boolean",
            "description": "Hide Season 0 (Specials) tab and its episodes from the TV show page.",
            "default": True
        }
    },
    "Discover Settings": {
        "tab": "Additional Settings",
        "hide_no_rating": {
            "type": "boolean",
            "description": "Hide items without a rating from discover results",
            "default": False
        },
        "hide_no_poster": {
            "type": "boolean",
            "description": "Hide items without a poster image from discover results",
            "default": False
        },
        "only_show_missing": {
            "type": "boolean",
            "description": "Only show items that are not in your library (hide items already in database)",
            "default": False
        },
        "tv_show_episode_view": {
            "type": "string",
            "description": "Where to route when clicking on TV shows not in library (movies always go to Discover details)",
            "default": "discover",
            "choices": ["discover", "add_media"]
        },
        "hide_specials": {
            "type": "boolean",
            "description": "Hide TV show specials (Season 0) from the Discover details page season list",
            "default": True
        }
    },
    "Bazarr Integration": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable Radarr/Sonarr API spoofing for Bazarr subtitle integration. When enabled, Clyde will expose API endpoints that allow Bazarr to connect as if this were Radarr/Sonarr.",
            "default": False
        },
        "api_key": {
            "type": "string",
            "description": "API key for Bazarr authentication. Click 'Generate' to create a new key, or enter your own. This key must be used when configuring Bazarr.",
            "default": "",
            "sensitive": True
        },
        "service_type": {
            "type": "string",
            "description": "Which service to emulate. 'Auto' exposes both Radarr (movies) and Sonarr (TV) endpoints.",
            "default": "auto",
            "choices": ["auto", "radarr", "sonarr"]
        },
        "spoofed_version": {
            "type": "string",
            "description": "Version number to report to Bazarr. Change only if Bazarr requires a specific version.",
            "default": "5.14.0.9383"
        },
    },
    "AI Assistant": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable the AI Butler chat widget. Requires OpenClaw to be installed and configured.",
            "default": False
        },
        "openclaw_url": {
            "type": "string",
            "description": "URL of your OpenClaw instance (e.g. http://192.168.1.x:18789). OpenClaw handles AI provider selection and session memory.",
            "default": ""
        },
        "openclaw_token": {
            "type": "string",
            "description": "Bearer token for OpenClaw authentication. Leave blank if OpenClaw has no auth configured.",
            "default": "",
            "sensitive": True
        },
        "agent_id": {
            "type": "string",
            "description": "OpenClaw agent ID to use (default: main).",
            "default": "main"
        },
        "health_notifications": {
            "type": "boolean",
            "description": "Send proactive health alerts via your configured notification channels when the AI Butler detects issues (stuck queues, high blacklist rate, errors, etc.).",
            "default": True
        },
        "health_check_interval": {
            "type": "integer",
            "description": "How often (in seconds) the AI Butler runs background health checks. Minimum 300 (5 minutes).",
            "default": 900
        },
        "enable_settings_assistant": {
            "type": "boolean",
            "description": "Phase 2 — Settings Assistant: Allows the AI to suggest and apply setting changes directly from chat. When enabled, the AI can output Apply buttons in its responses that save a setting with one click, and can trigger a program restart after applying changes. When disabled, the AI becomes read-only — it can still explain settings but cannot change them, and the /api/ai/apply_setting and /api/ai/restart endpoints will be blocked.",
            "default": True
        },
        "enable_proactive_notifications": {
            "type": "boolean",
            "description": "Phase 3 — Proactive Notifications: Runs a background health monitor that periodically checks for issues (stuck queues, high blacklist rate, error spikes, stalled upgrades, large DB) and sends plain-English alerts via your configured notification channels (Discord, Telegram, etc.). When disabled, no background health checks run and you will not receive AI-generated alerts. The health_notifications and health_check_interval settings below only apply when this is enabled.",
            "default": True
        },
        "enable_recommendations": {
            "type": "boolean",
            "description": "Phase 4 — Content Recommendations & Add to Library: Allows the AI to suggest movies and shows based on your watch history and ratings, and lets you add them to your library with one click from the chat. When enabled, your watch history (from Plex or Trakt) and your full collected library are included in the AI's context so it can make personalised suggestions and avoid recommending things you already have. When disabled, watch history and the full library list are excluded from the prompt, the ADD_TO_LIBRARY action cards will not appear, and the /api/ai/add_to_library endpoint will be blocked.",
            "default": True
        },
        "enable_habit_tracking": {
            "type": "boolean",
            "description": "Phase 5 — Habit Learning & Automation: Records significant actions you take in Clyde (manually triggering content sources, upgrade scans, library adds, program start/stop) to a local ai_habits table. The AI analyses these patterns and proactively suggests automations — for example, if you trigger the same content source every morning it will suggest scheduling it, or if you frequently add items manually it will suggest adding a Trakt list. When disabled, no actions are recorded and the AI will not make automation suggestions. Previously recorded habit data is kept but ignored.",
            "default": True
        },
        "share_full_config": {
            "type": "boolean",
            "description": "Share Full Config with AI: When enabled, the AI receives your complete config.json (with all tokens, passwords and API keys permanently redacted to ***) plus expanded log tails, upgrade hub history, and notification history. This gives the AI much deeper context — it can diagnose configuration problems, explain exactly why a setting is set the way it is, identify conflicting settings across sections, and give more accurate advice. When disabled, the AI only receives a short key-settings summary, which reduces prompt size and limits how much of your configuration is sent to the AI provider but also reduces the AI's ability to help with complex issues.",
            "default": True
        },
        "plex_labels": {
            "type": "object",
            "description": "Plex label settings for items added via AI Butler.",
            "schema": {
                "enabled": {
                    "type": "boolean",
                    "description": "Apply Plex labels to items added to your library via the AI Butler.",
                    "default": False
                },
                "label_mode": {
                    "type": "string",
                    "description": "How to label items: 'fixed' applies the same label to all AI Butler additions.",
                    "default": "fixed",
                    "choices": ["fixed"]
                },
                "fixed_label": {
                    "type": "string",
                    "description": "Label to apply to all items added via AI Butler (e.g. 'AI Butler'). Comma-separated for multiple labels.",
                    "default": "AI Butler"
                }
            }
        }
    },
    "Overlay Settings": {
        "tab": "Additional Settings",
        "overlays_enabled": {
            "type": "boolean",
            "description": "Enable the poster overlay system. When enabled, overlay sync tasks will automatically run and apply media info badges to Plex posters. Requires Plex URL and token to be configured.",
            "default": False
        },
        "textless_posters": {
            "type": "boolean",
            "description": "When enabled, Clyde will fetch a language-neutral (textless) poster from TMDB as the base image. Use the Title Logo badge in the layout editor to add the title as a transparent clearlogo PNG. Note: Not all titles have textless posters on TMDB — the standard English poster is used as fallback.",
            "default": False
        },
        "plex_data_path": {
            "type": "string",
            "description": "Path to the Plex Media Server data directory, the folder that contains Plex's Metadata folder (e.g. /config/Library/Application Support/Plex Media Server). Required for the poster cleanup task to delete old uploaded overlay versions directly from the filesystem. Only uploaded posters Plex reports as not selected, on items Clyde overlaid, are deleted.",
            "default": ""
        },
        "overlay_content_check_interval_days": {
            "type": "number",
            "description": "How old (in days) a title's ratings (IMDb/TMDB/Trakt/RT) can get before they are re-fetched to detect changes that should trigger an overlay refresh. Each sync refreshes up to 40 of the oldest. Set to 0 to refresh every sync. Default is 7 days. Show status and version count changes are checked every sync regardless of this setting.",
            "default": 7
        },
        "sync_items_per_run": {
            "type": "number",
            "description": "Maximum number of shows/movies to process per overlay sync run. Higher values clear backlogs faster but each run takes longer. Default is 200.",
            "default": 200
        }
    },
    "Plex Smart Collections": {
        "tab": "Additional Settings",
        "poster_design": {"type": "number", "description": "Poster layout design ID (0=Plex Default)", "default": 0},
        "poster_accent": {"type": "string", "description": "Accent color hex. Leave empty for design default.", "default": ""},
        "poster_eyebrow": {"type": "string", "description": "Small text above the collection title.", "default": ""},
        "poster_icon": {"type": "string", "description": "Icon path. Leave empty for source default.", "default": ""},
        "poster_overlay_opacity": {"type": "number", "description": "Card overlay opacity 0-100.", "default": 60},
        "poster_glow_opacity": {"type": "number", "description": "Accent glow opacity 0-100.", "default": 80},
        "poster_glow_radius": {"type": "number", "description": "Accent glow radius 10-200.", "default": 55},
        "collections": {"type": "dict", "description": "Per-collection enabled states.", "default": {}}
    },
    "Tags": {
        "tab": "Additional Settings",
        "tags_list": {
            "type": "list",
            "description": "Global tag list for Direct Mount NZB folder routing. Tags are embedded in NZB filenames so cli_mount can route items to virtual folders.",
            "default": []
        }
    },
    "Plex Movie Box Sets": {
        "tab": "Additional Settings",
        "enabled": {"type": "bool", "description": "Enable automatic Plex movie box set collection management.", "default": False},
        "grab_missing": {"type": "bool", "description": "Add missing movies from box sets to the wanted queue.", "default": False},
        "grab_version": {"type": "string", "description": "Version/quality to use when grabbing missing movies.", "default": "Default"},
        "collection_name_pattern": {"type": "string", "description": "Naming pattern for box set collections. {title} = franchise name (e.g. The Godfather).", "default": "{title} Collection"},
        "min_movies": {"type": "number", "description": "Minimum number of owned movies required to create/keep a box set collection.", "default": 2},
        "sort_order": {"type": "string", "description": "Sort order for movies within each box set collection.", "default": "release_date_asc"}
    },
    "Plex Reported Issues": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Replace movies and episodes that Plex users report with Plex's Report Issue (the ... menu on an item). The reported item is sent back to Wanted to get a different release, and the current file keeps playing until the new one is in. Reports already in Plex when this is turned on are left alone. Reports on a whole show or season are only flagged, not replaced. New reports are picked up within about 30 seconds. Same switch as the task in the Task Manager.",
            "default": False
        },
        "reply_to_reporter": {
            "type": "boolean",
            "description": "Reply in the Plex report thread when a report is picked up and again once the item has been replaced.",
            "default": True
        }
    },
}
