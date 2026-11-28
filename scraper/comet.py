"""Comet (https://github.com/g0ldyy/comet) Stremio addon scraper.

Comet serves ``/{b64config}/stream/{movie|series}/{imdb}[:{s}:{e}].json``.
With no config (or a config with no debrid service) it answers with plain
torrent streams carrying ``infoHash``. With a debrid config it answers with
playback URLs of the form ``.../playback/{info_hash}/{entry}/{file_idx|n}/{s}/{e}``
and no ``infoHash``; the hash is recovered from that URL (or from
``behaviorHints.bingeGroup`` = ``comet|{service}|{info_hash}``).

The URL the user supplies may carry their debrid key inside the base64 config
segment, so it is never written to the log: see ``redact_url``.
"""
import base64
import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

from database.database_reading import get_imdb_aliases
from routes.api_tracker import api

MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 0.5
# Comet answers "scraping in progress" to a request that arrives while another
# request for the same title is still scraping. Wait briefly, then ask again.
BUSY_RETRIES = 2
BUSY_WAIT_SECONDS = 3.0
# Comet may have to scrape upstream indexers on a cold title: allow a long read.
REQUEST_TIMEOUT = (10, 60)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

_HEX_HASH = re.compile(r'^[0-9a-fA-F]{40}$')
_BASE32_HASH = re.compile(r'^[A-Za-z2-7]{32}$')
_PLAYBACK_RE = re.compile(r'/playback/([0-9A-Za-z]{32,40})/(\d+)/([^/?#]+)/')
_BINGE_RE = re.compile(r'^comet\|[^|]*\|([0-9A-Za-z]{32,40})$')
_NOTICE_HOST = 'comet.feels.legal'


class CometError(Exception):
    """Raised for an unusable Comet instance/config (logged once, no results)."""


# ---------------------------------------------------------------------------
# URL / config handling
# ---------------------------------------------------------------------------

def _encode_config(config: str) -> str:
    """Return a base64 config segment from user input.

    Accepts an already base64-encoded config (what Comet's configure page puts
    in the URL) or raw JSON, which is encoded for the user.
    """
    config = (config or '').strip()
    if not config:
        return ''
    if config.startswith('{'):
        try:
            json.loads(config)
        except ValueError as e:
            raise CometError(f"Comet 'config' is not valid JSON: {e}")
        return base64.b64encode(config.encode('utf-8')).decode('ascii')
    return config.strip('/')


def derive_base_url(url: str, config: str = '') -> str:
    """Normalise whatever the user pasted into a Comet base URL (no trailing slash).

    Accepts the instance root (``https://comet.example``), the configured
    manifest URL (``https://comet.example/<b64config>/manifest.json``, also as
    ``stremio://``) or the ``/configure`` page. If ``config`` is given and the
    URL does not already carry one, it is appended as the config segment.
    """
    url = (url or '').strip()
    if not url:
        return ''
    if url.lower().startswith('stremio://'):
        url = 'https://' + url[len('stremio://'):]
    elif not re.match(r'^https?://', url, re.IGNORECASE):
        url = 'https://' + url
    parts = urlsplit(url)
    path = parts.path.rstrip('/')
    for suffix in ('/manifest.json', '/configure'):
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)].rstrip('/')
            break
    base = f"{parts.scheme}://{parts.netloc}{path}"
    if config and path.strip('/') == '':
        base = f"{base}/{_encode_config(config)}"
    return base


def redact_url(url: str) -> str:
    """Hide the (possibly debrid-key bearing) config segment of a Comet URL."""
    parts = urlsplit(url or '')
    path = re.sub(r'/[^/]+/(stream/|manifest\.json)', r'/<config>/\1', parts.path, count=1)
    return f"{parts.scheme}://{parts.netloc}{path}" if parts.scheme else path


def construct_url(base_url: str, imdb_id: str, content_type: str,
                  season: int = None, episode: int = None) -> str:
    if season is not None and episode is None:
        episode = 1
    if content_type == 'movie':
        return f"{base_url}/stream/movie/{imdb_id}.json"
    if content_type == 'episode' and season is not None and episode is not None:
        return f"{base_url}/stream/series/{imdb_id}:{season}:{episode}.json"
    if content_type == 'episode':
        return f"{base_url}/stream/series/{imdb_id}.json"
    logging.error("Comet: invalid content type provided. Must be 'movie' or 'episode'.")
    return ''


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get(url: str):
    # api.session directly: api.get() logs str(exception), which embeds the full
    # URL and so the user's config segment.
    return api.session.get(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
                           timeout=REQUEST_TIMEOUT)


def _sleep_backoff(attempt: int) -> float:
    return BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.25)


def fetch_data(url: str) -> Dict:
    """GET a Comet JSON endpoint with the same retry/backoff as torrentio.

    5xx and connection errors/timeouts are retried; other statuses (4xx) are not.
    Returns {} on failure.
    """
    safe = redact_url(url)
    for attempt in range(MAX_RETRIES):
        last = attempt == MAX_RETRIES - 1
        try:
            response = _http_get(url)
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    logging.error(f"Comet: malformed JSON from {safe} - is this a Comet instance URL?")
                    return {}
                if not isinstance(data, dict):
                    logging.error(f"Comet: unexpected response shape from {safe} (expected a JSON object)")
                    return {}
                return data
            if (status >= 500 or status == 429) and not last:
                wait = _sleep_backoff(attempt)
                logging.warning(f"Comet: HTTP {status} from {safe} (attempt {attempt + 1}/{MAX_RETRIES}). Retrying in {wait:.2f}s")
                time.sleep(wait)
                continue
            if status in (401, 403):
                logging.error(f"Comet: HTTP {status} from {safe} - the instance rejected the request (private instance or wrong config?)")
            elif status == 404:
                logging.error(f"Comet: HTTP 404 from {safe} - check the URL points at a Comet instance (or its manifest.json)")
            else:
                logging.warning(f"Comet: non-200 status ({status}) for {safe}")
            return {}
        except Exception as e:
            # requests exceptions embed the URL (and thus the config) - log the type only
            if not last:
                wait = _sleep_backoff(attempt)
                logging.warning(f"Comet: {type(e).__name__} fetching {safe} (attempt {attempt + 1}/{MAX_RETRIES}). Retrying in {wait:.2f}s")
                time.sleep(wait)
                continue
            logging.error(f"Comet: {type(e).__name__} fetching {safe} - giving up")
            return {}
    return {}


def validate_manifest(url: str, config: str = '') -> Tuple[bool, str, Dict[str, Any]]:
    """Check that the URL is a working Comet manifest. Returns (ok, message, details)."""
    try:
        base = derive_base_url(url, config)
    except CometError as e:
        return False, str(e), {}
    if not base:
        return False, 'URL not configured', {}
    safe = redact_url(f"{base}/manifest.json")
    try:
        response = _http_get(f"{base}/manifest.json")
    except Exception as e:
        return False, f"Could not reach {safe}: {type(e).__name__}", {'url': safe}
    if response.status_code != 200:
        return False, f"Manifest returned HTTP {response.status_code} ({safe})", {'url': safe}
    try:
        manifest = response.json()
    except ValueError:
        return False, f"{safe} did not return JSON - not a Comet manifest", {'url': safe}
    if not isinstance(manifest, dict) or 'id' not in manifest or 'resources' not in manifest:
        return False, 'Invalid Stremio manifest format', {'url': safe}
    name = str(manifest.get('name', ''))
    description = str(manifest.get('description', ''))
    if 'OBSOLETE CONFIGURATION' in description:
        return False, ('Comet reports an OBSOLETE CONFIGURATION: re-configure on the instance '
                       'and paste the new manifest URL'), {'url': safe}
    resources = [r.get('name') if isinstance(r, dict) else r for r in manifest.get('resources', [])]
    if 'stream' not in resources:
        return False, "Manifest has no 'stream' resource", {'url': safe}
    return True, 'OK', {'url': safe, 'addon_name': name or 'Unknown',
                        'addon_version': manifest.get('version', 'Unknown')}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def normalize_info_hash(value: str) -> str:
    """Lowercase 40-char hex hash, converting a 32-char base32 one; '' if invalid."""
    value = (value or '').strip()
    if _HEX_HASH.match(value):
        return value.lower()
    if _BASE32_HASH.match(value):
        try:
            return base64.b16encode(base64.b32decode(value.upper())).decode('ascii').lower()
        except Exception:
            return ''
    return ''


def extract_info_hash(stream: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """Return (info_hash, file_idx) for a Comet stream, ('', None) if unrecoverable."""
    file_idx = stream.get('fileIdx')
    if not isinstance(file_idx, int) or isinstance(file_idx, bool):
        file_idx = None

    info_hash = normalize_info_hash(stream.get('infoHash') or '')
    if info_hash:
        return info_hash, file_idx

    url = stream.get('url') or ''
    match = _PLAYBACK_RE.search(url) if isinstance(url, str) else None
    if match:
        info_hash = normalize_info_hash(match.group(1))
        if info_hash:
            idx = match.group(3)
            return info_hash, int(idx) if idx.isdigit() else None

    hints = stream.get('behaviorHints') or {}
    binge = hints.get('bingeGroup') if isinstance(hints, dict) else None
    match = _BINGE_RE.match(binge) if isinstance(binge, str) else None
    if match:
        info_hash = normalize_info_hash(match.group(1))
        if info_hash:
            return info_hash, None
    return '', None


def _is_notice(stream: Dict[str, Any]) -> bool:
    url = stream.get('url')
    # Comet's status/error pseudo-streams link to the bare https://comet.feels.legal.
    # That host is also the public instance, whose real debrid results link to
    # .../playback/<hash>/..., so only the bare root counts as a notice.
    if not isinstance(url, str) or stream.get('infoHash'):
        return False
    parts = urlsplit(url.strip())
    return parts.netloc.lower() == _NOTICE_HOST and parts.path.strip('/') == ''


def parse_size(text: str) -> float:
    """GB from a '💾 1.5 GB' fragment (Comet uses 1024 steps), 0.0 if absent."""
    m = re.search(r'💾\s*([\d.]+)\s*([KMGTP]?i?B)\b', text or '', re.IGNORECASE)
    if not m:
        m = re.search(r'\bSize:\s*([\d.]+)\s*([KMGTP]?i?B)\b', text or '', re.IGNORECASE)
    if not m:
        return 0.0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2).upper()[0] if m.group(2).upper() != 'B' else 'B'
    factors = {'B': 1 / 1024 ** 3, 'K': 1 / 1024 ** 2, 'M': 1 / 1024, 'G': 1.0, 'T': 1024.0, 'P': 1024.0 ** 2}
    return value * factors.get(unit, 0.0)


def parse_seeder(text: str) -> Optional[int]:
    m = re.search(r'(?:👤|\bSeeders:)\s*(\d+)', text or '')
    return int(m.group(1)) if m else None


def parse_results(streams: List[Dict[str, Any]], instance: str) -> List[Dict[str, Any]]:
    results = []
    seen = set()
    no_hash = 0
    for stream in streams or []:
        try:
            if not isinstance(stream, dict):
                continue
            if _is_notice(stream):
                continue  # status/error pseudo-streams, reported by _log_notices
            info_hash, file_idx = extract_info_hash(stream)
            if not info_hash:
                no_hash += 1
                logging.debug(f"Comet: skipping stream without a recoverable info hash: {str(stream.get('name'))[:80]!r}")
                continue
            if info_hash in seen:
                continue  # same torrent listed for several debrid entries / P2P
            seen.add(info_hash)

            hints = stream.get('behaviorHints') or {}
            if not isinstance(hints, dict):
                hints = {}
            filename = hints.get('filename') or None
            description = stream.get('description') or stream.get('title') or ''
            lines = [ln.strip() for ln in str(description).split('\n') if ln.strip()]

            # Title: Comet's "📄 <torrent title>" line, else the filename hint.
            title = ''
            for ln in lines:
                if ln.startswith('📄'):
                    title = ln[1:].strip()
                    break
            if not title:
                title = (filename or '').strip()
            if not title and lines:
                title = lines[0]
            if not title:
                logging.debug(f"Comet: skipping {info_hash}: no title")
                continue

            size = 0.0
            video_size = hints.get('videoSize')
            if isinstance(video_size, (int, float)) and not isinstance(video_size, bool) and video_size > 0:
                size = video_size / (1024 ** 3)
            seeders = 0
            tracker = ''
            for ln in lines:
                if not size:
                    size = parse_size(ln)
                s = parse_seeder(ln)
                if s is not None:
                    seeders = s
                m = re.search(r'(?:🔎|\bSource:)\s*(.+)$', ln)
                if m:
                    tracker = m.group(1).strip()
            # Comet prefixes its own cache hits "Comet|<tracker>"
            tracker = tracker.rsplit('|', 1)[-1].strip() if tracker.startswith('Comet|') else tracker

            parsed_info = {'filename': filename, 'bingeGroup': hints.get('bingeGroup')}
            if tracker:
                parsed_info['source_site'] = tracker

            magnet = f'magnet:?xt=urn:btih:{info_hash}'
            if file_idx is not None:
                magnet += f'&dn={quote_plus(filename or title)}&so={file_idx}'

            results.append({
                'title': title,
                'size': round(size, 2),
                'source': f'{instance}{f" - {tracker}" if tracker else ""}',
                'indexer': tracker,
                'magnet': magnet,
                'seeders': seeders,
                'info_hash': info_hash,
                'parsed_info': parsed_info,
            })
        except Exception as e:
            logging.error(f"Comet: error parsing stream: {e}", exc_info=True)
    if no_hash:
        logging.debug(f"Comet {instance}: skipped {no_hash} stream(s) with no info hash")
    return results


def _notices(streams: List[Any]) -> List[str]:
    return [str(s.get('description', '')).replace('\n', ' ') for s in streams
            if isinstance(s, dict) and _is_notice(s)]


def _fetch_streams(url: str, instance: str) -> Optional[List[Any]]:
    """Fetch one stream list; None on failure. Handles Comet's status pseudo-streams."""
    for attempt in range(BUSY_RETRIES + 1):
        response = fetch_data(url)
        if not response:
            return None
        streams = response.get('streams')
        if not isinstance(streams, list):
            logging.error(f"Comet {instance}: response has no 'streams' list - is the URL a Comet addon? ({redact_url(url)})")
            return None
        notices = _notices(streams)
        real = [s for s in streams if isinstance(s, dict) and not _is_notice(s)]
        if notices and not real:
            text = ' | '.join(notices)
            if 'Scraping in progress' in text and attempt < BUSY_RETRIES:
                logging.info(f"Comet {instance}: scrape in progress, retrying in {BUSY_WAIT_SECONDS:.0f}s")
                time.sleep(BUSY_WAIT_SECONDS)
                continue
            if 'OBSOLETE CONFIGURATION' in text:
                logging.error(f"Comet {instance}: the instance says the config is OBSOLETE. Re-configure on the "
                              f"Comet site and paste the new manifest URL into the scraper settings.")
            else:
                logging.warning(f"Comet {instance}: no streams, instance said: {text[:200]}")
            return []
        for note in notices:
            logging.warning(f"Comet {instance}: {note[:200]}")
        return streams
    return []


def scrape_comet_instance(instance: str, settings: Dict[str, Any], imdb_id: str, title: str, year: int,
                          content_type: str, season: int = None, episode: int = None,
                          multi: bool = False) -> List[Dict[str, Any]]:
    try:
        base_url = derive_base_url(settings.get('url', ''), settings.get('config', ''))
        if not base_url:
            logging.error(f"Comet {instance}: no URL configured")
            return []
        if not imdb_id:
            logging.debug(f"Comet {instance}: no IMDb ID, nothing to look up")
            return []

        all_results = []
        imdb_ids = get_imdb_aliases(imdb_id)
        for current_imdb_id in imdb_ids:
            url = construct_url(base_url, current_imdb_id, content_type, season, episode)
            if not url:
                return []
            logging.debug(f"Comet {instance}: scraping {redact_url(url)}")
            streams = _fetch_streams(url, instance)
            if not streams:
                logging.warning(f"No streams found for IMDb ID: {current_imdb_id} in instance {instance}")
                continue
            all_results.extend(parse_results(streams, instance))

        seen_hashes = set()
        unique_results = []
        for result in all_results:
            if result['info_hash'] not in seen_hashes:
                seen_hashes.add(result['info_hash'])
                unique_results.append(result)
        logging.debug(f"Comet {instance}: {len(unique_results)} unique results after checking {len(imdb_ids)} IMDB IDs")
        return unique_results
    except CometError as e:
        logging.error(f"Comet {instance}: {e}")
        return []
    except Exception as e:
        logging.error(f"Error in scrape_comet_instance for {instance}: {str(e)}", exc_info=True)
        return []
