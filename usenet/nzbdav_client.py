"""
Thin client for submitting NZB downloads to NzbDAV (https://github.com/nzbdav-dev/nzbdav).

NzbDAV exposes a SABnzbd-compatible API at /api with mode-based query params:
  - mode=version       : version check
  - mode=addfile       : POST multipart .nzb file upload (field 'name'), with cat + nzbname
  - mode=addurl        : GET/POST with name=<URL>, cat + nzbname — nzbdav fetches the URL itself
  - mode=queue         : current download queue, returns slots[]
  - mode=history       : completed entries, returns slots[] with storage path
  - mode=history&name=delete&value=<nzo_id> : delete history entry
  - mode=get_cats      : configured categories (must include cat before submit)

Files appear on the WebDAV mount under two roots:
  /content/<cat>/<nzbname>/                    : virtual files served via WebDAV
  /completed-symlinks/<cat>/<nzbname>/         : symlinks to .ids/{uuid} per file
The `storage` field in history points to the completed-symlinks root.

Designed to match the CliMountClient class interface so cli-debrid can route
either provider transparently via the factory in usenet/__init__.py.

LIMITATIONS vs climount:
  * NzbDAV has no user-triggered health-check API — health checks run internally
    via HealthCheckService. trigger_health_check() / poll_health_result() return
    no-op success so callers don't break, but cannot drive on-demand repair.
  * NzbDAV stores no separate 'downloadFolder' override — destination is purely
    determined by `cat`. The category must already exist in nzbdav's
    `api.categories` config (default: audio,software,tv,movies — extendable).
  * Browse-by-folder-name is not part of nzbdav's API; we fall back to a
    filesystem listing of the mounted WebDAV root (env var NZBDAV_MOUNT_PATH or
    config 'mounted_file_location' minus '/__all__').
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any, Tuple, List

from utilities.settings import get_setting
from routes.api_tracker import api


# Video-file extensions used for "is this a media file" checks in browse helpers.
_VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.m4v', '.ts'}

# ── Category taxonomy (single source of truth) ─────────────────────────────
#
# nzbdav (unlike cli_mount) does NO post-categorisation: whatever SAB category a
# job is submitted under becomes the folder, verbatim. If cli-debrid submits a
# category that the instance hasn't created (and that no Plex section maps), the
# item is invisible to Plex and loops in "Wanted". So the set of categories the
# submit-router can emit, the set the setup-helper tells the user to create, and
# the set repair/health is allowed to act on MUST be identical. Historically
# these were three independent hard-coded lists that drifted apart.
#
# They now all derive from ONE place: the taxonomy below + an optional per-user
# `Usenet Provider.nzbdav_category_map`.
#
# `_CATEGORY_PARENT` is the specificity ladder: each detailed bucket falls back
# to a less-detailed parent when the user hasn't mapped it, e.g.
#   movies_2160p_remux → movies_2160p → movies
# Base buckets (movies/shows) and music have no parent.
_CATEGORY_PARENT = {
    'movies_2160p_remux': 'movies_2160p',
    'movies_1080p_remux': 'movies_1080p',
    'movies_2160p':       'movies',
    'movies_1080p':       'movies',
    'anime_movies':       'movies',
    'shows_2160p':        'shows',
    'shows_1080p':        'shows',
    'anime_shows':        'shows',
}

# Every category the title heuristic can emit.
_ALL_CATEGORIES = set(_CATEGORY_PARENT) | {'movies', 'shows', 'music', '__unplayable__'}

# Default "managed" set when no category map is configured. 'music' is excluded —
# a shared nzbdav typically serves music via Lidarr and repair must never purge
# another app's history. This is what repair/health and the setup helper use by
# default; the submit-router uses identity routing (stock names) when unmapped.
_DEFAULT_MANAGED_CATEGORIES = set(_ALL_CATEGORIES) - {'music'}

# Pseudo-bucket key in the category map: the catch-all used when nothing in a
# bucket's parent chain is mapped (e.g. an unmapped 'music', or an unstructured
# title the heuristic couldn't classify).
_FALLBACK_KEY = 'fallback'

# Back-compat alias: external callers referenced _DEFAULT_OWNED_CATEGORIES.
_DEFAULT_OWNED_CATEGORIES = _DEFAULT_MANAGED_CATEGORIES


def _parse_category_map(value) -> dict:
    """Parse a `bucket=name, bucket=name` string into a {bucket: name} dict.

    Tolerant of surrounding whitespace and malformed/empty entries. Accepts a
    dict unchanged. Empty/falsey input → {} (identity routing = stock behaviour).
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip()
                for k, v in value.items() if str(k).strip() and str(v).strip()}
    out = {}
    for part in str(value).split(','):
        part = part.strip()
        if not part or '=' not in part:
            continue
        k, v = part.split('=', 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def _resolve_category(bucket: str, cat_map: dict) -> str:
    """Map a heuristic bucket onto a real nzbdav category name via `cat_map`.

    Walks the specificity ladder (movies_2160p_remux → movies_2160p → movies) to
    the first bucket present in `cat_map`. If none match, uses the explicit
    'fallback' mapping; else returns '' (caller applies self.default_category).
    An empty `cat_map` means identity: the bucket name is used verbatim — this is
    exactly the stock taxonomy, so existing setups are unaffected.
    """
    if not cat_map:
        return bucket or ''
    node = bucket
    while node:
        if node in cat_map:
            return cat_map[node]
        node = _CATEGORY_PARENT.get(node)
    # Tag-exclusive routing passes the tag itself as `bucket`. A tag is not a
    # structural bucket (movies/shows/resolution/remux/anime) and has no parent
    # chain, so without this it would fall through to the catch-all whenever a
    # category map is configured. Land tag-exclusive items in their own category
    # verbatim instead. Structural buckets stay in _ALL_CATEGORIES → unchanged.
    if bucket and bucket not in _ALL_CATEGORIES:
        return bucket
    return cat_map.get(_FALLBACK_KEY, '')


def managed_categories(cat_map: dict) -> set:
    """The distinct real category names cli-debrid manages on this instance.

    With a map: the set of its values (the actual category names, incl. the
    fallback's). Without a map: the default taxonomy (music excluded). This one
    function feeds the setup-helper required list AND the repair owned-set, so
    they cannot disagree with what the submit-router emits.
    """
    if cat_map:
        return set(cat_map.values())
    return set(_DEFAULT_MANAGED_CATEGORIES)


# Title-based category detection patterns.
# Order matters: ANIME > SHOW > MUSIC > MOVIE (most specific first).
_SHOW_PATTERN    = re.compile(r'\bS\d{1,2}E\d{1,3}\b|\bS\d{1,2}\b|\bSeason[.\s_]\d+\b', re.IGNORECASE)
_MUSIC_PATTERN   = re.compile(r'\b(FLAC|MP3|320[\s_-]?kbps|hi-?res|discography)\b', re.IGNORECASE)
_MOVIE_YEAR      = re.compile(r'\b(19[5-9]\d|20[0-3]\d)\b')
_QUALITY_1080P   = re.compile(r'\b1080p\b', re.IGNORECASE)
_QUALITY_2160P   = re.compile(r'\b2160p\b|\b4K\b|\bUHD\b', re.IGNORECASE)
_REMUX_PATTERN   = re.compile(r'\bREMUX\b', re.IGNORECASE)


def _detect_category_from_title(title: str, is_anime: bool = False, media_type: str = '',
                                  tags=None, tags_exclusive: bool = False) -> str:
    """Derive nzbdav category from release title + optional metadata.

    Categories (all qualities also land in movies/shows base category via owned_categories):
      movies / shows              base buckets — all resolutions
      movies_1080p / shows_1080p  1080p content (any codec)
      movies_2160p / shows_2160p  4K/UHD content (any codec)
      movies_1080p_remux          1080p remux
      movies_2160p_remux          4K remux
      anime_movies / anime_shows  anime (identified by cli's trigger_is_anime flag)
      music                       audio releases
      __unplayable__              fallback

    Remux items land in BOTH the remux category AND the base resolution category
    (caller submits once; ownership tracking covers both via _DEFAULT_OWNED_CATEGORIES).

    Returns the most-specific category name, or empty string (caller falls back
    to self.default_category).
    """
    # Tag-based routing: if tags are set and tags_exclusive is True,
    # use the first tag as the category (lowercased, spaces→underscores).
    # If tags_exclusive is False, tags are embedded in the title only (cli_mount behavior)
    # and resolution detection runs normally.
    if tags and tags_exclusive:
        first_tag = str(tags).split(',')[0].strip().lower().replace(' ', '_')
        if first_tag:
            return first_tag

    if not title:
        return ''

    is_show = bool(_SHOW_PATTERN.search(title)) or media_type == 'episode'
    is_movie = (not is_show) and (bool(_MOVIE_YEAR.search(title)) or media_type == 'movie')

    if not is_show and not is_movie:
        if _MUSIC_PATTERN.search(title):
            return 'music'
        return ''

    is_1080p = bool(_QUALITY_1080P.search(title))
    is_2160p = bool(_QUALITY_2160P.search(title))
    is_remux = bool(_REMUX_PATTERN.search(title))

    # Anime takes priority over resolution splits
    if is_anime:
        return 'anime_shows' if is_show else 'anime_movies'

    if is_show:
        if is_2160p:
            return 'shows_2160p'
        if is_1080p:
            return 'shows_1080p'
        return 'shows'

    # movie path
    if is_remux:
        if is_2160p:
            return 'movies_2160p_remux'
        if is_1080p:
            return 'movies_1080p_remux'
    if is_2160p:
        return 'movies_2160p'
    if is_1080p:
        return 'movies_1080p'
    return 'movies'


class NzbdavClient:
    """Submits NZBs to nzbdav and polls for completion.

    Mirrors the CliMountClient interface 1:1 so cli-debrid call sites are
    drop-in compatible. Differences in behaviour are documented per method.
    """

    PROVIDER_NAME = "NzbDAV (Usenet)"

    def __init__(self):
        cfg = get_setting('Usenet Provider') or {}
        self.base_url = cfg.get('url', 'http://localhost:3000').rstrip('/')
        # nzbdav uses ?apikey= for SAB-API auth — not Bearer headers.
        self.api_key = cfg.get('api_token', '')
        # category to default to when caller passes none AND title-heuristic fails.
        # Convention: '__unplayable__' mirrors zurg's catch-all bucket name.
        self.default_category = cfg.get('download_folder', '') or '__unplayable__'
        self.enabled = cfg.get('enabled', False)
        # Which nzbdav categories repair/health may act on. nzbdav history is SHARED
        # with other SAB clients (Lidarr music, optionally Radarr/Sonarr), and repair
        # can only re-acquire content cli-debrid manages — so it must never touch
        # another app's entries. Resolution (all setup-agnostic, no hard-coding of
        # one user's layout):
        #   include = `owned_categories` config if set, else the heuristic's video
        #             categories + the configured fallback (auto-picked default)
        #   then subtract `exclude_categories` config (deny-list, optional)
        # Optional user map: heuristic bucket -> real category name on this
        # instance (see _resolve_category). Empty = stock identity routing.
        self.category_map = _parse_category_map(cfg.get('nzbdav_category_map'))
        self.owned_categories = self._resolve_owned_categories(cfg)
        # Host-side filesystem path to where the nzbdav WebDAV mount appears
        # (used for browse helpers since nzbdav has no /browse API). Default to
        # the standard rclone-sidecar mount point shipped with nzbdav docs.
        self.mount_path = cfg.get('mounted_file_location', '').rstrip('/')
        if self.mount_path.endswith('/__all__'):
            self.mount_path = self.mount_path[: -len('/__all__')]
        if not self.mount_path:
            self.mount_path = '/mnt/remote/nzbdav'
        # Flag set by add_nzb_content when nzbdav reports ARTICLE_NOT_FOUND-style
        # errors (matches CliMountClient.last_missing_segments contract).
        self.last_missing_segments = False

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _parse_cat_list(value) -> set:
        """Accept a comma-separated string or a list; return a lowercased set."""
        if not value:
            return set()
        if isinstance(value, str):
            value = value.split(',')
        return {str(c).strip().lower() for c in value if str(c).strip()}

    def _resolve_owned_categories(self, cfg: dict) -> set:
        """Determine which nzbdav categories repair/health may act on.

        include (allow-list) = config 'owned_categories' if set, else the
        auto-picked default (heuristic video cats + configured fallback).
        Then subtract config 'exclude_categories' (deny-list).
        """
        include = self._parse_cat_list(cfg.get('owned_categories'))
        if not include:
            # Derive from the same source the submit-router and setup-helper use,
            # so repair never owns a category the router can't emit (and never a
            # category another app, e.g. Lidarr's music, manages).
            include = managed_categories(_parse_category_map(cfg.get('nzbdav_category_map')))
            if self.default_category:
                include.add(self.default_category.lower())
        # Add any tag categories defined in content sources
        try:
            from utilities.settings import get_setting
            content_sources = get_setting('Content Sources') or {}
            for source_id, source_cfg in content_sources.items():
                if isinstance(source_cfg, dict):
                    for tag in source_cfg.get('tags', []):
                        t = str(tag).strip().lower().replace(' ', '_')
                        if t:
                            include.add(t)
        except Exception:
            pass
        include -= self._parse_cat_list(cfg.get('exclude_categories'))
        return include

    def _route_category(self, title: str, **kw) -> str:
        """Detect the release bucket from the title, then resolve it to a real
        category name on this instance via the configured category map."""
        bucket = _detect_category_from_title(title, **kw)
        return _resolve_category(bucket, self.category_map)

    def _sab_params(self, **extra) -> Dict[str, str]:
        """Build query params for nzbdav SAB-API calls. apikey is always added."""
        p: Dict[str, str] = {}
        if self.api_key:
            p['apikey'] = self.api_key
        p.update({k: str(v) for k, v in extra.items() if v is not None})
        return p

    def _sab_url(self) -> str:
        return f'{self.base_url}/api'

    def is_enabled(self) -> bool:
        return bool(self.enabled and self.base_url)

    # -- 1:1 interface mirror ----------------------------------------------

    def check_connectivity(self) -> tuple:
        """Returns (ok: bool, error: str|None). Calls mode=version."""
        try:
            r = api.get(self._sab_url(), params=self._sab_params(mode='version'), timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get('version'):
                    return True, None
                return False, f'unexpected response: {str(data)[:200]}'
            return False, f'HTTP {r.status_code}'
        except Exception as exc:
            return False, str(exc)

    def add_nzb_content(self, nzb_content: str, title: str = '', category: str = '',
                        is_anime: bool = False, media_type: str = '',
                        tags=None, tags_exclusive: bool = False) -> Optional[str]:
        """Submit NZB content directly as a file upload.

        Matches CliMountClient.add_nzb_content signature & return contract.
        Returns the nzo_id string on success, None on failure.
        """
        self.last_missing_segments = False
        if not self.is_enabled():
            logging.warning('[NzbDAV] Usenet provider is disabled or not configured')
            return None

        cat = category or self._route_category(title, is_anime=is_anime, media_type=media_type,
                                               tags=tags, tags_exclusive=tags_exclusive) or self.default_category
        # nzbdav uses `nzbname` for the resulting folder name; default to title
        nzbname = title or 'download'
        filename = f'{nzbname}.nzb'

        try:
            r = api.post(
                self._sab_url(),
                params=self._sab_params(mode='addfile', cat=cat, nzbname=nzbname),
                files={'name': (filename, nzb_content.encode('utf-8'), 'application/x-nzb')},
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json() or {}
                if data.get('status') is True and data.get('nzo_ids'):
                    job_id = data['nzo_ids'][0]
                    logging.info(f'[NzbDAV] NZB content submitted: id={job_id} title={title!r}')
                    return str(job_id)
                err_msg = str(data.get('error') or '')
                logging.error(f'[NzbDAV] add_nzb_content error: {err_msg}')
                if 'ARTICLE_NOT_FOUND' in err_msg.upper() or 'article not found' in err_msg.lower():
                    self.last_missing_segments = True
                return None
            logging.error(f'[NzbDAV] add_nzb_content HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[NzbDAV] add_nzb_content exception: {exc}')
            return None

    def add_nzb(self, nzb_url: str, title: str = '', category: str = '',
                is_anime: bool = False, media_type: str = '',
                tags=None, tags_exclusive: bool = False) -> Optional[str]:
        """Submit an NZB URL — nzbdav fetches it server-side via mode=addurl.

        Falls back to pre-fetch + add_nzb_content if the server-side fetch fails
        (e.g. if the indexer blocks nzbdav's User-Agent).
        Matches CliMountClient.add_nzb signature & return contract.
        """
        self.last_missing_segments = False
        if not self.is_enabled():
            logging.warning('[NzbDAV] Usenet provider is disabled or not configured')
            return None

        cat = category or self._route_category(title, is_anime=is_anime, media_type=media_type,
                                               tags=tags, tags_exclusive=tags_exclusive) or self.default_category
        nzbname = title or 'download'

        try:
            r = api.post(
                self._sab_url(),
                params=self._sab_params(mode='addurl', name=nzb_url, cat=cat, nzbname=nzbname),
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json() or {}
                if data.get('status') is True and data.get('nzo_ids'):
                    job_id = data['nzo_ids'][0]
                    logging.info(f'[NzbDAV] NZB URL submitted: id={job_id} title={title!r}')
                    return str(job_id)
                err_msg = str(data.get('error') or '')
                # If server-side fetch fails, retry with pre-fetched content.
                if 'fetch' in err_msg.lower() or 'received status code' in err_msg.lower():
                    logging.info(f'[NzbDAV] addurl failed ({err_msg!r}), retrying with pre-fetched content')
                    try:
                        _r = api.get(nzb_url, timeout=15, allow_redirects=True,
                                     headers={'User-Agent': 'Sabnzbd/3.0.0'})
                        if _r.status_code == 200 and '<nzb' in _r.text.lower():
                            return self.add_nzb_content(_r.text, title=title, category=category,
                                                       is_anime=is_anime, media_type=media_type,
                                                       tags=tags, tags_exclusive=tags_exclusive)
                    except Exception as pe:
                        logging.warning(f'[NzbDAV] Pre-fetch retry error: {pe}')
                logging.error(f'[NzbDAV] add_nzb error: {err_msg}')
                return None
            logging.error(f'[NzbDAV] add_nzb HTTP {r.status_code}: {r.text[:300]}')
            return None
        except Exception as exc:
            logging.error(f'[NzbDAV] add_nzb exception: {exc}')
            return None

    # -- browse helpers (filesystem-backed since nzbdav has no /browse API) -

    def _content_root(self) -> str:
        """Return the root path that contains category subdirectories.

        The mount_path setting points to where category folders live directly
        (e.g. /debrid/content/ which contains movies/, shows/, etc.).
        """
        return self.mount_path

    def _find_nzb_folder(self, job_norm: str, original_name: str = '') -> Optional[str]:
        """Find an nzbdav content folder matching the job-name.

        nzbdav has no `/api/browse/nzbs/<name>` endpoint, so we list the host
        mount filesystem under <content_root>/<cat>/. We try `original_name`
        as a fast direct stat first, then fall through to a normalised scan.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        content_root = self._content_root()
        if not os.path.isdir(content_root):
            logging.debug(f'[NzbDAV] content root not found: {content_root}')
            return None

        # Exact + nzbdav dedup-suffix matches ("Name", "Name (2)", "Name (3)").
        # nzbdav appends " (N)" when a folder of that name already exists, so on a
        # re-grab/upgrade the freshly-downloaded copy carries the newest mtime.
        # Picking the newest avoids resolving to a stale older copy (which the old
        # fast-path did by returning the bare exact name). The match is precise —
        # only true dedup variants of original_name — so it cannot grab an
        # unrelated folder.
        dedup_re = re.compile(r'^' + re.escape(original_name) + r' \(\d+\)$') if original_name else None
        dedup_matches = []  # (entry, mtime)
        try:
            for cat_dir in os.listdir(content_root):
                cat_path = os.path.join(content_root, cat_dir)
                if not os.path.isdir(cat_path):
                    continue
                for entry in os.listdir(cat_path):
                    if original_name and (entry == original_name or (dedup_re and dedup_re.match(entry))):
                        try:
                            mt = os.path.getmtime(os.path.join(cat_path, entry))
                        except OSError:
                            mt = 0
                        dedup_matches.append((entry, mt))
        except Exception as exc:
            logging.warning(f'[NzbDAV] _find_nzb_folder scan error: {exc}')
        if dedup_matches:
            dedup_matches.sort(key=lambda c: c[1], reverse=True)
            best = dedup_matches[0][0]
            if len(dedup_matches) > 1:
                logging.info(f'[NzbDAV] _find_nzb_folder: {len(dedup_matches)} name-collision variants '
                             f'for {original_name!r}, picked newest {best!r}')
            return best

        # Fuzzy fallback: normalised match (unchanged behaviour).
        try:
            for cat_dir in os.listdir(content_root):
                cat_path = os.path.join(content_root, cat_dir)
                if not os.path.isdir(cat_path):
                    continue
                for entry in os.listdir(cat_path):
                    name_norm = _norm(entry)
                    if name_norm == job_norm or job_norm in name_norm or name_norm in job_norm:
                        return entry
        except Exception as exc:
            logging.warning(f'[NzbDAV] _find_nzb_folder error: {exc}')
        return None

    @staticmethod
    def _is_uuid_filename(fname: str) -> bool:
        """Return True if fname is a NzbDAV internal UUID/hash (not a real release name).

        Real release filenames always contain at least one word separator in the stem:
        a dot, space, or underscore. UUID/hash stems have none of these.
        Examples of garbage: yIYL.mkv, v7RV.mkv, BgZbaqpxb0zg.mkv, KTBQ5bs1UjvhjTX.mkv
        Examples of real: Movie.Title.2024.mkv, Movie Title 2024.mkv, Movie_Title.mkv
        """
        stem = os.path.splitext(fname)[0]
        return '.' not in stem and ' ' not in stem and '_' not in stem

    def _rename_uuid_to_folder(self, file_path: str, folder_name: str) -> Optional[str]:
        """Rename a UUID-named file to match the folder name. Returns new filename or None on failure."""
        try:
            ext = os.path.splitext(file_path)[1]
            new_name = folder_name + ext
            new_path = os.path.join(os.path.dirname(file_path), new_name)
            if os.path.exists(new_path):
                return new_name  # already renamed
            os.rename(file_path, new_path)
            logging.info(f'[NzbDAV] Renamed UUID file to: {new_name!r}')
            return new_name
        except Exception as e:
            logging.warning(f'[NzbDAV] Failed to rename UUID file {file_path!r}: {e}')
            return None

    def _list_nzb_folder_files(self, folder_name: str) -> list:
        """Return all video files inside an nzbdav content folder (name, size).

        For single-file folders with UUID names, renames the file to match the
        folder name so it's usable as a media filename.
        Multi-file folders with all UUID names stay in Checking and retry.
        """
        content_root = self._content_root()
        if not os.path.isdir(content_root):
            return []
        try:
            for cat_dir in os.listdir(content_root):
                folder_path = os.path.join(content_root, cat_dir, folder_name)
                if not os.path.isdir(folder_path):
                    continue
                results = []
                uuid_files = []
                for root, _dirs, files in os.walk(folder_path):
                    for fname in files:
                        if os.path.splitext(fname)[1].lower() not in _VIDEO_EXTS:
                            continue
                        full_path = os.path.join(root, fname)
                        try:
                            size = os.path.getsize(full_path)
                        except OSError:
                            size = 0
                        if self._is_uuid_filename(fname):
                            uuid_files.append((full_path, fname, size))
                        else:
                            results.append((fname, size))

                # If we have real filenames, return them (ignore UUIDs)
                if results:
                    return results

                # Only UUID files found
                if len(uuid_files) == 1:
                    # Single file — safe to rename to folder name
                    full_path, fname, size = uuid_files[0]
                    new_name = self._rename_uuid_to_folder(full_path, folder_name)
                    if new_name:
                        return [(new_name, size)]
                    # Rename failed — skip
                    logging.debug(f'[NzbDAV] UUID rename failed for {fname!r} in {folder_name!r}')
                elif len(uuid_files) > 1:
                    # Multiple UUID files (season pack) — cannot safely rename, retry later
                    logging.debug(f'[NzbDAV] {len(uuid_files)} UUID files in {folder_name!r} — retrying later')

                return []
        except Exception as exc:
            logging.warning(f'[NzbDAV] _list_nzb_folder_files error for {folder_name!r}: {exc}')
        return []

    def get_nzb_file_info(self, job_name: str, season: int = None, episode: int = None) -> Optional[Tuple[str, str]]:
        """Find folder + best-matching video for a completed job.

        Identical signature & return contract to CliMountClient.
        """
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)
        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name)
            if not folder_name:
                logging.debug(f'[NzbDAV] No folder found for job {job_name!r}')
                return None

            video_files = self._list_nzb_folder_files(folder_name)
            if not video_files:
                logging.warning(f'[NzbDAV] No video files in folder {folder_name!r}')
                return folder_name, None

            from debrid.common import filter_unwanted_video_files
            video_files = filter_unwanted_video_files(video_files)

            best_file = None
            if season is not None and episode is not None:
                ep_pat = re.compile(
                    rf'[Ss]{season:02d}[Ee]{episode:02d}(?![0-9])',
                    re.IGNORECASE,
                )
                for name, _ in video_files:
                    if ep_pat.search(name):
                        best_file = name
                        break

            if not best_file:
                best_file = max(video_files, key=lambda x: x[1])[0]

            logging.info(f'[NzbDAV] get_nzb_file_info: folder={folder_name!r} file={best_file!r}')
            return folder_name, best_file
        except Exception as exc:
            logging.warning(f'[NzbDAV] get_nzb_file_info error for {job_name!r}: {exc}')
            return None

    def get_nzb_folder_all_files(self, job_name: str) -> Optional[Tuple[str, list]]:
        """Return all video files in folder, sorted by name."""
        def _norm(s):
            return re.sub(r'[^a-z0-9]', '', s.lower())

        job_norm = _norm(job_name)
        try:
            folder_name = self._find_nzb_folder(job_norm, original_name=job_name)
            if not folder_name:
                return None
            from debrid.common import filter_unwanted_video_files
            filtered_files = filter_unwanted_video_files(self._list_nzb_folder_files(folder_name))
            video_files = sorted([name for name, _ in filtered_files])
            logging.info(f'[NzbDAV] get_nzb_folder_all_files: folder={folder_name!r} files={video_files}')
            return folder_name, video_files
        except Exception as exc:
            logging.warning(f'[NzbDAV] get_nzb_folder_all_files error for {job_name!r}: {exc}')
            return None

    # -- queue / removal ----------------------------------------------------

    @staticmethod
    def _release_name_key(s: str) -> str:
        """Normalise a release/folder/file name for EXACT matching.

        Drops a trailing video extension (so a filled_by_file like
        `X-GROUP.mkv` matches the history folder `X-GROUP`), lowercases, and
        strips every non-alphanumeric character. Callers compare the FULL key for
        equality — never as a substring — so quality/codec/group tokens
        (1080p vs 2160p, x264 vs x265) keep distinct releases apart.
        """
        s = os.path.basename(str(s or ''))
        root, ext = os.path.splitext(s)
        if ext.lower() in _VIDEO_EXTS:
            s = root
        return re.sub(r'[^a-z0-9]', '', s.lower())

    def _raw_delete_by_id(self, info_hash: str) -> bool:
        """Delete one history entry by nzo_id via the SABnzbd GET convention.

          GET /api?mode=history&name=delete&value=<nzo_id>&del_files=1
        An HTTP DELETE to the same URL returns 200 but does NOT delete anything
        (the entry silently survives) — so always use GET. Returns True only on
        confirmed removal (or 404 already-gone); False otherwise (incl.
        status=false, meaning the id was not present).
        """
        try:
            r = api.get(
                self._sab_url(),
                params=self._sab_params(mode='history', name='delete', value=info_hash, del_files=1),
                timeout=15,
            )
            if r.status_code == 200:
                try:
                    ok = (r.json() or {}).get('status', True)
                except Exception:
                    ok = True
                if ok:
                    logging.info(f'[NzbDAV] Removed NZB job {info_hash}')
                    return True
                logging.info(f'[NzbDAV] delete-by-id status=false for {info_hash} (id not present)')
                return False
            if r.status_code == 404:
                logging.info(f'[NzbDAV] NZB job {info_hash} already gone (404)')
                return True
            logging.info(f'[NzbDAV] delete-by-id HTTP {r.status_code} for {info_hash}: {r.text[:200]}')
            return False
        except Exception as exc:
            logging.debug(f'[NzbDAV] _raw_delete_by_id error: {exc}')
            return False

    def _id_in_history(self, info_hash: str) -> bool:
        """True iff `info_hash` is a real nzo_id currently in nzbdav's history."""
        target = str(info_hash)
        for s in self._history_slots():
            if str(s.get('nzo_id', '')) == target:
                return True
        return False

    def _delete_by_exact_name(self, entry_name: str) -> bool:
        """Remove a history entry matched by EXACT, full normalised name.

        Last-resort fallback when the stored job-id is not a live nzbdav nzo_id
        (e.g. an item migrated between providers via provider_transfer, whose
        filled_by_torrent_id still holds the old provider's id). Matching is on
        the *whole* normalised name, never a substring: a 1080p release must
        never resolve to its 2160p sibling. If the name is ambiguous (more than
        one history entry normalises equal) we REFUSE rather than guess.
        """
        key = self._release_name_key(entry_name)
        if not key:
            return False
        matches = []
        for s in self._history_slots():
            nm = s.get('name') or s.get('nzb_name') or ''
            if self._release_name_key(nm) == key:
                nzo = str(s.get('nzo_id') or '')
                if nzo:
                    matches.append(nzo)
        matches = list(dict.fromkeys(matches))  # dedup, keep order
        if len(matches) == 1:
            logging.info(f'[NzbDAV] exact-name fallback matched {entry_name!r} -> {matches[0]}')
            return self._raw_delete_by_id(matches[0])
        if not matches:
            logging.warning(f'[NzbDAV] exact-name fallback: no history entry matches {entry_name!r}')
            return False
        logging.warning(
            f'[NzbDAV] exact-name fallback: {len(matches)} history entries match '
            f'{entry_name!r}; refusing to guess which to delete'
        )
        return False

    def remove_nzb(self, info_hash: str, entry_name: str = '') -> bool:
        """Delete a library item's NZB from nzbdav.

        Primary path: delete by nzo_id (info_hash). If the id is not a live
        nzbdav id — which happens for items migrated from another provider via
        provider_transfer, whose filled_by_torrent_id still holds the old
        provider's id — fall back to an EXACT-name match (see
        _delete_by_exact_name). Without this, a library delete leaves the file in
        nzbdav and Plex re-imports it, writing the item straight back into the DB
        (delete-from-library doesn't stick).

        Returns True if removed (or confirmed already gone), False otherwise.
        """
        if not self.is_enabled():
            return False
        if info_hash and self._raw_delete_by_id(info_hash):
            return True
        # Delete-by-id didn't confirm. Distinguish "id genuinely absent" (safe to
        # try a name fallback) from "id present but delete errored" (a real
        # failure — must NOT start guessing by name).
        if info_hash and self._id_in_history(info_hash):
            logging.warning(
                f'[NzbDAV] job {info_hash} is in history but could not be deleted — '
                'treating as failure (not guessing by name)'
            )
            return False
        if entry_name:
            return self._delete_by_exact_name(entry_name)
        logging.info(f'[NzbDAV] remove_nzb: id {info_hash!r} absent and no name to match')
        return False

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Poll nzbdav for a single job's state.

        Strategy:
          1. Check mode=queue — if found, returns state=downloading/queued
          2. If not in queue, check mode=history — if found, completed
          3. Otherwise unknown (likely deleted/expired)
        Returns dict matching CliMountClient.get_job_status shape.
        """
        try:
            # Queue first
            r = api.get(self._sab_url(), params=self._sab_params(mode='queue'), timeout=10)
            if r.status_code == 200:
                q = r.json().get('queue', {}) if isinstance(r.json(), dict) else {}
                for slot in q.get('slots', []) or []:
                    if str(slot.get('nzo_id', '')) == job_id:
                        state = str(slot.get('status', '')).lower()
                        # nzbdav uses values like Downloading, Queued, Paused, Completed
                        try:
                            progress = int(float(slot.get('percentage', 0)))
                        except (ValueError, TypeError):
                            progress = 0
                        return {
                            'state': _map_state(state),
                            'progress': progress,
                            'name': slot.get('filename', ''),
                            'raw': slot,
                        }

            # History fallback
            r = api.get(
                self._sab_url(),
                params=self._sab_params(mode='history', limit=500),
                timeout=10,
            )
            if r.status_code == 200:
                h = r.json().get('history', {}) if isinstance(r.json(), dict) else {}
                for slot in h.get('slots', []) or []:
                    if str(slot.get('nzo_id', '')) == job_id:
                        state = str(slot.get('status', '')).lower()
                        return {
                            'state': _map_state(state),
                            'progress': 100 if state == 'completed' else 0,
                            'name': slot.get('name', ''),
                            'raw': slot,
                        }

            # Not in queue or history — assume completed-and-cleaned
            return {'state': 'completed', 'progress': 100, 'raw': {}}
        except Exception as exc:
            logging.debug(f'[NzbDAV] get_job_status exception: {exc}')
            return None

    # -- health-check stubs --------------------------------------------------
    # nzbdav has internal auto-health-check (HealthCheckService); there is no
    # user-callable trigger/poll endpoint. We return success/None so callers
    # don't error out, but the actual repair is driven by nzbdav itself.

    def trigger_health_check(self, entry_name: str) -> bool:
        """No-op for nzbdav — internal HealthCheckService runs automatically.

        Returns True so callers continue with their normal flow.
        """
        logging.debug(f'[NzbDAV] trigger_health_check no-op for {entry_name!r} '
                      '(nzbdav health checks run internally)')
        return True

    def poll_health_result(self, entry_name: str) -> Optional[str]:
        """No-op for nzbdav — returns None so callers treat as 'not ready'."""
        return None

    def check_entry_health(self, entry_name: str) -> Optional[str]:
        """Legacy wrapper — returns None for nzbdav (no user-driven health-API)."""
        return None

    # -- repair-support (history-based) -------------------------------------
    # nzbdav has NO /api/repair endpoint. The only failure signal it exposes is
    # history slots with status='Failed' (download-time NNTP failures, or items
    # cli-debrid marked Collected that never actually landed). The repair engine
    # treats these as 'broken' entries: orphans get purged from history, items
    # still mapped to a live DB row get re-scraped. There is no rot/health-scan
    # to trigger — failures are already live in history.

    def _history_slots(self, limit: int = 1000) -> List[dict]:
        try:
            r = api.get(self._sab_url(), params=self._sab_params(mode='history', limit=limit), timeout=30)
            if r.status_code != 200:
                return []
            data = r.json()
            return (data.get('history', {}) or {}).get('slots', []) or []
        except Exception as exc:
            logging.debug(f'[NzbDAV] _history_slots error: {exc}')
            return []

    def fetch_broken_items(self) -> list:
        """Return failed history entries as repair-engine entry dicts."""
        if not self.is_enabled():
            return []
        broken = []
        for s in self._history_slots():
            if str(s.get('status', '')).lower() != 'failed':
                continue
            if str(s.get('category', '')).lower() not in self.owned_categories:
                continue  # shared provider — skip other apps' entries (e.g. Lidarr music)
            name = s.get('name') or (s.get('nzb_name') or '').rsplit('.nzb', 1)[0]
            broken.append({
                'entry_name': name,
                'name': name,
                'info_hash': s.get('nzo_id', ''),
                # nzo_id is exactly the value cli-debrid stores as
                # filled_by_torrent_id ('nzb:'+nzo_id) — so the hash is an
                # authoritative ownership test. repair skips its fuzzy
                # title-match fallback when this is set (avoids false positives).
                'hash_is_authoritative': True,
                'status': 'broken',
                'nzb_url': '',
                'fail_message': s.get('fail_message', ''),
                'broken_files': [],
            })
        logging.info(f'[NzbDAV] fetch_broken_items: {len(broken)} failed history entr(ies)')
        return broken

    def get_health_summary(self) -> dict:
        """Counts by health state derived from history (Failed→broken, Completed→healthy)."""
        if not self.is_enabled():
            return {}
        counts: Dict[str, int] = {}
        for s in self._history_slots():
            if str(s.get('category', '')).lower() not in self.owned_categories:
                continue  # shared provider — only count cli-debrid's own categories
            st = str(s.get('status', 'unknown')).lower()
            key = 'broken' if st == 'failed' else ('healthy' if st == 'completed' else st)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def trigger_health_scan(self) -> bool:
        """No-op: nzbdav runs health checks internally; failures already live in history."""
        logging.debug('[NzbDAV] trigger_health_scan no-op (history is the live failure source)')
        return True

    def resolve_job_id(self, entry_name: str) -> str:
        """Resolve an nzo_id from history by release name (repair fallback)."""
        if not self.is_enabled() or not entry_name:
            return ''
        target = entry_name.strip()
        for s in self._history_slots():
            if (s.get('name') or '').strip() == target:
                return s.get('nzo_id', '')
        return ''

    def wait_for_completion(self, job_id: str, timeout: int = 3600, poll_interval: int = 10) -> bool:
        """Poll until the job completes or timeout."""
        if job_id in ('submitted', ''):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_job_status(job_id)
            if not status:
                time.sleep(poll_interval)
                continue
            state = status.get('state', 'unknown')
            if state == 'completed':
                logging.info(f'[NzbDAV] Job {job_id} completed')
                return True
            if state == 'failed':
                logging.error(f'[NzbDAV] Job {job_id} failed')
                return False
            logging.debug(f'[NzbDAV] Job {job_id} state={state} progress={status.get("progress", 0)}%')
            time.sleep(poll_interval)
        logging.warning(f'[NzbDAV] Job {job_id} timed out after {timeout}s')
        return False


def _map_state(raw: str) -> str:
    """Map nzbdav SAB-API status values to cli-debrid's canonical states."""
    raw = raw.lower()
    if raw in ('completed', 'downloaded', 'done', 'finished'):
        return 'completed'
    if raw in ('failed', 'error'):
        return 'failed'
    if raw in ('downloading', 'queued', 'processing', 'active', 'paused'):
        return 'downloading'
    return 'unknown'


_client_instance: Optional[NzbdavClient] = None


def get_nzbdav_client() -> NzbdavClient:
    """Return the singleton NzbdavClient instance."""
    global _client_instance
    if _client_instance is None:
        _client_instance = NzbdavClient()
    return _client_instance


def reset_nzbdav_client() -> None:
    """Force re-creation of the singleton on next access (call after config change)."""
    global _client_instance
    _client_instance = None
