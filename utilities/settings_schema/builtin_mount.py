"""Built-in mount engine settings."""

from utilities.content_routing import DEFAULT_VIRTUAL_FOLDERS_JSON

SECTIONS = {
    "Built-in Mount": {
        "tab": "Required Settings",
        "enabled": {
            "type": "boolean",
            "description": "Set by onboarding (Mount tool = Built-in mount): run the built-in mount for a debrid-only setup. Not shown in Settings. Usenet Provider = builtin always runs it, and whenever it runs it streams your debrid account too.",
            "default": False
        },
        "mount_path": {
            "type": "string",
            "description": "Built-in mount only (Usenet Provider = builtin). Where the engine mounts its files inside the container. Bind it with rshared propagation so Plex sees it, and point File Management's original files path at <mount_path>/__all__.",
            "default": "/mnt/clid"
        },
        "cache_dir": {
            "type": "string",
            "description": "Built-in mount only. Folder for the engine's disk cache of downloaded articles. Use a dedicated folder: the engine evicts files in it.",
            "default": "/user/cache/engine"
        },
        "cache_size_gb": {
            "type": "integer",
            "description": "Built-in mount only. Maximum size of the disk cache in GB (0 disables it).",
            "default": 50
        },
        "read_ahead_mb": {
            "type": "integer",
            "description": "Built-in mount only. Read ahead: megabytes fetched ahead of playback (cli_mount default 16).",
            "default": 16
        },
        "max_connections_per_file": {
            "type": "integer",
            "description": "Built-in mount only. Max connections per file: downloads running at once for one playing file (0 = no limit).",
            "default": 15
        },
        "precache_mb": {
            "type": "integer",
            "description": "Built-in mount only. Pre-cache on open: MB of a file's start and end fetched the moment a player opens it, so the first bytes are ready sooner (0 = off).",
            "default": 2
        },
        "warm_connections": {
            "type": "integer",
            "description": "Built-in mount only. Warm connections per server: kept open and logged in ahead of time so playback doesn't wait for a new connection (backup servers excluded; 0 = off).",
            "default": 2
        },
        "primary_load_mode": {
            "type": "string",
            "description": "Built-in mount only. Primary server load: Fill first uses your first primary server until all its connections are busy and only then the next one. Balanced spreads every request over all primary servers by how busy each is, so each carries load in proportion to its connections. Backup servers are never part of it: they are still only asked for articles the primaries don't have. A server that keeps missing a file's articles is asked last for that file. Takes effect when the mount restarts.",
            "default": "fill",
            "choices": ["fill", "balanced"]
        },
        "playback_reserve_percent": {
            "type": "integer",
            "description": "Built-in mount only. Playback connection reserve: % of each server's connections that only playback may use; read-ahead, imports and health checks never take them (0 = off).",
            "default": 11
        },
        "playback_reserve_min": {
            "type": "integer",
            "description": "Built-in mount only. Reserve at least this many connections per server for playback (when the reserve is on; a server always keeps one connection outside it).",
            "default": 6
        },
        "playback_reserve_max": {
            "type": "integer",
            "description": "Built-in mount only. Reserve at most this many connections per server for playback.",
            "default": 100
        },
        "debug_logging": {
            "type": "boolean",
            "description": "Built-in mount only. Debug logging: the engine also logs every debrid link fetch, refused API call and what each import found in the NZB (archive sets, skipped files). Problems are always logged; turn this on while chasing one.",
            "default": False
        },
        "debrid_chunk_mb": {
            "type": "integer",
            "description": "Built-in mount only. Debrid range size (MB): each debrid download request fetches this much. Larger = fewer requests; debrid CDNs throttle many small ones.",
            "default": 8
        },
        "debrid_connections_per_file": {
            "type": "integer",
            "description": "Built-in mount only. Parallel debrid downloads per file.",
            "default": 4
        },
        "debrid_read_ahead_mb": {
            "type": "integer",
            "description": "Built-in mount only. Debrid read ahead (MB) fetched past what a player reads (0 = same as the usenet read ahead).",
            "default": 32
        },
        "debrid_sync_seconds": {
            "type": "integer",
            "description": "Built-in mount only. How often the debrid account is listed (seconds, 10 or more): new torrents show up on the mount within this time.",
            "default": 30
        },
        "debrid_link_lifetime_hours": {
            "type": "integer",
            "description": "Built-in mount only. Reuse a debrid download link for this many hours before asking for a new one (0 = the provider's own lifetime, e.g. 12 h for Real-Debrid). An expired or refused link is always replaced right away.",
            "default": 0
        },
        "debrid_download_api_keys": {
            "type": "string",
            "description": "Built-in mount only. Real-Debrid only: extra API keys (comma separated) used to get download links when the account's key hits a limit (cli_mount's download_api_keys).",
            "default": "",
            "sensitive": True
        },
        "debrid_proxy": {
            "type": "string",
            "description": "Built-in mount only. Proxy for debrid downloads, e.g. http://user:pass@host:8080 or socks5://host:1080 (empty = none).",
            "default": "",
            "sensitive": True
        },
        "debrid_user_agent": {
            "type": "string",
            "description": "Built-in mount only. User-Agent sent with debrid downloads (empty = default).",
            "default": ""
        },
        "plex_url": {
            "type": "string",
            "description": "Built-in mount only. Plex URL for stream protection, show precache and Now playing, e.g. http://192.168.1.10:32400. Leave empty to use the Plex URL from your other cli_debrid settings (Plex, or Symlink Plex settings).",
            "default": ""
        },
        "plex_token": {
            "type": "string",
            "description": "Built-in mount only. Plex token for the URL above. Leave empty to use the token from your other cli_debrid settings.",
            "default": "",
            "sensitive": True
        },
        "jellyfin_url": {
            "type": "string",
            "description": "Built-in mount only. Jellyfin or Emby URL for stream protection, show precache and Now playing, e.g. http://192.168.1.10:8096. Leave empty to use the Jellyfin/Emby URL from Settings. Works next to Plex: sessions of both are used.",
            "default": ""
        },
        "jellyfin_token": {
            "type": "string",
            "description": "Built-in mount only. Jellyfin/Emby API key for the URL above (Dashboard → API Keys). Leave empty to use the one from Settings.",
            "default": "",
            "sensitive": True
        },
        "plex_playback_protection": {
            "type": "boolean",
            "description": "Built-in mount only. Stream protection: the playback connection reserve is only for files your media servers (Plex, Jellyfin/Emby) report playing (checked every 10 s). Library scans, thumbnails, intro detection and Bazarr can't take it while someone watches. Uses the servers in the Media servers group above; if one can't be reached, every read is treated as playback, as without this.",
            "default": True
        },
        "webdav_enabled": {
            "type": "boolean",
            "description": "Built-in mount only. Also share the mount over WebDAV (read-only), for a media server on another machine (mount it there with rclone) or Windows without WinFsp. Needs a username and password, and the port published in docker-compose.",
            "default": False
        },
        "webdav_port": {
            "type": "integer",
            "description": "Built-in mount only. Port of the WebDAV share (on all interfaces).",
            "default": 8284
        },
        "webdav_username": {
            "type": "string",
            "description": "Built-in mount only. WebDAV username.",
            "default": ""
        },
        "webdav_password": {
            "type": "string",
            "description": "Built-in mount only. WebDAV password.",
            "default": "",
            "sensitive": True
        },
        "plex_prewarm": {
            "type": "boolean",
            "description": "Built-in mount only. Show precache: when someone is far enough into an episode playing from the mount (Plex, Jellyfin or Emby), fetch the start of the next episode in the background so it starts instantly.",
            "default": True
        },
        "prewarm_trigger_percent": {
            "type": "integer",
            "description": "Built-in mount only. Precache when the current episode is this far in (%).",
            "default": 70
        },
        "prewarm_percent": {
            "type": "integer",
            "description": "Built-in mount only. How much of the next episode to fetch (%), up to the size limit below.",
            "default": 20
        },
        "prewarm_max_mb": {
            "type": "integer",
            "description": "Built-in mount only. Precache at most this many MB of the next episode.",
            "default": 256
        },
        "buffer_memory_mb": {
            "type": "integer",
            "description": "Built-in mount only. Buffer memory: RAM in MB for downloaded articles across all open files (the disk cache holds the rest).",
            "default": 512
        },
        "cache_expiry_hours": {
            "type": "integer",
            "description": "Built-in mount only. Cache expiry: drop disk cache entries not used for this many hours (0 = only when the cache is full).",
            "default": 0
        },
        "cache_cleanup_minutes": {
            "type": "integer",
            "description": "Built-in mount only. Cache cleanup interval in minutes (how often cache expiry runs).",
            "default": 1
        },
        "processing_connections": {
            "type": "integer",
            "description": "Built-in mount only. Processing connections per file: parallel downloads while importing an NZB (reading archive headers, PAR2).",
            "default": 8
        },
        "processing_timeout_minutes": {
            "type": "integer",
            "description": "Built-in mount only. Processing timeout per NZB import in minutes. A timeout is retried, never treated as a dead release (0 = no limit).",
            "default": 5
        },
        "import_sample_percent": {
            "type": "integer",
            "description": "Built-in mount only. NZB import availability sample %: share of the main file's articles checked before the release counts as added (0 = off; a proven miss rejects the release, a timeout never does).",
            "default": 1
        },
        "health_sample_percent": {
            "type": "integer",
            "description": "Built-in mount only. Repair availability sample %: share of each file's articles checked by health checks (0 = cli_mount's default, 10% with no cap per file; 100 = every article). Health checks also read each video file's first bytes: a wrong container header marks it broken, as cli_mount does.",
            "default": 0
        },
        "media_probe": {
            "type": "boolean",
            "description": "Built-in mount only. Health checks also open each video file with ffprobe, like cli_mount's 'media probe' repair option: a file ffprobe can't open, or one with no audio or video stream, is marked broken for repair. Reads up to 5 MB per file per check. Replacements are always checked this way.",
            "default": False
        },
        "idle_timeout_minutes": {
            "type": "integer",
            "description": "Built-in mount only. Connection idle timeout: minutes to keep unused usenet connections open before closing them.",
            "default": 5
        },
        "allow_other": {
            "type": "boolean",
            "description": "Built-in mount only. Let other users (Plex) read the mount. Needs user_allow_other in /etc/fuse.conf (set in the image).",
            "default": True
        },
        "uid": {
            "type": "string",
            "description": "Built-in mount only. User ID (UID) that owns the mounted files. Empty = the container's PUID.",
            "default": ""
        },
        "gid": {
            "type": "string",
            "description": "Built-in mount only. Group ID (GID) that owns the mounted files. Empty = the container's PGID.",
            "default": ""
        },
        "umask": {
            "type": "string",
            "description": "Built-in mount only. UMASK: permission bits removed from the read-only files, in octal (e.g. 022; 027 hides files from other users). Empty = none.",
            "default": ""
        },
        "allowed_extensions": {
            "type": "string",
            "description": "Built-in mount only. Allowed file extensions: only these files are listed in the mount (comma separated). Other files (nfo, samples' sidecars, subtitles) stay out of Plex's way. Empty = list everything.",
            "default": "mkv,mp4,m4v,avi,mov,mpg,mpeg"
        },
        "deobfuscate_mode": {
            "type": "string",
            "description": "Built-in mount only. Deobfuscate mode: how new usenet imports name video files whose real name couldn't be found (PAR2 and yEnc names are always used first). A single video file always takes the job's name. Off keeps the posted names.",
            "default": "season_ep",
            "choices": ["season_ep", "index", "off"]
        },
        "virtual_folders": {
            "type": "string",
            "description": "Built-in mount only. Virtual folders: extra top folders listing the jobs that match their filters (cli_mount's custom folders), e.g. separate movies and shows folders for Plex libraries in Plex mode. Edit them on the Mount page. New installs start with Movies and TV Shows (the docs' Content routing filters), so every release lands in exactly one of them.",
            "default": DEFAULT_VIRTUAL_FOLDERS_JSON
        },
        "servers": {
            "type": "string",
            "description": "Built-in mount only. Your usenet servers as a JSON list, tried top to bottom (servers marked backup go last, asked only when the others lack an article). Edit them on the Mount page.",
            "default": "",
            "sensitive": True
        },
        "run_alongside": {
            "type": "boolean",
            "description": "Migration from cli_mount: run the built-in mount next to cli_mount (on its own mount path) while cli_mount is still the usenet backend. Set from the Mount page's Migrate tab.",
            "default": False
        },
        "pre_migration_mount_path": {
            "type": "string",
            "description": "Migration from cli_mount: the built-in mount's path before it took over cli_mount's path (used when switching back).",
            "default": ""
        },
        "api_port": {
            "type": "integer",
            "description": "Built-in mount only. Local port of the engine's API (127.0.0.1 only). Change it only if it clashes.",
            "default": 8283
        },
        "api_token": {
            "type": "string",
            "description": "Built-in mount only. Shared secret between cli_debrid and the engine. Generated automatically when empty.",
            "default": "",
            "sensitive": True
        },
    },
}
