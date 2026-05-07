"""
MDBList API Integration
Provides access to curated lists from IMDB, Trakt, Netflix, Disney+, FlixPatrol and more.
Uses caching to minimize API calls (1000 free requests/day limit).
"""

import logging
import requests
import json
import os
from datetime import datetime, timedelta
from utilities.settings import get_setting

# In-memory cache for MDBList data
_mdblist_cache = {}

# MDBList API base URL
MDBLIST_API_BASE = "https://mdblist.com/api"
# MDBList public lists URL (for curated lists - no API key needed)
MDBLIST_LISTS_BASE = "https://mdblist.com/lists"

# Predefined popular lists from various sources
# These use real MDBList public lists with format: username/list-slug
# To get JSON data, append /json/ to the list URL
# Example: https://mdblist.com/lists/garycrawfordgc/netflix-movies/json/
CURATED_LISTS = {
    # ===================
    # MDBList Popular (verified working)
    # ===================
    'mdblist_popular_movies': {
        'name': 'Most Popular Movies',
        'list_id': 'linaspurinis/mdblist-com-most-popular-movies',
        'icon': 'mdblist',
        'description': 'Most popular movies on MDBList',
        'category': 'mdblist'
    },
    'mdblist_popular_shows_top50': {
        'name': 'Most Popular Shows [top-50]',
        'list_id': 'linaspurinis/most-popular-shows-top-50',
        'icon': 'mdblist',
        'description': 'Top 50 most popular shows on MDBList',
        'category': 'mdblist'
    },
    'mdblist_trending_movies': {
        'name': 'Trending Movies',
        'list_id': 'linaspurinis/trending-movies-list',
        'icon': 'mdblist',
        'description': 'Trending movies on MDBList',
        'category': 'mdblist'
    },
    'mdblist_top_watched_week': {
        'name': 'Top Watched Movies of the Week',
        'list_id': 'linaspurinis/top-watched-movies-of-the-week',
        'icon': 'mdblist',
        'description': 'Most watched movies this week',
        'category': 'mdblist'
    },
    'mdblist_latest_releases': {
        'name': 'Latest Releases',
        'list_id': 'linaspurinis/latest-releases',
        'icon': 'mdblist',
        'description': 'Latest movie releases',
        'category': 'mdblist'
    },
    'mdblist_top_watched_kids': {
        'name': 'Top Watched Movies for Kids',
        'list_id': 'linaspurinis/top-watched-movies-of-the-week-for-kids',
        'icon': 'mdblist',
        'description': 'Top watched family-friendly movies',
        'category': 'mdblist'
    },
    'mdblist_top_pirated': {
        'name': 'Top 10 Pirated Movies of The Week',
        'list_id': 'linaspurinis/top-10-pirated-movies-of-the-week-50',
        'icon': 'mdblist',
        'description': 'Most pirated movies this week',
        'category': 'mdblist'
    },

    # ===================
    # Gary's Top Lists (verified working)
    # ===================
    'gary_top_movies': {
        'name': 'Top Movies',
        'list_id': 'garycrawfordgc/top-movies',
        'icon': 'mdblist',
        'description': 'Top rated movies',
        'category': 'mdblist'
    },
    'gary_top_movies_week': {
        'name': 'Top Movies of the Week',
        'list_id': 'garycrawfordgc/top-movies-of-the-week',
        'icon': 'mdblist',
        'description': 'Top movies this week',
        'category': 'mdblist'
    },
    'gary_latest_shows': {
        'name': 'Latest TV Shows',
        'list_id': 'garycrawfordgc/latest-tv-shows',
        'icon': 'mdblist',
        'description': 'Latest TV show releases',
        'category': 'mdblist'
    },
    'gary_bluray_releases': {
        'name': 'Latest Blu-ray Releases',
        'list_id': 'garycrawfordgc/latest-blu-ray-releases',
        'icon': 'mdblist',
        'description': 'Latest Blu-ray releases',
        'category': 'mdblist'
    },

    # ===================
    # Netflix (verified working)
    # ===================
    'netflix_top_movies': {
        'name': 'Netflix Top Movies',
        'list_id': 'garycrawfordgc/netflix-movies',
        'icon': 'netflix',
        'description': 'Top movies on Netflix',
        'category': 'streaming'
    },
    'netflix_top_shows': {
        'name': 'Netflix Top Shows',
        'list_id': 'garycrawfordgc/netflix-shows',
        'icon': 'netflix',
        'description': 'Top TV shows on Netflix',
        'category': 'streaming'
    },

    # ===================
    # Disney+ (verified working)
    # ===================
    'disney_top_movies': {
        'name': 'Disney+ Top Movies',
        'list_id': 'garycrawfordgc/disney-movies',
        'icon': 'disney',
        'description': 'Top movies on Disney+',
        'category': 'streaming'
    },
    'disney_top_shows': {
        'name': 'Disney+ Top Shows',
        'list_id': 'garycrawfordgc/disney-shows',
        'icon': 'disney',
        'description': 'Top TV shows on Disney+',
        'category': 'streaming'
    },

    # ===================
    # Amazon Prime (verified working)
    # ===================
    'amazon_top_movies': {
        'name': 'Amazon Prime Top Movies',
        'list_id': 'garycrawfordgc/amazon-prime-movies',
        'icon': 'amazon',
        'description': 'Top movies on Amazon Prime',
        'category': 'streaming'
    },
    'amazon_top_shows': {
        'name': 'Amazon Prime Top Shows',
        'list_id': 'garycrawfordgc/amazon-prime-shows',
        'icon': 'amazon',
        'description': 'Top TV shows on Amazon Prime',
        'category': 'streaming'
    },

    # ===================
    # HBO / Max (verified working)
    # ===================
    'hbo_top_shows': {
        'name': 'HBO/Max Top Shows',
        'list_id': 'garycrawfordgc/hbo-shows',
        'icon': 'hbo',
        'description': 'Top TV shows on HBO/Max',
        'category': 'streaming'
    },

    # ===================
    # Hulu (verified working)
    # ===================
    'hulu_top_movies': {
        'name': 'Hulu Top Movies',
        'list_id': 'garycrawfordgc/hulu-movies',
        'icon': 'hulu',
        'description': 'Top movies on Hulu',
        'category': 'streaming'
    },
    'hulu_top_shows': {
        'name': 'Hulu Top Shows',
        'list_id': 'garycrawfordgc/hulu-shows',
        'icon': 'hulu',
        'description': 'Top TV shows on Hulu',
        'category': 'streaming'
    },

    # ===================
    # BBC (verified working)
    # ===================
    'bbc_top_shows': {
        'name': 'BBC Top Shows',
        'list_id': 'garycrawfordgc/bbc-shows',
        'icon': 'bbc',
        'description': 'Top TV shows on BBC',
        'category': 'streaming'
    },

    # ===================
    # Streaming Originals (Kometa lists - used by Agregarr)
    # ===================
    'netflix_originals': {
        'name': 'Netflix Originals',
        'list_id': 'k0meta/netflix-originals',
        'icon': 'netflix',
        'description': 'Netflix Original content',
        'category': 'originals'
    },
    'disney_originals': {
        'name': 'Disney+ Originals',
        'list_id': 'k0meta/disney-originals',
        'icon': 'disney',
        'description': 'Disney+ Original content',
        'category': 'originals'
    },
    'amazon_originals': {
        'name': 'Amazon Originals',
        'list_id': 'k0meta/amazon-originals',
        'icon': 'amazon',
        'description': 'Amazon Original content',
        'category': 'originals'
    },
    'hbomax_originals': {
        'name': 'HBO Max Originals',
        'list_id': 'k0meta/hbomax-originals',
        'icon': 'hbo',
        'description': 'HBO Max Original content',
        'category': 'originals'
    },
    'max_originals': {
        'name': 'Max Originals',
        'list_id': 'k0meta/max-originals',
        'icon': 'hbo',
        'description': 'Max Original content',
        'category': 'originals'
    },
    'paramount_originals': {
        'name': 'Paramount+ Originals',
        'list_id': 'k0meta/paramount-originals',
        'icon': 'paramount',
        'description': 'Paramount+ Original content',
        'category': 'originals'
    },
    'hulu_originals': {
        'name': 'Hulu Originals',
        'list_id': 'k0meta/hulu-originals',
        'icon': 'hulu',
        'description': 'Hulu Original content',
        'category': 'originals'
    },
    'peacock_originals': {
        'name': 'Peacock Originals',
        'list_id': 'k0meta/peacock-originals',
        'icon': 'peacock',
        'description': 'Peacock Original content',
        'category': 'originals'
    },
    'appletv_originals': {
        'name': 'Apple TV+ Originals',
        'list_id': 'k0meta/appletv-originals',
        'icon': 'apple',
        'description': 'Apple TV+ Original content',
        'category': 'originals'
    },
    'discovery_movies': {
        'name': 'Discovery+ Movies',
        'list_id': 'k0meta/discovery-movies',
        'icon': 'discovery',
        'description': 'Discovery+ Movies',
        'category': 'originals'
    },

    # ===================
    # Kometa Curated Lists
    # ===================
    'certified_fresh_movies': {
        'name': 'Certified Fresh Movies',
        'list_id': 'k0meta/certifiedfreshmovies',
        'icon': 'rottentomatoes',
        'description': 'Rotten Tomatoes Certified Fresh movies',
        'category': 'curated'
    },
    'certified_fresh_shows': {
        'name': 'Certified Fresh Shows',
        'list_id': 'k0meta/certifiedfreshshows',
        'icon': 'rottentomatoes',
        'description': 'Rotten Tomatoes Certified Fresh shows',
        'category': 'curated'
    },
    'metacritic_movies': {
        'name': 'Metacritic Must-See Movies',
        'list_id': 'k0meta/metacriticmustseemovies',
        'icon': 'metacritic',
        'description': 'Metacritic Must-See movies',
        'category': 'curated'
    },
    'metacritic_shows': {
        'name': 'Metacritic Must-See Shows',
        'list_id': 'k0meta/metacriticmustseeshows',
        'icon': 'metacritic',
        'description': 'Metacritic Must-See shows',
        'category': 'curated'
    },
    'common_sense_movies': {
        'name': 'Common Sense Selection - Movies',
        'list_id': 'k0meta/cssfamiliesmovies',
        'icon': 'commonsense',
        'description': 'Family-friendly movies by Common Sense Media',
        'category': 'curated'
    },
    'common_sense_shows': {
        'name': 'Common Sense Selection - Shows',
        'list_id': 'k0meta/cssfamiliesshows',
        'icon': 'commonsense',
        'description': 'Family-friendly shows by Common Sense Media',
        'category': 'curated'
    },
    'based_on_comics_movies': {
        'name': 'Movies Based on Comics',
        'list_id': 'k0meta/based_on_comics_movies',
        'icon': 'mdblist',
        'description': 'Movies based on comic books',
        'category': 'curated'
    },
    'based_on_comics_shows': {
        'name': 'Shows Based on Comics',
        'list_id': 'k0meta/based_on_comics_shows',
        'icon': 'mdblist',
        'description': 'TV shows based on comic books',
        'category': 'curated'
    },
    'based_on_games_movies': {
        'name': 'Movies Based on Video Games',
        'list_id': 'k0meta/based_on_video_games_movies',
        'icon': 'mdblist',
        'description': 'Movies based on video games',
        'category': 'curated'
    },
    'based_on_games_shows': {
        'name': 'Shows Based on Video Games',
        'list_id': 'k0meta/based_on_video_games_shows',
        'icon': 'mdblist',
        'description': 'TV shows based on video games',
        'category': 'curated'
    },
    'based_on_books_movies': {
        'name': 'Movies Based on Books',
        'list_id': 'k0meta/based_on_books_movies',
        'icon': 'mdblist',
        'description': 'Movies based on books',
        'category': 'curated'
    },
    'based_on_books_shows': {
        'name': 'Shows Based on Books',
        'list_id': 'k0meta/based_on_books_shows',
        'icon': 'mdblist',
        'description': 'TV shows based on books',
        'category': 'curated'
    },
    'based_on_true_story_movies': {
        'name': 'Movies Based on True Stories',
        'list_id': 'k0meta/based_on_true_story_movies',
        'icon': 'mdblist',
        'description': 'Movies based on true stories',
        'category': 'curated'
    },
    'based_on_true_story_shows': {
        'name': 'Shows Based on True Stories',
        'list_id': 'k0meta/based_on_true_story_shows',
        'icon': 'mdblist',
        'description': 'TV shows based on true stories',
        'category': 'curated'
    }
}


def get_mdblist_api_key():
    """Get MDBList API key from settings"""
    return get_setting('MDBList', 'api_key', '')


def get_cache_duration():
    """Get cache duration in hours from settings"""
    return get_setting('MDBList', 'cache_duration', 24)


def is_mdblist_configured():
    """Check if MDBList API key is configured"""
    return bool(get_mdblist_api_key())


def _get_cache_key(endpoint, **kwargs):
    """Generate a cache key for the given endpoint and parameters"""
    key_parts = [f'mdblist:{endpoint}']
    key_parts.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
    return ':'.join(key_parts)


def _get_from_cache(key):
    """Get data from cache if not expired"""
    if key in _mdblist_cache:
        cached_data, expiry = _mdblist_cache[key]
        if datetime.now() < expiry:
            logging.debug(f"MDBList cache HIT: {key}")
            return cached_data
        else:
            del _mdblist_cache[key]
    return None


def _set_in_cache(key, data, ttl_hours=None):
    """Set data in cache with configurable TTL"""
    if ttl_hours is None:
        ttl_hours = get_cache_duration()
    expiry = datetime.now() + timedelta(hours=ttl_hours)
    _mdblist_cache[key] = (data, expiry)
    logging.debug(f"MDBList cache SET: {key} (TTL: {ttl_hours}h)")

    # Clean old entries if cache gets too large
    if len(_mdblist_cache) > 50:
        now = datetime.now()
        expired_keys = [k for k, (_, exp) in _mdblist_cache.items() if exp < now]
        for k in expired_keys:
            del _mdblist_cache[k]


def clear_mdblist_cache():
    """Clear all MDBList cache entries"""
    global _mdblist_cache
    _mdblist_cache.clear()
    logging.info("MDBList cache cleared")


def fetch_mdblist_top_lists():
    """
    Fetch the available top lists from MDBList
    Returns a list of available predefined lists grouped by category
    Note: Public lists don't require an API key
    """
    # Return the curated lists we support with category grouping
    lists = []
    for list_key, list_info in CURATED_LISTS.items():
        lists.append({
            'key': list_key,
            'name': list_info['name'],
            'icon': list_info['icon'],
            'description': list_info['description'],
            'category': list_info.get('category', 'other')
        })

    return {'success': True, 'lists': lists}


def fetch_list_items(list_key, limit=20):
    """
    Fetch items from a specific MDBList curated list

    Args:
        list_key: The key of the curated list (e.g., 'netflix_movies')
        limit: Maximum number of items to return

    Returns:
        Dict with 'success', 'items' (list of TMDB IDs with metadata)
    """
    # Note: Public lists don't require API key - they use the /json/ endpoint
    if list_key not in CURATED_LISTS:
        return {'error': f'Unknown list: {list_key}', 'items': []}

    # Check cache first
    cache_key = _get_cache_key('list', list_key=list_key, limit=limit)
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    list_info = CURATED_LISTS[list_key]
    list_slug = list_info['list_id']  # Format: username/list-name

    try:
        # MDBList public lists endpoint - append /json/ to get JSON response
        # URL format: https://mdblist.com/lists/{username}/{list-name}/json/
        url = f"{MDBLIST_LISTS_BASE}/{list_slug}/json/"

        response = requests.get(url, timeout=15)
        response.raise_for_status()

        data = response.json()

        # Process the response - public JSON endpoint returns a list directly
        items = []
        for item in data if isinstance(data, list) else data.get('items', data.get('results', [])):
            processed_item = {
                'tmdb_id': item.get('id'),  # Public JSON uses 'id' for TMDB ID
                'imdb_id': item.get('imdb_id'),
                'title': item.get('title') or item.get('name'),
                'year': item.get('release_year') or item.get('year'),
                'media_type': 'movie' if item.get('mediatype', 'movie') == 'movie' else 'tv',
                'rank': item.get('rank', len(items) + 1),
            }
            if processed_item['tmdb_id']:
                items.append(processed_item)

        result = {
            'success': True,
            'list_name': list_info['name'],
            'items': items[:limit]
        }

        # Cache the result
        _set_in_cache(cache_key, result)

        logging.info(f"MDBList: Fetched {len(items)} items from {list_info['name']}")
        return result

    except requests.exceptions.RequestException as e:
        logging.error(f"MDBList API error for list {list_key}: {e}")
        return {'error': f'Failed to fetch list: {str(e)}', 'items': []}
    except Exception as e:
        logging.error(f"MDBList processing error for list {list_key}: {e}")
        return {'error': f'Error processing list: {str(e)}', 'items': []}


def search_mdblist(query, limit=20):
    """
    Search MDBList for content

    Args:
        query: Search query string
        limit: Maximum results to return

    Returns:
        Dict with search results
    """
    api_key = get_mdblist_api_key()
    if not api_key:
        return {'error': 'MDBList API key not configured', 'items': []}

    try:
        url = f"{MDBLIST_API_BASE}/?apikey={api_key}&s={query}&limit={limit}"

        response = requests.get(url, timeout=15)
        response.raise_for_status()

        data = response.json()

        items = []
        for item in data if isinstance(data, list) else data.get('results', []):
            processed_item = {
                'tmdb_id': item.get('tmdb_id') or item.get('id'),
                'imdb_id': item.get('imdb_id'),
                'title': item.get('title') or item.get('name'),
                'year': item.get('year'),
                'media_type': 'movie' if item.get('mediatype', 'movie') == 'movie' else 'tv',
            }
            if processed_item['tmdb_id']:
                items.append(processed_item)

        return {
            'success': True,
            'items': items[:limit]
        }

    except requests.exceptions.RequestException as e:
        logging.error(f"MDBList search error: {e}")
        return {'error': f'Search failed: {str(e)}', 'items': []}


def get_mdblist_username():
    """Fetch the MDBList username for the current API key via api.mdblist.com."""
    api_key = get_mdblist_api_key()
    if not api_key:
        return None
    try:
        response = requests.get(f"https://api.mdblist.com/user/?apikey={api_key}", timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('username') or data.get('name')
    except Exception as e:
        logging.error(f"MDBList get username error: {e}")
        return None


def get_user_lists(username=None):
    """
    Fetch user's custom MDBList lists

    Args:
        username: MDBList username (optional, uses API key owner if not provided)

    Returns:
        Dict with user's lists
    """
    api_key = get_mdblist_api_key()
    if not api_key:
        return {'error': 'MDBList API key not configured', 'lists': []}

    try:
        url = f"{MDBLIST_API_BASE}/lists/user/?apikey={api_key}"
        if username:
            url += f"&u={username}"

        response = requests.get(url, timeout=15)
        response.raise_for_status()

        data = response.json()

        # Resolve username once to build fetch URLs
        mdb_username = username or get_mdblist_username()

        # Deduplicate by slug — API returns one entry per mediatype for mixed lists.
        # Merge them: keep the name/slug, sum item counts.
        seen = {}
        for lst in data if isinstance(data, list) else data.get('lists', []):
            slug = lst.get('slug', '')
            if slug not in seen:
                seen[slug] = {
                    'id': lst.get('id'),
                    'name': lst.get('name'),
                    'slug': slug,
                    'items_count': lst.get('items', 0),
                    'description': lst.get('description', ''),
                    'username': mdb_username,
                }
            else:
                seen[slug]['items_count'] += lst.get('items', 0)

        return {
            'success': True,
            'lists': list(seen.values())
        }

    except requests.exceptions.RequestException as e:
        logging.error(f"MDBList user lists error: {e}")
        return {'error': f'Failed to fetch user lists: {str(e)}', 'lists': []}


def fetch_custom_list_items(list_id, limit=100, username=None, slug=None):
    """
    Fetch items from a personal MDBList list using mdblist.com/lists/{username}/{slug}/json.
    Requires username and slug — both are stored in the index cache by get_user_lists().
    """
    api_key = get_mdblist_api_key()
    if not api_key:
        return {'error': 'MDBList API key not configured', 'items': []}

    cache_key = _get_cache_key('custom_list', list_id=list_id, limit=limit)
    cached = _get_from_cache(cache_key)
    if cached and cached.get('items'):
        return cached

    if not username:
        username = get_mdblist_username()
    if not username:
        return {'error': 'Could not determine MDBList username', 'items': []}
    if not slug:
        slug = str(list_id)

    try:
        url = f"https://mdblist.com/lists/{username}/{slug}/json?apikey={api_key}"
        logging.info(f"MDBList personal list fetch: {url}")
        response = requests.get(url, timeout=15)
        logging.info(f"MDBList personal list {list_id}: HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            raw_items = data.get('items', data.get('results', []))
        else:
            raw_items = data if isinstance(data, list) else []
        logging.info(f"MDBList personal list {list_id}: raw_count={len(raw_items)}")

        items = []
        for item in raw_items:
            media_type = item.get('mediatype', item.get('type', 'movie')).lower()
            ids = item.get('ids', {})
            tmdb_id = item.get('tmdb_id') or ids.get('tmdb') or item.get('id')
            imdb_id = item.get('imdb_id') or ids.get('imdb')
            processed = {
                'tmdb_id': tmdb_id,
                'imdb_id': imdb_id,
                'title': item.get('title') or item.get('name'),
                'year': item.get('release_year') or item.get('year'),
                'media_type': 'movie' if media_type == 'movie' else 'tv',
                'rank': item.get('rank', len(items) + 1),
            }
            if processed['tmdb_id']:
                items.append(processed)

        result = {'success': True, 'items': items[:limit]}
        if items:
            _set_in_cache(cache_key, result)
        return result

    except requests.exceptions.RequestException as e:
        logging.error(f"MDBList custom list error for {list_id}: {e}")
        return {'error': f'Failed to fetch list: {str(e)}', 'items': []}


def test_api_connection():
    """
    Test the MDBList API connection

    Returns:
        Dict with connection status
    """
    api_key = get_mdblist_api_key()
    if not api_key:
        return {'success': False, 'error': 'MDBList API key not configured'}

    try:
        # Use a simple API call to test connection
        url = f"{MDBLIST_API_BASE}/?apikey={api_key}&l=1&limit=1"
        response = requests.get(url, timeout=10)

        if response.status_code == 401:
            return {'success': False, 'error': 'Invalid API key'}
        elif response.status_code == 429:
            return {'success': False, 'error': 'Rate limit exceeded'}

        response.raise_for_status()
        return {'success': True, 'message': 'API connection successful'}

    except requests.exceptions.RequestException as e:
        return {'success': False, 'error': str(e)}
