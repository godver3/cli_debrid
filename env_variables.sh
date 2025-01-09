#!/bin/bash

# System environment variables
export USER_CONFIG="/user/config"
export USER_LOGS="/user/logs"
export USER_DB_CONTENT="/user/db_content"
export CLI_DEBRID_PORT="5000"
export CLI_DEBRID_BATTERY_PORT="5001"

# UI Settings
export DEBRID_ENABLE_USER_SYSTEM="true"

# Plex configuration
export DEBRID_PLEX_URL="asdf"
export DEBRID_PLEX_TOKEN=""
export DEBRID_PLEX_MOVIE_LIBRARIES=""
export DEBRID_PLEX_SHOWS_LIBRARIES=""

# File Management
export DEBRID_FILE_MANAGEMENT_TYPE=""
export DEBRID_ORIGINAL_FILES_PATH=""
export DEBRID_SYMLINKED_FILES_PATH=""
export DEBRID_SYMLINK_ORGANIZE_BY_TYPE="true"
export DEBRID_PLEX_URL_FOR_SYMLINK=""
export DEBRID_PLEX_TOKEN_FOR_SYMLINK=""

# Debrid Provider
export DEBRID_DEBRID_PROVIDER=""
export DEBRID_DEBRID_API_KEY=""

# TMDB
export DEBRID_TMDB_API_KEY=""

# Staleness Threshold
export DEBRID_STALENESS_THRESHOLD="7"

# Sync Deletions
export DEBRID_SYNC_DELETIONS="false"

# Metadata Battery
export DEBRID_METADATA_BATTERY_URL=""

# Queue Settings
export DEBRID_QUEUE_WAKE_LIMIT=""
export DEBRID_QUEUE_MOVIE_AIRTIME_OFFSET=""
export DEBRID_QUEUE_EPISODE_AIRTIME_OFFSET=""
export DEBRID_QUEUE_BLACKLIST_DURATION=""

# Scraping Settings
export DEBRID_UNCACHED_CONTENT_HANDLING=""
export DEBRID_UPGRADE_SIMILARITY_THRESHOLD=""
export DEBRID_HYBRID_MODE="false"
export DEBRID_JACKETT_SEEDERS_ONLY="false"
export DEBRID_ULTIMATE_SORT_ORDER=""
export DEBRID_SOFT_MAX_SIZE_GB="false"
export DEBRID_ENABLE_UPGRADING="false"
export DEBRID_ENABLE_UPGRADING_CLEANUP="false"
export DEBRID_DISABLE_ADULT="false"
export DEBRID_TRAKT_EARLY_RELEASES="false"

# Trakt Settings
export DEBRID_TRAKT_CLIENT_ID=""
export DEBRID_TRAKT_CLIENT_SECRET=""

# Debug Settings
export DEBRID_CONSOLE_LOGGING_LEVEL="INFO"
export DEBRID_SKIP_INITIAL_PLEX_UPDATE="false"
export DEBRID_AUTO_RUN="false"
export DEBRID_DISABLE_INIT="false"
export DEBRID_SORT_BY_UNCACHED_STATUS="false"
export DEBRID_CHECKING_QUEUE_PERIOD="3600"
export DEBRID_RESCRAPE_MISSING_FILES="false"
export DEBRID_ENABLE_REVERSE_ORDER_SCRAPING="false"
export DEBRID_DISABLE_NOT_WANTED_CHECK="false"
export DEBRID_PLEX_WATCHLIST_REMOVAL="false"
export DEBRID_PLEX_WATCHLIST_KEEP_SERIES="false"
export DEBRID_SYMLINK_MOVIE_TEMPLATE=""
export DEBRID_SYMLINK_EPISODE_TEMPLATE=""

# Reverse Parser Settings
export DEBRID_DEFAULT_VERSION=""

# Complex configurations (these need to be valid JSON strings)
# Example format for SCRAPING_VERSIONS:
# export DEBRID_SCRAPING_VERSIONS='{"version1":{"enable_hdr":false,"max_resolution":"1080p"}}'
export DEBRID_SCRAPING_VERSIONS='{
  "2160p": {
    "enable_hdr": true,
    "max_resolution": "2160p",
    "resolution_wanted": "==",
    "resolution_weight": 5,
    "hdr_weight": 5,
    "similarity_weight": 3,
    "size_weight": 5,
    "bitrate_weight": 3,
    "min_size_gb": 0.01,
    "max_size_gb": null,
    "filter_in": [],
    "filter_out": ["Telesync", "3D", "HDTS", "HD-TS", "\".TS.\"", "\".CAM.\"", "HDCAM", "Telecine"],
    "preferred_filter_in": [],
    "preferred_filter_out": [["720p", 5], ["TrueHD", 3], ["SDR", 5]]
  },
  "1080p": {
    "enable_hdr": false,
    "max_resolution": "1080p",
    "resolution_wanted": "<=",
    "resolution_weight": 5,
    "hdr_weight": 5,
    "similarity_weight": 3,
    "size_weight": 5,
    "bitrate_weight": 3,
    "min_size_gb": 0.01,
    "max_size_gb": null,
    "filter_in": [],
    "filter_out": ["Telesync", "3D", "HDTS", "HD-TS", "\".TS.\"", "\".CAM.\"", "HDCAM", "Telecine"],
    "preferred_filter_in": [],
    "preferred_filter_out": [["720p", 5], ["TrueHD", 3], ["SDR", 5]]
  }
}'

# Example format for SCRAPERS:
# export DEBRID_SCRAPERS='{"Jackett_1":{"type":"Jackett","enabled":true}}'
export DEBRID_SCRAPERS='{
  "Zilean_1": {
    "type": "Zilean",
    "enabled": true,
    "url": "http://192.168.1.51:8181"
  },
  "Jackett_1": {
    "type": "Jackett",
    "enabled": true,
    "url": "http://192.168.1.51:9117",
    "api": "your_api_key",
    "enabled_indexers": "EZTV, Nyaa.si, YTS"
  }
}'

# Example format for CONTENT_SOURCES:
# export DEBRID_CONTENT_SOURCES='{"source1":{"type":"type1","enabled":true}}'
export DEBRID_CONTENT_SOURCES='{
  "MDBList_1": {
    "versions": ["2160p", "1080p"],
    "enabled": true,
    "urls": "https://mdblist.com/lists/user/trending-movies",
    "display_name": "Trending Movies"
  },
  "Trakt Lists_1": {
    "versions": ["2160p", "1080p"],
    "enabled": true,
    "trakt_lists": "https://trakt.tv/users/user/lists/my-list",
    "display_name": "My Trakt List"
  }
}'

# Example format for NOTIFICATIONS:
# export DEBRID_NOTIFICATIONS='{"notification1":{"type":"Discord","enabled":true}}'
export DEBRID_NOTIFICATIONS='{
  "Discord_1": {
    "type": "Discord",
    "enabled": true,
    "title": "Discord",
    "webhook_url": "your_webhook_url",
    "notify_on": {
      "collected": true,
      "wanted": false,
      "sleeping": false,
      "checking": true
    }
  }
}'