"""
Streaming Top 10 charts per platform (Netflix, Disney+, Max, ...).

Backed by JustWatch's public GraphQL streaming-charts endpoint: plain JSON,
no API key, no Cloudflare challenge, and every entry already carries its
IMDb/TMDB IDs, so callers can skip title -> TMDB search lookups.

Ranks come from JustWatch's chart for the whole country; filtering by a
provider package returns that provider's titles in chart order, so ranks are
renumbered 1..N per platform and media type.

Replaces the old FlixPatrol scraper, which needed a headed Chromium + Xvfb to
get past Cloudflare (~1.1 GB of the Docker image).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

import requests

JUSTWATCH_GRAPHQL_URL = 'https://apis.justwatch.com/graphql'
COUNTRY = 'US'
TOP_N = 10

# Platform keys are persisted in adaptive-list configs ("flixpatrol:netflix")
# and the Discover UI's localStorage — keep them stable.
STREAMING_PLATFORMS = {
    'netflix': {'name': 'Netflix', 'icon': 'netflix', 'package': 'nfx'},
    'disney': {'name': 'Disney+', 'icon': 'disney', 'package': 'dnp'},
    'amazon': {'name': 'Amazon Prime', 'icon': 'amazon', 'package': 'amp'},
    'hbo': {'name': 'Max', 'icon': 'hbo', 'package': 'mxx'},
    'apple': {'name': 'Apple TV', 'icon': 'apple', 'package': 'atp'},
    'paramount': {'name': 'Paramount+', 'icon': 'paramount', 'package': 'pmp'},
    'hulu': {'name': 'Hulu', 'icon': 'hulu', 'package': 'hlu'},
    'peacock': {'name': 'Peacock', 'icon': 'peacock', 'package': 'pct'},
}

_CATEGORY_DAILY = 'DAILY_POPULARITY_SAME_CONTENT_TYPE'
_CATEGORY_WEEKLY = 'WEEKLY_POPULARITY_SAME_CONTENT_TYPE'

_CHART_QUERY = """
query StreamingTop10($country: Country!, $first: Int!, $filter: StreamingChartsFilter) {
  streamingCharts(country: $country, first: $first, filter: $filter) {
    edges {
      streamingChartInfo { rank daysInTop10 }
      node {
        objectType
        content(country: $country, language: "en") {
          title
          originalReleaseYear
          externalIds { imdbId tmdbId }
        }
      }
    }
  }
}
"""

_HEADERS = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
}


def get_available_platforms() -> Dict[str, Any]:
    """List the streaming platforms that have a Top 10 chart."""
    return {
        'success': True,
        'platforms': [
            {'id': key, 'name': info['name'], 'slug': key, 'icon': info['icon']}
            for key, info in STREAMING_PLATFORMS.items()
        ],
    }


def _fetch_chart(package: str, object_type: str, category: str) -> List[Dict]:
    """One chart (one platform, one media type). Raises on HTTP/GraphQL errors."""
    payload = {
        'query': _CHART_QUERY,
        'variables': {
            'country': COUNTRY,
            'first': TOP_N,
            'filter': {'objectType': object_type, 'packages': [package], 'category': category},
        },
    }
    resp = requests.post(JUSTWATCH_GRAPHQL_URL, json=payload, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if body.get('errors'):
        raise RuntimeError(body['errors'][0].get('message', 'GraphQL error'))

    edges = ((body.get('data') or {}).get('streamingCharts') or {}).get('edges') or []
    media_type = 'tv' if object_type == 'SHOW' else 'movie'
    items = []
    for edge in edges:
        content = ((edge.get('node') or {}).get('content')) or {}
        title = content.get('title')
        if not title:
            continue
        ids = content.get('externalIds') or {}
        tmdb_id = ids.get('tmdbId')
        try:
            tmdb_id = int(tmdb_id) if tmdb_id else None
        except (TypeError, ValueError):
            tmdb_id = None
        items.append({
            'rank': len(items) + 1,
            'title': title,
            'year': content.get('originalReleaseYear'),
            'media_type': media_type,
            'tmdb_id': tmdb_id,
            'imdb_id': ids.get('imdbId') or None,
            'days_in_top10': (edge.get('streamingChartInfo') or {}).get('daysInTop10'),
        })
    return items


def _fetch(platform: str, media_type: str, category: str, label: str) -> Dict[str, Any]:
    info = STREAMING_PLATFORMS.get(platform)
    if not info:
        return {'error': f'Unknown platform: {platform}', 'items': []}

    wanted = []
    if media_type in ('all', 'movie'):
        wanted.append('MOVIE')
    if media_type in ('all', 'tv'):
        wanted.append('SHOW')

    items: List[Dict] = []
    errors = []
    for object_type in wanted:
        try:
            items.extend(_fetch_chart(info['package'], object_type, category))
        except Exception as e:
            errors.append(f'{object_type}: {e}')
            logging.warning(f"[Top10] {info['name']} {label} {object_type} chart failed: {e}")

    if not items and errors:
        return {'error': f"Failed to fetch {info['name']} Top 10: {'; '.join(errors)}", 'items': []}

    logging.info(f"[Top10] {info['name']} {label}: {len(items)} items")
    return {
        'success': True,
        'platform': platform,
        'platform_name': info['name'],
        'date': datetime.now().strftime('%Y-%m-%d'),
        'items': items,
    }


def fetch_top10(platform: str, media_type: str = 'all') -> Dict[str, Any]:
    """
    Today's Top 10 for a platform: up to 10 movies then up to 10 shows
    (media_type 'all'), or just one of them ('movie' / 'tv').
    """
    return _fetch(platform, media_type, _CATEGORY_DAILY, 'daily')


def fetch_top10_weekly(platform: str, media_type: str = 'all') -> Dict[str, Any]:
    """This week's Top 10 for a platform, same shape as fetch_top10()."""
    result = _fetch(platform, media_type, _CATEGORY_WEEKLY, 'weekly')
    if result.get('success'):
        result['period'] = 'weekly'
    return result
