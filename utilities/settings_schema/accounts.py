"""SSO, UI, Plex, file management and debrid/usenet provider settings."""

from ._logos import AVAILABLE_LOGOS

SECTIONS = {
    "SSO": {
        "tab": "Additional Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable SSO / OIDC login",
            "default": False
        },
        "provider": {
            "type": "string",
            "description": "OIDC provider type",
            "default": "authentik",
            "choices": ["authentik", "authelia", "generic"]
        },
        "discovery_url": {
            "type": "string",
            "description": "OIDC discovery URL (e.g. https://auth.example.com/application/o/cli-debrid/.well-known/openid-configuration)",
            "default": ""
        },
        "client_id": {
            "type": "string",
            "description": "OIDC client ID",
            "default": ""
        },
        "client_secret": {
            "type": "string",
            "description": "OIDC client secret",
            "default": ""
        },
        "default_role": {
            "type": "string",
            "description": "Default role for new SSO users",
            "default": "user",
            "choices": ["user", "requester", "admin"]
        },
        "auto_provision": {
            "type": "boolean",
            "description": "Automatically create accounts for new SSO users",
            "default": True
        },
        "redirect_uri_base": {
            "type": "string",
            "description": "Public base URL for the OIDC callback (e.g. https://cli.mash2k3.us). Leave blank to auto-detect from request.",
            "default": ""
        },
        "disable_local_auth": {
            "type": "boolean",
            "description": "Disable local username/password login (SSO only)",
            "default": False
        }
    },
    "UI Settings": {
        "tab": "Additional Settings",
        "enable_user_system": {
            "type": "boolean",
            "description": "Enable user account system",
            "default": True
        },
        "use_24hour_format": {
            "type": "boolean",
            "description": "Use 24-hour time format instead of 12-hour",
            "default": True
        },
        "compact_view": {
            "type": "boolean",
            "description": "Use compact view for statistics page",
            "default": False
        },
        "enable_phalanx_db": {
            "type": "boolean",
            "description": "Enable the phalanx_db service (requires restart)",
            "default": False
        },
        "disable_auto_browser": {
            "type": "boolean",
            "description": "Disable automatic browser launch on Windows systems",
            "default": False
        },
        "program_logo": {
            "type": "string",
            "description": "Select the program logo to display in the UI. Credits to:@mrcuriousny for Plex-Inspired logo",
            "default": "Default",
            "choices": AVAILABLE_LOGOS
        },
        "hide_support_message": {
            "type": "boolean",
            "description": "Hide the Patreon support message in the header",
            "default": False
        },
        "recently_added_limit": {
             "type": "integer",
             "description": "Amount of items displayed for recently added on statistics page",
             "default": 5,
             "min": 5,
             "max": 14
         },
        "recently_upgraded_limit": {
             "type": "integer",
             "description": "Amount of items displayed for recently upgraded on statistics page",
             "default": 5,
             "min": 5,
             "max": 14
         },
        "upcoming_releases_start_limit": {
             "type": "integer",
             "description": "How many days back should be displayed on statistics page, 0 being today and 7 being a week back.",
             "default": 0,
             "min": 0
         },
        "upcoming_releases_end_limit": {
             "type": "integer",
             "description": "How many days forward should be displayed on statistics page",
             "default": 28,
             "min": 0
         },
        "date_format": {
             "type": "string",
             "description": "Set your preferred date format. <a href='#' class='format-help-link' data-modal='dateTimeFormatModal'>Click here</a> to see the parameter list.",
             "default": "%Y-%m-%d"
         },
        "time_format": {
             "type": "string",
             "description": "Set your preferred time format. <a href='#' class='format-help-link' data-modal='dateTimeFormatModal'>Click here</a> to see the parameter list.",
             "default": "%H:%M:%S"
         },
        "enable_caching": {
            "type": "boolean",
            "description": "Enable web caching and compression for faster page loads. This improves performance by caching static assets (CSS, JavaScript, images) and compressing responses. Disable if experiencing issues with updates not appearing.",
            "default": False
        },
        "stats_provider_priority": {
            "type": "string",
            "description": "Choose which provider's stats to display on the Statistics page. Auto shows Combined when both debrid and usenet are configured, otherwise whichever one is. Select Usenet to always show cli_mount/usenet stats even when a debrid provider is also configured. Select Combined to show the total size across every debrid provider and usenet together, the debrid subscription days, and broken entries split into NZB and debrid.",
            "default": "auto",
            "choices": ["auto", "debrid", "usenet", "combined"]
        }
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
            "description": "Comma-separated list of Plex movie library names or IDs (e.g., 'Movies,4K Movies' or '1,5')",
            "default": "Movies"
        },
        "shows_libraries": {
            "type": "string",
            "description": "Comma-separated list of Plex TV show library names or IDs (e.g., 'TV Shows,Anime' or '2,3')",
            "default": "Shows"
        },
        "update_plex_on_file_discovery": {
            "type": "boolean",
            "description": "Update Plex on file discovery (cli_debrid must be able to access mount at the below location)",
            "default": False
        },
        "mounted_file_location": {
            "type": "string",
            "description": "The single path cli-debrid checks for file presence (in Zurg use the /__all__ folder). When you run more than one provider (e.g. Real-Debrid via zurg AND Usenet via NzbDAV), point this at one combined mount that contains all of them — e.g. a mergerfs union of the providers' mounts — so file checks succeed regardless of which provider holds the item. May differ from the per-provider mount path set under Usenet Provider.",
            "default": "/mnt/zurg/__all__"
        },
        "disable_plex_library_checks": {
            "type": "boolean",
            "description": "Disable Plex library checks - if enabled use the mounted_file_location above to confirm file presence for Collection. If no file location indicated, immediately mark as Collected on addition. This setting is essentially a Local-Only mode, to allow for third party symlinking",
            "default": False
        }
    },
    "File Management": {
        "tab": "Required Settings",
        "file_collection_management": {
            "type": "string",
            "description": "How your library is built. Direct Mount: your media server (Plex or Silo) reads the mount directly. Symlinked/Local: cli_debrid makes a folder of symlinks into the mount, and your media server (Plex, Jellyfin, Emby or Silo) scans that. On Windows with Plex use Direct Mount: Plex Media Server for Windows skips symlinked files, so a Symlinked/Local library never appears in it.",
            "default": "Plex",
            "choices": ["Plex", "Symlinked/Local"],
            # Shown instead of the stored value ('Plex' stays the saved value)
            "choice_labels": {"Plex": "Direct Mount"}
        },
        "original_files_path": {
            "type": "string",
            "description": "Path to the original files (in Zurg use the /__all__ folder).",
            "default": "/mnt/zurg/__all__"
        },
        "symlinked_files_path": {
            "type": "string",
            "description": "Path to the destination folder (where you want your files linked to).",
            "default": "/mnt/symlinked"
        },
        "symlink_organize_by_type": {
            "type": "boolean",
            "description": "Organize symlinked files into Movies and TV Shows folders",
            "default": True
        },
        "symlink_organize_by_resolution": {
            "type": "boolean",
            "description": "Organize symlinked files by resolution (e.g., 1080p, 2160p) before media type folders",
            "default": False
        },
        "symlink_organize_by_version": {
            "type": "boolean",
            "description": "Organize symlinked files by version (e.g., Remux, WEB-DL) before media type folders",
            "default": False
        },
        "symlink_folder_order": {
            "type": "string",
            "description": "Defines the customizable order of organizational folders for symlinks. Use a comma-separated list containing 'type', 'version', and 'resolution' in your desired order (e.g., 'version,type,resolution'). The individual 'Organize by X' toggles still control if a specific folder component is included in the path.",
            "default": "type,version,resolution"
        },
        "scan_mount_for_external_adds": {
            "type": "boolean",
            "description": "Periodically scan the original files path for content added outside cli_debrid (e.g. via DMM or the debrid provider's own UI) and import it - symlink it and trigger a media server scan. Use this when your mount provider has no library-update webhook (Decypharr, plain rclone); zurg users already get this via its on_library_update hook. The first run records existing mount content as a baseline without importing it.",
            "default": False
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
        },
        "media_server_type": {
            "type": "string",
            "description": "Media server type to use for symlink updates when using Symlinked/Local file collection management",
            "default": "plex",
            "choices": ["plex", "jellyfin", "silo"]
        },
        "plex_mode_media_server": {
            "type": "string",
            "description": "Direct Mount: the media server that reads the mount and tracks your library. Silo works like Plex here: it reads the mount's folders, and cli_debrid asks it what it has. Silo needs its URL and an admin API key.",
            "default": "plex",
            "choices": ["plex", "silo"]
        }
    },
    "Debrid Provider": {
        "tab": "Required Settings",
        "provider": {
            "type": "string",
            "description": "Debrid service provider. AllDebrid note: AllDebrid can't report whether a release is cached without adding it to your account, and it has suspended accounts that add many releases without downloading them. cli_debrid limits AllDebrid cache checks to 150 per hour and 10 per item and removes the ones it doesn't use; items over the limit wait in Adding. When another provider is also set up, AllDebrid is only checked if the providers ranked above it don't have the release cached.",
            "default": "RealDebrid",
            "choices": ["RealDebrid", "AllDebrid", "Premiumize", "Torbox", "DebridLink", "HighWay"]
        },
        "api_key": {
            "type": "string",
            "description": "API key for the debrid service",
            "default": "demo_key",
            "sensitive": True
        },
        "enable_debrid_naming": {
            "type": "boolean",
            "description": "Name debrid torrent folders in cli_mount's DFS mount using a structured format: {title} ({year}) - {imdb-id} - {version} - (original) for movies and {title} ({year}) - SxxExx - {episode title} - {imdb-id} - {version} - (original) for episodes. Requires cli_mount as the usenet provider (URL configured). Renames the entry in cli_mount only (single-file torrents get the file renamed too; the folder shows the new name only when cli_mount's Folder Naming is 'File name') — the actual file on the debrid service is unchanged.",
            "default": False
        },
        "include_version_in_debrid_naming": {
            "type": "boolean",
            "description": "Include the version (e.g. Default, 4K Remux) in the debrid folder name when Debrid File Naming is enabled.",
            "default": True
        },
        "include_content_source_in_debrid_naming": {
            "type": "boolean",
            "description": "Include the content source display name in the debrid folder name when Debrid File Naming is enabled.",
            "default": False
        },
        "ffprobe_all_debrid_additions": {
            "type": "boolean",
            "description": "Before collecting a debrid (Real-Debrid/etc.) file, probe it with ffprobe to confirm it's actually playable, on top of the normal checks. Works in both Symlinked/Local and Direct Mount — in Direct Mount it runs the moment the file is found on the mount, before cli_debrid tells Plex about it, so a confirmed-broken file is rejected and re-scraped before it ever shows up in Plex. Requires ffprobe to be installed. This will slow down how long it takes items to reach Collected.",
            "default": False
        }
    },
    "Usenet Provider": {
        "tab": "Required Settings",
        "enabled": {
            "type": "boolean",
            "description": "Enable usenet as a download source via cli_mount",
            "default": False
        },
        "url": {
            "type": "string",
            "description": "Mount URL: where cli_debrid reaches the usenet mount (cli_mount, NzbDAV or Zurg). Settings → Required → Usenet Mount → Detect looks for it. For cli_mount use its host or container name and port, e.g. http://cli_mount:8282, not localhost:8888 (that is cli_debrid's own Phalanx DB).",
            "default": ""
        },
        "api_token": {
            "type": "string",
            "description": "cli_mount API token (leave empty if auth is disabled)",
            "default": "",
            "sensitive": True
        },
        "download_folder": {
            "type": "string",
            "description": "Download folder category in cli_mount (leave empty for default)",
            "default": ""
        },
        "data_path": {
            "type": "string",
            "description": "Path to cli_mount's data directory inside the container — bind mount your cli_mount host data folder to /climount_data in your docker-compose (e.g. - /mnt/data/appdata/climount:/climount_data) then set this to /climount_data. Required for the cli_mount cleanup tool.",
            "default": "/climount_data"
        },
        "enable_nzb_naming": {
            "type": "boolean",
            "description": "Name NZB jobs submitted to the Usenet provider using a structured format with title, year, IMDb ID, version and original filename. Applies to movies, episodes, season packs and virtual aggregate packs.",
            "default": False
        },
        "include_version_in_nzb_naming": {
            "type": "boolean",
            "description": "Include the version (e.g. Default, 4K Remux) in the NZB job title when NZB File Naming is enabled.",
            "default": True
        },
        "include_content_source_in_nzb_naming": {
            "type": "boolean",
            "description": "Include the content source display name in the NZB job title when NZB File Naming is enabled.",
            "default": False
        },
        "retention_days": {
            "type": "integer",
            "description": "Maximum age of NZB results in days. Results older than this are filtered out before submission. Set to 0 to disable. Applies everywhere NZB indexers are searched.",
            "default": 5000
        },
        "disable_nzb_season_packs": {
            "type": "boolean",
            "description": "Reject NZB season packs entirely (movies are unaffected). A single damaged article in a season pack repairs the whole pack via a fresh grab; with this enabled, only aggregate/single-episode NZB results are considered, so a bad file only affects that one episode.",
            "default": True
        },
        "ffprobe_all_nzbs": {
            "type": "boolean",
            "description": "Before collecting an NZB file, probe it with ffprobe to confirm it's actually playable, on top of the existing missing-article/segment health check. Works in both Symlinked/Local and Direct Mount — in Direct Mount it runs the moment the file is found on the mount, before cli_debrid tells Plex about it, so a confirmed-broken file is rejected and re-scraped before it ever shows up in Plex. Requires ffprobe to be installed. This will slow down how long it takes NZB items to reach Collected.",
            "default": False
        },
        "provider": {
            "type": "string",
            "description": "Which usenet backend to use: cli_mount, NzbDAV, Zurg (Zurg 2.0's SABnzbd-compatible endpoint, or any other flat-layout SAB-emulation mount), or builtin (cli_debrid's own optional mount engine, configured under Built-in Mount; the container needs /dev/fuse). Zurg reuses the NzbDAV submit/poll/health-check logic unchanged, but resolves the job folder directly under the mount root instead of walking a <category>/<job> subfolder layout — required because Zurg's mount stores content flat and has no per-category subdirectories.",
            "default": "builtin",
            "choices": ["climount", "nzbdav", "zurg", "builtin"]
        },
        "owned_categories": {
            "type": "string",
            "description": "NzbDAV/Zurg only. Comma-separated list of nzbdav categories the repair/health tool may act on. nzbdav history is shared with other SAB clients (e.g. Lidarr music), and repair can only re-acquire content cli-debrid manages — so it must never touch another app's entries. Leave empty to auto-pick the categories cli-debrid grabs into (movies, shows, movies_1080p_264, shows_1080p_264, plus the download-folder fallback). Set this only if your category names differ.",
            "default": ""
        },
        "exclude_categories": {
            "type": "string",
            "description": "NzbDAV/Zurg only. Comma-separated nzbdav categories the repair/health tool must ignore, subtracted from the included set. Use this if you point Radarr/Sonarr at the same nzbdav and don't want cli-debrid touching their categories.",
            "default": ""
        },
        "nzbdav_category_map": {
            "type": "string",
            "description": "NzbDAV/Zurg only. Optional. Choose which categories you want and what they're named on your instance, as comma-separated bucket=name pairs, e.g. `movies=movies, shows=shows, movies_1080p=movies_1080p, shows_1080p=shows_1080p, fallback=__unplayable__`. Detected buckets you omit fall back to their parent (movies_2160p_remux → movies_2160p → movies), so items never land in a category that doesn't exist on your instance. Recognised buckets: movies, shows, movies_1080p, shows_1080p, movies_2160p, shows_2160p, movies_1080p_remux, movies_2160p_remux, anime_movies, anime_shows, music, and 'fallback'. Leave empty to use the full default taxonomy. The setup helper's required-category list and the repair scope follow this map automatically.",
            "default": ""
        },
    },
}
