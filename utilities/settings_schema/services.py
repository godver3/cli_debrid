"""Metadata and list service accounts (TMDB, TVDB, MDBList, Trakt, Scrob, Floppy, Simkl) and staleness."""

SECTIONS = {
    "TMDB": {
        "tab": "Required Settings",
        "api_key": {
            "type": "string",
            "description": "TMDB API key - used for poster retrieval and release date supplementation when TVDB is primary metadata source (not 'API Read Access Token')",
            "default": "",
            "sensitive": True
        },
        "certification_region": {
            "type": "select",
            "description": "Content rating region - select which country's certification system to display (e.g., US: R, PG-13 | GB: 15, 12A | CA: 14A, 18A)",
            "default": "US",
            "choices": ["US", "GB", "CA", "AU", "DE", "FR", "ES", "IT", "JP", "KR", "BR", "MX", "IN", "NL", "SE", "NO", "DK", "FI", "NZ", "IE", "AT", "CH", "BE", "PT", "PL", "RU", "TR", "AR", "CL", "CO"]
        }
    },
    "TVDB": {
        "tab": "Required Settings",
        "api_key": {
            "type": "string",
            "description": "TVDB v4 API key - when set, uses TVDB instead of Trakt for metadata lookups. A TMDB API key is also required for full release date support (digital/physical). It also unlocks TVDB's other episode orders (DVD, absolute, ...), which a show can be switched to from Show options on its library page. Get a key at <a href='https://thetvdb.com/api-information' target='_blank'>thetvdb.com/api-information</a>",
            "default": "",
            "sensitive": True
        }
    },
    "MDBList": {
        "tab": "Additional Settings",
        "api_key": {
            "type": "string",
            "description": "MDBList API key - enables curated lists from IMDB, Trakt, Netflix, Disney+, and more on the Discover page, and is required for MDBList content sources that use an API endpoint (watchlist / username+list name / list ID). Get your API key at <a href='https://mdblist.com/preferences/' target='_blank'>mdblist.com/preferences</a>",
            "default": "",
            "sensitive": True
        },
        "cache_duration": {
            "type": "integer",
            "description": "How long to cache MDBList data (in hours). Lower values mean fresher data but more API calls.",
            "default": 24,
            "min": 1,
            "max": 168
        }
    },
    "Trakt": {
        "tab": "Additional Settings",
        "client_id": {
            "type": "string",
            "description": "Trakt client ID",
            "default": "",
            "sensitive": True
        },
        "client_secret": {
            "type": "string",
            "description": "Trakt client secret (optional; new Trakt apps don't get one)",
            "default": "",
            "sensitive": True
        }
    },
    "Scrob": {
        "tab": "Additional Settings",
        "url": {
            "type": "string",
            "description": "Base URL of your self-hosted Scrob instance, e.g. http://192.168.1.24:7330",
            "default": ""
        },
        "api_key": {
            "type": "string",
            "description": "Scrob API key (Settings → Connections → API Key in the Scrob UI). Shared by all Scrob content sources (Lists, Collection, Special) below. Used for all read operations (syncing Lists, Collection, and Special Lists into cli_debrid).",
            "default": "",
            "sensitive": True
        },
        "username": {
            "type": "string",
            "description": "Optional: your Scrob login username. Only needed if you want cli_debrid to remove items from Scrob Lists/Collection when you delete them from your library — Scrob's API key cannot authorize deletions, only a logged-in session can. Leave blank to skip deletion sync (syncing content in still works fine with just the API key above).",
            "default": ""
        },
        "password": {
            "type": "string",
            "description": "Optional: your Scrob login password, paired with the username above. Only used to obtain a session token for removing items from Scrob when deleted in cli_debrid — never sent anywhere else. Leave blank to skip deletion sync.",
            "default": "",
            "sensitive": True
        }
    },
    "Floppy": {
        "tab": "Additional Settings",
        "url": {
            "type": "string",
            "description": "Base URL of your self-hosted Floppy instance, e.g. http://192.168.1.24:8000",
            "default": ""
        },
        "api_key": {
            "type": "string",
            "description": "Your Floppy API token (Floppy → Settings → Integrations / API). Sent as the X-API-Key header. Shared by every Floppy content source.",
            "default": "",
            "sensitive": True
        }
    },
    "Simkl": {
        "tab": "Additional Settings",
        "client_id": {
            "type": "string",
            "description": "Client ID of your Simkl app. Create one at simkl.com/settings/developer (Auth V2, type 'TV, devices & command line'), then click Sign in to Simkl below.",
            "default": ""
        },
        "client_secret": {
            "type": "string",
            "description": "Only for apps registered as 'Server apps & services'. Leave blank for 'TV, devices & command line' apps.",
            "default": "",
            "sensitive": True
        }
    },
    "Staleness Threshold": {
        "tab": "Additional Settings",
        "staleness_threshold": {
            "type": "integer",
            "description": "Staleness threshold for metadata (in days)",
            "default": 7
        }
    },
}
