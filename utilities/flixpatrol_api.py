"""
FlixPatrol API
Direct integration with FlixPatrol for streaming service Top 10 charts.
No API key required - scrapes public data from FlixPatrol.

Based on the implementation from Agregarr project.
"""

import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Dict, List, Optional, Any
import re

# Supported streaming platforms
# Most platforms have global Top 10 data, Hulu and Peacock use US data
FLIXPATROL_PLATFORMS = {
    'netflix': {
        'name': 'Netflix',
        'slug': 'netflix',
        'icon': 'netflix',
        'region': 'world'
    },
    'disney': {
        'name': 'Disney+',
        'slug': 'disney',
        'icon': 'disney',
        'region': 'world'
    },
    'amazon': {
        'name': 'Amazon Prime',
        'slug': 'amazon-prime',
        'icon': 'amazon',
        'region': 'world'
    },
    'hbo': {
        'name': 'Max',
        'slug': 'hbo-max',
        'icon': 'hbo',
        'region': 'world'
    },
    'apple': {
        'name': 'Apple TV+',
        'slug': 'apple-tv',
        'icon': 'apple',
        'region': 'world'
    },
    'paramount': {
        'name': 'Paramount+',
        'slug': 'paramount-plus',
        'icon': 'paramount',
        'region': 'world'
    },
    'hulu': {
        'name': 'Hulu',
        'slug': 'hulu',
        'icon': 'hulu',
        'region': 'united-states'  # US-only service
    },
    'peacock': {
        'name': 'Peacock',
        'slug': 'peacock',
        'icon': 'peacock',
        'region': 'united-states'  # US-only service
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

    # Find the h2 that matches our section
    for h2 in soup.find_all('h2'):
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
    platform_region = FLIXPATROL_PLATFORMS[platform].get('region', 'world')

    # FlixPatrol URL - use region-specific URL for US-only services
    if platform_region == 'united-states':
        url = f'https://flixpatrol.com/top10/{platform_slug}/{platform_region}/'
    else:
        url = f'https://flixpatrol.com/top10/{platform_slug}/'

    try:
        logging.info(f"[FlixPatrol] Fetching {platform_name} Top 10 from: {url}")

        response = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
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

        # For US-only platforms (Hulu, Peacock), use combined extraction
        if platform_region == 'united-states':
            combined_items = _extract_combined_top10(soup, platform_name)
            for item in combined_items:
                # US pages don't separate movies/TV, mark as 'unknown' initially
                # The TMDB enrichment will determine the actual type
                item['media_type'] = 'unknown'
                items.append(item)
        else:
            # Extract Movies if requested
            if media_type in ['all', 'movie']:
                movies = _extract_top10_from_section(soup, 'TOP Movies')
                for movie in movies:
                    movie['media_type'] = 'movie'
                    items.append(movie)

            # Extract TV Shows if requested
            if media_type in ['all', 'tv']:
                shows = _extract_top10_from_section(soup, 'TOP TV Shows')
                for show in shows:
                    show['media_type'] = 'tv'
                    items.append(show)

            # Sort by rank (movies first, then shows if both)
            if media_type == 'all':
                # Keep movies and shows in their respective rank order
                pass
            else:
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
