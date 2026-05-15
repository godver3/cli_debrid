import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from routes.api_tracker import api
from utilities.settings import get_setting
from PTT import parse_title as ptt_parse

# Newznab category IDs
_CAT_MOVIE = '2000'
_CAT_TV    = '5000,5010,5020'  # TV All, TV Foreign, TV SD

_SANITIZE_RE = re.compile(r'[!?:&,;"()\[\]{}]')

def scrape_newznab_instance(
    instance: str,
    settings: Dict[str, Any],
    imdb_id: Optional[str],
    title: str,
    year: int,
    content_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    multi: bool = False,
    tmdb_id: Optional[str] = None,
    **kwargs
) -> List[Dict[str, Any]]:
    logging.info(f"Scraping Newznab instance: {instance} for '{title}' ({year})")

    url = settings.get('url', '').rstrip('/')
    api_key = settings.get('api_key', '').strip()

    if not url:
        logging.error(f"Newznab instance '{instance}' is missing URL.")
        return []

    if content_type.lower() == 'movie':
        cats = _CAT_MOVIE
        query_parts = [title]
        if year:
            query_parts.append(str(year))
    elif content_type.lower() == 'episode':
        cats = _CAT_TV
        query_parts = [title]
        if season is not None:
            if episode is not None and not multi:
                query_parts.append(f'S{season:02d}E{episode:02d}')
            else:
                query_parts.append(f'S{season:02d}')
    else:
        cats = f'{_CAT_MOVIE},{_CAT_TV}'
        query_parts = [title]

    # Sanitize query — remove characters that break Newznab APIs
    raw_query = ' '.join(query_parts)
    query = _SANITIZE_RE.sub('', raw_query).replace('’', '').strip()

    params: Dict[str, Any] = {
        'apikey': api_key,
        't': 'search',
        'q': query,
        'cat': cats,
        'limit': 100,
    }

    if imdb_id:
        params['imdbid'] = imdb_id.replace('tt', '')

    logging.info(f"Newznab '{instance}' query: q={query!r} cat={cats}")

    endpoint = f'{url}/api'
    timeout = get_setting('Scraping', 'scraper_timeout', 30)

    try:
        response = api.get(endpoint, params=params, timeout=timeout)
    except Exception as exc:
        logging.error(f"Newznab request error for '{instance}': {exc}")
        return []

    if response.status_code != 200:
        logging.error(f"Newznab '{instance}' returned HTTP {response.status_code}: {response.text[:300]}")
        return []

    logging.info(f"Newznab '{instance}' response length: {len(response.text)} chars, preview: {response.text[:300]}")
    return _parse_newznab_xml(response.text, instance)


def _parse_newznab_xml(xml_text: str, instance: str) -> List[Dict[str, Any]]:
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logging.error(f"Newznab XML parse error for '{instance}': {exc}")
        return results

    ns = {'newznab': 'http://www.newznab.com/DTD/2010/feeds/attributes/'}
    channel = root.find('channel')
    if channel is None:
        return results

    for item in channel.findall('item'):
        title_el = item.find('title')
        if title_el is None or not title_el.text:
            continue
        title = title_el.text.strip()

        # NZB download link is in <link> or <enclosure>
        link_el = item.find('link')
        enclosure_el = item.find('enclosure')
        nzb_url = None
        if enclosure_el is not None:
            nzb_url = enclosure_el.get('url', '')
        if not nzb_url and link_el is not None:
            nzb_url = (link_el.text or '').strip()
        if not nzb_url:
            continue

        guid_el = item.find('guid')
        guid = guid_el.text.strip() if guid_el is not None and guid_el.text else nzb_url

        # Size from enclosure or newznab:attr
        size_bytes = 0
        if enclosure_el is not None:
            try:
                size_bytes = int(enclosure_el.get('length', 0))
            except (ValueError, TypeError):
                pass
        if not size_bytes:
            for attr in item.findall('newznab:attr', ns):
                if attr.get('name') == 'size':
                    try:
                        size_bytes = int(attr.get('value', 0))
                    except (ValueError, TypeError):
                        pass
                    break

        size_gb = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0

        # Category
        cats = []
        for cat_el in item.findall('category'):
            if cat_el.text:
                cats.append(cat_el.text.strip())

        pub_date_el = item.find('pubDate')
        pub_date = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ''

        # Run PTT on the NZB title so the filter pipeline gets resolution, seasons, etc.
        try:
            ptt = ptt_parse(title)
        except Exception:
            ptt = {}

        parsed_info = {
            'guid': guid,
            'protocol': 'nzb',
            'publish_date': pub_date,
            'categories_newznab': cats,
            'source_instance': instance,
            # PTT fields expected by filter_results
            'title': ptt.get('title', title),
            'year': ptt.get('year'),
            'resolution': ptt.get('resolution'),
            'source': ptt.get('quality'),
            'audio': ptt.get('audio', []),
            'codec': ptt.get('codec'),
            'group': ptt.get('group'),
            'season': ptt.get('seasons', [None])[0] if ptt.get('seasons') else None,
            'seasons': ptt.get('seasons', []),
            'episode': ptt.get('episodes', [None])[0] if ptt.get('episodes') else None,
            'episodes': ptt.get('episodes', []),
            'hdr': ptt.get('hdr', []),
            'is_hdr': bool(ptt.get('hdr')),
            'trash': ptt.get('trash', False),
            'original_title': title,
        }

        result = {
            'title': title,
            'original_title': title,
            'size': size_gb,
            'source': instance,
            'seeders': 0,
            'hash': '',
            'parsed_info': parsed_info,
            'magnet': None,
            'torrent_url': None,
            'magnet_link': None,
            'nzb_url': nzb_url,
            'protocol': 'nzb',
        }
        results.append(result)

    logging.info(f"Newznab '{instance}': parsed {len(results)} NZB results")
    return results
