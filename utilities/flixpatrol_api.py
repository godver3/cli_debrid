"""
FlixPatrol API
Direct integration with FlixPatrol for streaming service Top 10 charts.
No API key required - scrapes public data from FlixPatrol.

Based on the implementation from Agregarr project.
"""

import logging
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import re

# Supported streaming platforms
# Most platforms have global Top 10 data, Hulu and Peacock use US data
FLIXPATROL_PLATFORMS = {
    'netflix': {
        'name': 'Netflix',
        'slug': 'netflix',
        'icon': 'netflix',
        'region': 'united-states'
    },
    'disney': {
        'name': 'Disney+',
        'slug': 'disney',
        'icon': 'disney',
        'region': 'united-states'
    },
    'amazon': {
        'name': 'Amazon Prime',
        'slug': 'amazon-prime',
        'icon': 'amazon',
        'region': 'united-states'
    },
    'hbo': {
        'name': 'Max',
        'slug': 'hbo-max',
        'icon': 'hbo',
        'region': 'united-states'
    },
    'apple': {
        'name': 'Apple TV',  # FlixPatrol uses "Apple TV" not "Apple TV+" in headings
        'slug': 'apple-tv',
        'icon': 'apple',
        'region': 'united-states'
    },
    'paramount': {
        'name': 'Paramount+',
        'slug': 'paramount-plus',
        'icon': 'paramount',
        'region': 'united-states'
    },
    'hulu': {
        'name': 'Hulu',
        'slug': 'hulu',
        'icon': 'hulu',
        'region': 'united-states'
    },
    'peacock': {
        'name': 'Peacock',
        'slug': 'peacock',
        'icon': 'peacock',
        'region': 'united-states'
    }
}

# Browser-like headers to avoid bot detection
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"macOS"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1'
}


def get_available_platforms() -> Dict[str, Any]:
    """
    Get list of available streaming platforms for Top 10.
    """
    return {
        'success': True,
        'platforms': [
            {
                'id': key,
                'name': info['name'],
                'slug': info['slug'],
                'icon': info['icon']
            }
            for key, info in FLIXPATROL_PLATFORMS.items()
        ]
    }


def _extract_top10_from_section(soup: BeautifulSoup, section_keyword: str) -> List[Dict]:
    """
    Extract Top 10 items from a section identified by h2 heading keyword.

    The FlixPatrol page structure is:
    - h2 heading with "TOP Movies on {Platform}..." or "TOP TV Shows on {Platform}..."
    - Parent div contains a table with 10 rows (one per ranking)
    - Each row has an <a> tag with href="/title/..." containing the title
    """
    items = []

    # Find the heading (h2 or h3) that matches our section
    for h2 in soup.find_all(['h2', 'h3']):
        h2_text = h2.get_text(strip=True)
        if section_keyword in h2_text and 'by country' not in h2_text.lower() and 'by day' not in h2_text.lower():
            # Walk up to find the container with the table
            parent = h2.find_parent()
            table = None

            # Search up the DOM tree for a table
            while parent and not table:
                table = parent.find('table')
                if not table:
                    parent = parent.find_parent()

            if table:
                rows = table.find_all('tr')
                for rank, row in enumerate(rows[:10], start=1):
                    # Find title link in the row
                    title_link = row.find('a', href=re.compile(r'/title/'))
                    if title_link:
                        title = title_link.get_text(strip=True)
                        href = title_link.get('href', '')

                        # Extract FlixPatrol ID from URL
                        fp_id_match = re.search(r'/title/([^/]+)/?', href)
                        fp_id = fp_id_match.group(1) if fp_id_match else None

                        items.append({
                            'rank': rank,
                            'title': title,
                            'flixpatrol_id': fp_id,
                            'flixpatrol_url': f'https://flixpatrol.com{href}' if href.startswith('/') else href
                        })
            break

    return items


def _extract_combined_top10(soup: BeautifulSoup, platform_name: str) -> List[Dict]:
    """
    Extract Top 10 items from a combined section (used by US-only platforms like Hulu/Peacock).

    These pages have a single "{Platform} TOP 10" heading instead of separate Movies/Shows sections.
    """
    items = []

    # Find the h2 that matches "{Platform} TOP 10"
    for h2 in soup.find_all('h2'):
        h2_text = h2.get_text(strip=True)
        if f'{platform_name} TOP 10' in h2_text and 'by country' not in h2_text.lower() and 'by day' not in h2_text.lower():
            # Walk up to find the container with the table
            parent = h2.find_parent()
            table = None

            # Search up the DOM tree for a table
            while parent and not table:
                table = parent.find('table')
                if not table:
                    parent = parent.find_parent()

            if table:
                rows = table.find_all('tr')
                for rank, row in enumerate(rows[:10], start=1):
                    # Find title link in the row
                    title_link = row.find('a', href=re.compile(r'/title/'))
                    if title_link:
                        title = title_link.get_text(strip=True)
                        href = title_link.get('href', '')

                        # Extract FlixPatrol ID from URL
                        fp_id_match = re.search(r'/title/([^/]+)/?', href)
                        fp_id = fp_id_match.group(1) if fp_id_match else None

                        items.append({
                            'rank': rank,
                            'title': title,
                            'flixpatrol_id': fp_id,
                            'flixpatrol_url': f'https://flixpatrol.com{href}' if href.startswith('/') else href
                        })
            break

    return items


def fetch_top10(
    platform: str,
    media_type: str = 'all'
) -> Dict[str, Any]:
    """
    Fetch global Top 10 content from FlixPatrol for a specific platform.

    Args:
        platform: Platform key (netflix, disney, amazon, hbo, apple, paramount, hulu, peacock)
        media_type: Filter by type - 'all', 'movie', 'tv'

    Returns:
        Dict with success status and list of items with title, rank, and media type
    """
    if platform not in FLIXPATROL_PLATFORMS:
        return {'error': f'Unknown platform: {platform}', 'items': []}

    platform_slug = FLIXPATROL_PLATFORMS[platform]['slug']
    platform_name = FLIXPATROL_PLATFORMS[platform]['name']
    platform_region = FLIXPATROL_PLATFORMS[platform].get('region', 'united-states')

    # Always use region-specific URL (united-states for all platforms)
    url = f'https://flixpatrol.com/top10/{platform_slug}/{platform_region}/'
    fallback_url = f'https://flixpatrol.com/top10/{platform_slug}/'

    try:
        logging.info(f"[FlixPatrol] Fetching {platform_name} Top 10 from: {url}")

        # Use a session to carry cookies — FlixPatrol requires a homepage visit first
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            session.get('https://flixpatrol.com/', timeout=10, allow_redirects=True)
        except Exception:
            pass
        session.headers.update({'Referer': 'https://flixpatrol.com/'})
        response = session.get(url, timeout=15, allow_redirects=True)
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')

        items = []

        # Extract date from page title
        title_tag = soup.find('title')
        date_str = datetime.now().strftime('%Y-%m-%d')
        if title_tag:
            title_text = title_tag.get_text()
            date_match = re.search(r'(\w+ \d+, \d{4})', title_text)
            if date_match:
                try:
                    parsed_date = datetime.strptime(date_match.group(1), '%B %d, %Y')
                    date_str = parsed_date.strftime('%Y-%m-%d')
                except ValueError:
                    pass

        # US pages have separate h3 sections: "TOP 10 Movies" and "TOP 10 TV Shows"
        # Extract movies and shows separately to match original 20-item behaviour
        if media_type in ['all', 'movie']:
            movies = _extract_top10_from_section(soup, 'TOP 10 Movies')
            for movie in movies:
                movie['media_type'] = 'movie'
                items.append(movie)

        if media_type in ['all', 'tv']:
            shows = _extract_top10_from_section(soup, 'TOP 10 TV Shows')
            for show in shows:
                show['media_type'] = 'tv'
                items.append(show)

        # Fallback: if no separate sections found, use combined then global
        if not items:
            combined_items = _extract_combined_top10(soup, platform_name)
            if combined_items:
                for item in combined_items:
                    item['media_type'] = 'unknown'
                    items.append(item)
            else:
                logging.info(f"[FlixPatrol] No US results for {platform_name}, falling back to global")
                try:
                    fb_resp = session.get(fallback_url, timeout=15, allow_redirects=True)
                    fb_resp.raise_for_status()
                    fb_soup = BeautifulSoup(fb_resp.text, 'html.parser')
                    if media_type in ['all', 'movie']:
                        for movie in _extract_top10_from_section(fb_soup, 'TOP Movies'):
                            movie['media_type'] = 'movie'
                            items.append(movie)
                    if media_type in ['all', 'tv']:
                        for show in _extract_top10_from_section(fb_soup, 'TOP TV Shows'):
                            show['media_type'] = 'tv'
                            items.append(show)
                except Exception as fe:
                    logging.warning(f"[FlixPatrol] Global fallback failed for {platform_name}: {fe}")

        if media_type != 'all':
            items.sort(key=lambda x: x['rank'])

        logging.info(f"[FlixPatrol] Found {len(items)} items for {platform_name}")

        return {
            'success': True,
            'platform': platform,
            'platform_name': platform_name,
            'date': date_str,
            'items': items
        }

    except requests.exceptions.RequestException as e:
        logging.warning(f"[FlixPatrol] Request error for {url}: {e}")
        return {'error': f'Failed to fetch from FlixPatrol: {str(e)}', 'items': []}
    except Exception as e:
        logging.error(f"[FlixPatrol] Error parsing {platform_name} Top 10: {e}")
        return {'error': f'Error parsing FlixPatrol data: {str(e)}', 'items': []}


def _fetch_single_day(platform_slug: str, platform_region: str, date_str: str) -> List[Dict]:
    """Fetch raw items for one day's FlixPatrol page. Returns [] on failure."""
    url = f'https://flixpatrol.com/top10/{platform_slug}/{platform_region}/{date_str}/'
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = []
        for movie in _extract_top10_from_section(soup, 'TOP 10 Movies'):
            movie['media_type'] = 'movie'
            items.append(movie)
        for show in _extract_top10_from_section(soup, 'TOP 10 TV Shows'):
            show['media_type'] = 'tv'
            items.append(show)
        if not items:
            for item in _extract_combined_top10(soup, ''):
                item['media_type'] = 'unknown'
                items.append(item)
        return items
    except Exception as e:
        logging.debug(f"[FlixPatrol] Weekly day {date_str} fetch failed: {e}")
        return []


def fetch_top10_weekly(platform: str, media_type: str = 'all') -> Dict[str, Any]:
    """
    Aggregate the last 7 days of FlixPatrol daily Top 10 into a weekly ranking.

    Scoring: rank 1 = 10 pts, rank 2 = 9 pts … rank 10 = 1 pt per day.
    Items are deduplicated by flixpatrol_id and sorted by total score descending.
    The top 20 items (10 movies + 10 shows, or 20 combined) are returned.
    """
    if platform not in FLIXPATROL_PLATFORMS:
        return {'error': f'Unknown platform: {platform}', 'items': []}

    platform_slug = FLIXPATROL_PLATFORMS[platform]['slug']
    platform_name = FLIXPATROL_PLATFORMS[platform]['name']
    platform_region = FLIXPATROL_PLATFORMS[platform].get('region', 'united-states')

    # Build date strings for the past 7 days (yesterday back 6 more days)
    today = datetime.now()
    dates = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(1, 8)]

    logging.info(f"[FlixPatrol] Weekly fetch for {platform_name}: {dates[0]} → {dates[-1]}")

    # Fetch all days in parallel
    scores: Dict[str, Dict] = {}  # keyed by flixpatrol_id
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {ex.submit(_fetch_single_day, platform_slug, platform_region, d): d for d in dates}
        for future in as_completed(futures):
            day_items = future.result()
            for item in day_items:
                fp_id = item.get('flixpatrol_id')
                if not fp_id:
                    continue
                mt = item.get('media_type', 'unknown')
                pts = max(0, 11 - item.get('rank', 11))
                if fp_id not in scores:
                    scores[fp_id] = {**item, 'score': 0, 'days': 0}
                scores[fp_id]['score'] += pts
                scores[fp_id]['days'] += 1

    # Split into movies/shows, sort by weekly score descending — no cap, all unique titles
    movies = sorted([v for v in scores.values() if v['media_type'] == 'movie'],
                    key=lambda x: x['score'], reverse=True)
    shows = sorted([v for v in scores.values() if v['media_type'] == 'tv'],
                   key=lambda x: x['score'], reverse=True)
    combined = sorted([v for v in scores.values() if v['media_type'] == 'unknown'],
                      key=lambda x: x['score'], reverse=True)

    # Reassign ranks within each group
    all_items = []
    if movies or shows:
        if media_type in ('all', 'movie'):
            for rank, item in enumerate(movies, 1):
                all_items.append({**item, 'rank': rank})
        if media_type in ('all', 'tv'):
            for rank, item in enumerate(shows, 1):
                all_items.append({**item, 'rank': rank})
    else:
        for rank, item in enumerate(combined, 1):
            all_items.append({**item, 'rank': rank})

    logging.info(f"[FlixPatrol] Weekly {platform_name}: {len(all_items)} items from {len(scores)} unique titles")

    return {
        'success': True,
        'platform': platform,
        'platform_name': platform_name,
        'period': 'weekly',
        'date': f"{dates[-1]} to {dates[0]}",
        'items': all_items,
    }


def get_title_ids_from_flixpatrol(flixpatrol_id: str) -> Optional[Dict[str, Any]]:
    """
    Get IMDB and TMDB IDs from a FlixPatrol title page.

    Args:
        flixpatrol_id: The FlixPatrol title ID (from URL)

    Returns:
        Dict with imdb_id, tmdb_id, media_type if found
    """
    try:
        url = f'https://flixpatrol.com/title/{flixpatrol_id}/'

        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        result = {
            'flixpatrol_id': flixpatrol_id
        }

        # Look for IMDB link
        imdb_link = soup.find('a', href=re.compile(r'imdb\.com/title/'))
        if imdb_link:
            imdb_url = imdb_link.get('href', '')
            imdb_match = re.search(r'/(tt\d+)', imdb_url)
            if imdb_match:
                result['imdb_id'] = imdb_match.group(1)

        # Look for TMDB link
        tmdb_link = soup.find('a', href=re.compile(r'themoviedb\.org'))
        if tmdb_link:
            tmdb_url = tmdb_link.get('href', '')
            tmdb_match = re.search(r'/(movie|tv)/(\d+)', tmdb_url)
            if tmdb_match:
                result['tmdb_id'] = int(tmdb_match.group(2))
                result['media_type'] = tmdb_match.group(1)

        # Get title from page
        title_elem = soup.find('h1')
        if title_elem:
            result['title'] = title_elem.get_text(strip=True)

        return result if 'tmdb_id' in result or 'imdb_id' in result else None

    except Exception as e:
        logging.error(f"[FlixPatrol] Error fetching title {flixpatrol_id}: {e}")
        return None
