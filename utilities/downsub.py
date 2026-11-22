#!/usr/bin/env python3
import os
import sys
import json
import logging
import subprocess
import re
import time
from pathlib import Path

# Handle both relative and absolute imports
try:
    from .config.downsub_config import config
except ImportError:
    # Add the current directory to the Python path for absolute imports
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config.downsub_config import config

# Import subliminal components
SUBLIMINAL_AVAILABLE = False
try:
    from subliminal import download_best_subtitles, region
    from subliminal.video import Video, Episode, Movie
    from babelfish import Language
    SUBLIMINAL_AVAILABLE = True
except Exception as e:
    logging.warning(f"Subliminal packages not available: {e}")
    logging.warning("Subtitle downloading will be disabled. Install with: pip install subliminal babelfish")

# Logging configuration
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler()
    ]
)

# Language mapping from config codes to babelfish Language objects
# Only populated when babelfish is available
LANGUAGE_MAP = {}
if SUBLIMINAL_AVAILABLE:
    LANGUAGE_MAP = {
        'ara': Language('ara'),
        'eng': Language('eng'),
        'fre': Language('fra'),
        'fra': Language('fra'),  # Add fra mapping for consistency
        'ger': Language('deu'),
        'spa': Language('spa'),
        'ita': Language('ita'),
        'por': Language('por'),         # generic (keep if you want EU-PT too)
        'pt-BR': Language.fromietf('pt-BR'),
        'pob': Language('por', 'BR'),   # OpenSubtitles legacy code
        'pb': Language('por', 'BR'),    # common alias
        'dut': Language('nld'),
        'rus': Language('rus'),
        'chi': Language('zho'),
        'zho': Language('zho'),  # Alternative code for Chinese
        'jpn': Language('jpn'),
        'kor': Language('kor'),
    }

def expand_languages(codes):
    """Turn config codes into babelfish Languages, expanding 'por' to BR/PT."""
    out = []
    for code in codes:
        c = code.strip()
        # support IETF like 'pt-BR'
        try:
            if '-' in c:
                out.append(Language.fromietf(c))
                continue
        except Exception:
            pass
        if c in ('por', 'pt'):
            out.extend([Language('por'), Language('por','BR'), Language('por','PT')])
            continue
        if c in LANGUAGE_MAP:
            out.append(LANGUAGE_MAP[c])
            continue
        # last resort: try direct
        try:
            out.append(Language(c))
        except Exception as e:
            logging.warning(f"⚠️ Unknown language code: {c} - {e}")
    return out

#: Providers that work without a captcha. Order is only a tie-break: every
#: selected provider is queried and the best-scoring subtitle wins.
KNOWN_PROVIDERS = ('opensubtitlescom', 'podnapisi', 'gestdown', 'tvsubtitles',
                   'subtitulamos', 'bsplayer', 'opensubtitles')
DEFAULT_PROVIDERS = ['opensubtitlescom', 'podnapisi', 'gestdown', 'tvsubtitles']

#: Subtitle codecs every Plex/Jellyfin/Emby client can draw without the server
#: burning them into the video. Styled ASS/SSA and image subs (PGS, VobSub,
#: DVB) force a transcode on most clients.
TEXT_SUBTITLE_CODECS = {'subrip', 'srt', 'mov_text', 'webvtt', 'text'}

#: Minimum identity match. A subtitle below this is for a different episode
#: or movie and is never worth downloading: series+season+episode (episodes)
#: or title+year (movies). A hash match alone clears both.
MIN_EPISODE_SCORE = 486 + 54 + 54
MIN_MOVIE_SCORE = 162 + 54
MIN_MOVIE_SCORE_NO_YEAR = 162


# provider -> (monotonic until, reason). A provider that hit its download
# quota or failed its login is skipped until then, so a bulk run doesn't ask
# it again for every item.
_BLOCKED = {}


def _block_seconds(exc):
    name = type(exc).__name__ if exc else ''
    if name in ('DownloadLimitExceeded', 'DownloadLimitReached'):
        return 6 * 3600, 'daily download limit reached'
    if name in ('AuthenticationError', 'ConfigurationError'):
        return 3600, 'login failed (check the OpenSubtitles username/password)'
    if name == 'ServiceUnavailable':
        return 600, 'service unavailable'
    return 600, f'error ({name or "unknown"})'


def blocked_providers():
    now = time.monotonic()
    for k in [k for k, v in _BLOCKED.items() if v[0] <= now]:
        _BLOCKED.pop(k, None)
    return {k: v[1] for k, v in _BLOCKED.items()}


def _pool_class():
    """ProviderPool that remembers why a provider was discarded."""
    from subliminal.core import ProviderPool

    class _TrackingPool(ProviderPool):
        def _discard_provider(self, name):
            super()._discard_provider(name)
            exc = sys.exc_info()[1]
            secs, reason = _block_seconds(exc)
            if name not in _BLOCKED:
                logging.warning(f"[subs] pausing provider {name} for {secs // 60} min: {reason}")
            _BLOCKED[name] = (time.monotonic() + secs, reason)
    return _TrackingPool


def selected_providers(include_blocked=False):
    """The providers from settings that this subliminal knows, in order,
    minus any paused after a quota/login error."""
    try:
        from subliminal.extensions import provider_manager
        available = set(provider_manager.names())
    except Exception:
        available = set(KNOWN_PROVIDERS)
    chosen = config.SUBTITLE_PROVIDERS or DEFAULT_PROVIDERS
    if isinstance(chosen, str):
        chosen = [c.strip() for c in chosen.split(',')]
    out = [p for p in chosen if p in available]
    if not out:
        out = [p for p in DEFAULT_PROVIDERS if p in available]
    if include_blocked:
        return out
    blocked = blocked_providers()
    return [p for p in out if p not in blocked]


def build_provider_configs():
    """Credentials per provider. One OpenSubtitles account works for both the
    .com API (required to download there) and the legacy .org provider."""
    pc = {}
    user = config.OPENSUBTITLES_USERNAME
    pwd = config.OPENSUBTITLES_PASSWORD
    if user and pwd:
        pc['opensubtitles'] = {'username': user, 'password': pwd}
        pc['opensubtitlescom'] = {'username': user, 'password': pwd}
    return pc


def get_embedded_subtitle_languages(file_path: Path, text_only=None) -> set:
    """
    Use ffprobe to detect embedded subtitle track languages in a video file.
    Returns a set of ISO-639-2/3 language codes (lowercase) found as subtitle streams.
    Returns empty set if ffprobe is unavailable, times out, or the file is inaccessible.

    Forced tracks (foreign-dialogue only) never count. With text_only (default:
    the 'text_subtitles_only' setting) styled/image tracks don't count either,
    so an SRT is still fetched for a language that only has a PGS/ASS track.
    """
    if text_only is None:
        text_only = bool(getattr(config, 'TEXT_SUBTITLES_ONLY', False))
    tracks = _embedded_tracks(file_path)
    if tracks is None:
        return set()
    langs = set()
    for t in tracks:
        if t.get('forced'):
            continue
        if text_only and t.get('codec') not in TEXT_SUBTITLE_CODECS:
            continue
        lang = t.get('language') or ''
        if lang and lang not in ('und', 'unknown'):
            langs.add(lang)
    return langs


def _embedded_tracks(file_path):
    """Subtitle tracks of a file. Reuses the add-time playability ffprobe
    (one header read records the tracks) and only runs ffprobe itself when
    that check didn't see this file. None when the probe failed."""
    try:
        from usenet.repair_engine import cached_subtitle_tracks
        cached = cached_subtitle_tracks(str(file_path))
        if cached is not None:
            logging.debug(f"[subs] reusing add-time ffprobe tracks for {file_path}")
            return cached
    except Exception:
        pass
    try:
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error',
                '-select_streams', 's',
                '-show_entries', 'stream=codec_name:stream_tags=language:stream_disposition=forced',
                '-of', 'json',
                str(file_path),
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
        return [{'codec': (st.get('codec_name') or '').lower(),
                 'language': ((st.get('tags') or {}).get('language') or '').strip().lower(),
                 'forced': bool((st.get('disposition') or {}).get('forced'))}
                for st in data.get('streams', [])]
    except FileNotFoundError:
        logging.warning('ffprobe not found — skipping embedded subtitle probe')
        return None
    except subprocess.TimeoutExpired:
        logging.warning(f'ffprobe timed out probing {file_path} — skipping embedded subtitle probe')
        return None
    except Exception as e:
        logging.warning(f'ffprobe probe failed for {file_path}: {e}')
        return None


_EP_RE = re.compile(r'[Ss]\d{1,2}[ ._-]?[Ee]\d{1,3}|\b\d{1,2}x\d{2,3}\b')


def _looks_obfuscated(name):
    """True for names that carry no release info, e.g. 'tGcr.mkv' or a hash."""
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    if not stem:
        return True
    if re.fullmatch(r'[0-9a-fA-F]{16,}', stem):
        return True
    return not re.search(r'[ ._\-]', stem) and len(stem) < 25


def _name_candidates(original_path, real_path, name_hint, item):
    """Names to parse, best first: release names before the templated
    library name, which has the title but no group/source."""
    item = item or {}
    raw = [name_hint, item.get('filled_by_title'), item.get('original_scraped_torrent_title'),
           item.get('original_filename'),
           item.get('filled_by_file'), real_path.name if real_path else None,
           item.get('debrid_folder_name'), original_path.name]
    out = []
    for n in raw:
        n = (n or '').strip()
        if n and n not in out and not _looks_obfuscated(n):
            out.append(n)
    return out or [original_path.name]


def _build_video(original_path, real_path, name_hint, item):
    """Build the subliminal Video from the release name, then correct its
    identity with what the database already knows (IDs, title, S/E)."""
    item = item or {}
    is_episode = (item.get('type') == 'episode') if item.get('type') else None
    cands = _name_candidates(original_path, real_path, name_hint, item)
    if is_episode is None:
        is_episode = any(_EP_RE.search(c) for c in cands)
    # Episodes parse from the first name that names the episode; a season-pack
    # folder name ("Show S01 1080p ...") makes guessit fail.
    if is_episode:
        parse_name = next((c for c in cands if _EP_RE.search(c)), cands[0])
    else:
        parse_name = cands[0]

    video = None
    try:
        video = Video.fromname(parse_name)
    except Exception as e:
        logging.debug(f"[subs] could not parse {parse_name!r}: {e}")

    title = item.get('title')
    season = item.get('season_number')
    episode = item.get('episode_number')
    if is_episode and title and season is not None and episode is not None:
        if not isinstance(video, Episode):
            video = Episode(parse_name, title, int(season), int(episode))
    elif not is_episode and title:
        if not isinstance(video, Movie):
            video = Movie(parse_name, title, year=item.get('year') or None)
    if video is None:
        return None, parse_name

    # Fill release details (group, source, service) the episode name lacks
    # from the other candidates, e.g. the season-pack folder name.
    attrs = ('release_group', 'source', 'streaming_service', 'resolution', 'video_codec')
    if any(getattr(video, a, None) is None for a in attrs):
        try:
            from guessit import guessit
            for c in cands:
                if c == parse_name:
                    continue
                g = guessit(c)
                for a, key in (('release_group', 'release_group'), ('source', 'source'),
                               ('streaming_service', 'streaming_service'),
                               ('resolution', 'screen_size'), ('video_codec', 'video_codec')):
                    if getattr(video, a, None) is None and g.get(key):
                        setattr(video, a, g.get(key))
        except Exception as e:
            logging.debug(f"[subs] release-detail fill failed: {e}")

    # Database identity beats parsed identity: obfuscated or renamed files
    # parse to the wrong show/episode, the DB never does.
    imdb = (item.get('imdb_id') or '').strip()
    tmdb = str(item.get('tmdb_id') or '').strip()
    tvdb = str(item.get('tvdb_id') or '').strip()
    if isinstance(video, Episode):
        if imdb.startswith('tt'):
            video.external_ids['series_imdb_id'] = imdb
        if tmdb.isdigit():
            video.external_ids['series_tmdb_id'] = tmdb
        if tvdb.isdigit():
            video.external_ids['series_tvdb_id'] = tvdb
        if title and video.series != title:
            if video.series:
                video.alternative_series.append(video.series)
            video.series = title
        if season is not None and episode is not None:
            video.season = int(season)
            video.episodes = [int(episode)]
    elif isinstance(video, Movie):
        if imdb.startswith('tt'):
            video.external_ids['imdb_id'] = imdb
        if tmdb.isdigit():
            video.external_ids['tmdb_id'] = tmdb
        if title and video.title != title:
            if video.title:
                video.alternative_titles.append(video.title)
            video.title = title
        if item.get('year') and not video.year:
            try:
                video.year = int(item['year'])
            except (TypeError, ValueError):
                pass
    return video, parse_name


def _compute_hash(path, timeout=15):
    """OpenSubtitles hash: file size + first and last 64 KB, so two small
    reads even on a debrid/usenet mount. Runs in a thread so a stuck mount
    can't hold up post-processing; None when the file can't be read."""
    import threading
    out = {}

    def _run():
        try:
            from subliminal.refiners.hash import hash_opensubtitles
            if os.path.isfile(path):
                out['size'] = os.path.getsize(path)
                out['hash'] = hash_opensubtitles(str(path))
        except Exception as e:
            out['err'] = e

    t = threading.Thread(target=_run, daemon=True, name='subs-hash')
    t.start()
    t.join(timeout)
    if t.is_alive():
        logging.info(f"[subs] hash read of {path} took over {timeout}s; matching by name only")
        return None, None
    if out.get('err'):
        logging.debug(f"[subs] hash failed for {path}: {out['err']}")
    return out.get('hash'), out.get('size')


def _min_score(video):
    if isinstance(video, Episode):
        return MIN_EPISODE_SCORE
    return MIN_MOVIE_SCORE if getattr(video, 'year', None) else MIN_MOVIE_SCORE_NO_YEAR


def _lookup_item(path):
    """Find the collected item for a path, for callers that only have one
    (manual buttons, bulk script)."""
    try:
        from database.core import get_db_connection
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT * FROM media_items WHERE state IN ('Collected','Upgrading') "
                "AND location_on_disk = ? LIMIT 1", (str(path),)).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM media_items WHERE state IN ('Collected','Upgrading') "
                    "AND filled_by_file = ? LIMIT 2", (os.path.basename(str(path)),)).fetchall()
                row = row[0] if len(row) == 1 else None
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logging.debug(f"[subs] item lookup for {path} failed: {e}")
        return None


def resolve_item_path(item):
    """Local path of an item's video file for the subtitle downloader.

    Symlinked/Local: location_on_disk is the library file. Plex mode: it's
    the folder (or file) as Plex sees it, often under /debrid/...; that
    prefix is mapped to the local mount so the file can be hashed.
    """
    item = item or {}
    location = item.get('location_on_disk') or ''
    fname = item.get('filled_by_file') or ''
    if not location:
        return None
    from utilities.settings import get_setting, load_config
    if get_setting('File Management', 'file_collection_management', 'Plex') != 'Plex':
        return location
    mount_base = ''
    try:
        cfg = load_config()
        data_path = (cfg.get('Usenet Provider', {}).get('data_path') or '').strip()
        if data_path:
            with open(os.path.join(data_path, 'config.json')) as f:
                mount_base = (json.load(f).get('mount', {}).get('mount_path') or '').rstrip('/')
    except Exception:
        mount_base = ''
    if not mount_base:
        mount_base = (get_setting('Plex', 'mounted_file_location', '') or '').strip()
        if mount_base.endswith('/__all__'):
            mount_base = mount_base[:-len('/__all__')]
        mount_base = mount_base.rstrip('/')
    if mount_base:
        parts = location.split('/', 3)
        if len(parts) >= 3 and parts[1] == 'debrid':
            location = mount_base + '/' + '/'.join(parts[2:])
    if fname and not location.endswith(fname):
        location = os.path.join(location, fname)
    return location


def upload_subtitle_to_plex(rating_key, content, lang_code, fmt='srt'):
    """Upload a subtitle to a Plex item via the Plex API (no filesystem write).

    Used when the media lives on a read-only mount (Plex mode over a debrid/
    usenet rclone/WebDAV mount), where a sidecar .srt cannot be written. This is
    storage-agnostic — keyed on the Plex ratingKey — so it works identically for
    zurg, nzbdav, climount and any future backend. Returns True on success.
    """
    import requests as _rq
    from utilities.settings import get_setting
    plex_url = (get_setting('Plex', 'url', '') or '').rstrip('/')
    plex_token = get_setting('Plex', 'token', '') or ''
    if not (plex_url and plex_token and rating_key):
        logging.warning(f"[Plex subs] missing url/token/ratingKey — cannot upload (rk={rating_key})")
        return False
    data = content if isinstance(content, bytes) else (content or '').encode('utf-8')
    if not data:
        return False
    try:
        url = f"{plex_url}/library/metadata/{rating_key}/subtitles"
        # Plex REJECTS state-changing POSTs with HTTP 500 unless an
        # X-Plex-Client-Identifier header is present. Language is detected by Plex
        # from the title's ".<lang>." token (the bare 'language' param alone is
        # unreliable), so encode it in the title too. Upload subtitles is a free
        # Plex feature (no Plex Pass required).
        params = {
            'title': f'subtitle.{lang_code}.{fmt}',
            'format': fmt,
            'language': lang_code,
            'X-Plex-Token': plex_token,
        }
        headers = {
            'Accept': 'text/plain, */*',
            'X-Plex-Client-Identifier': 'cli-debrid-subtitles',
            'X-Plex-Product': 'cli_debrid',
            'X-Plex-Version': '1.0',
        }
        r = _rq.post(url, params=params, data=data, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            logging.info(f"[Plex subs] uploaded {lang_code} subtitle to ratingKey {rating_key}")
            return True
        logging.warning(f"[Plex subs] upload failed rk={rating_key} lang={lang_code}: HTTP {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        logging.error(f"[Plex subs] upload error rk={rating_key}: {e}")
        return False


def _final_format(sub):
    """(format, extension) to save a downloaded subtitle as, or (None, None)
    to drop it. Saved under its real extension: an ASS file named .srt
    shows raw style tags. With 'text_subtitles_only' a styled ASS/SSA (or any
    non-SRT) subtitle is converted to plain SRT so clients direct-play it."""
    fmt = (getattr(sub, 'subtitle_format', None) or 'srt').lower()
    if fmt != 'srt' and getattr(config, 'TEXT_SUBTITLES_ONLY', False):
        try:
            ok = sub.convert(output_format='srt', output_encoding='utf-8')
        except Exception as e:
            logging.debug(f"[subs] convert to srt failed: {e}")
            ok = False
        if not ok:
            logging.info(f"[subs] dropped {fmt} subtitle {sub!r}: could not convert to SRT (text-only mode)")
            return None, None
        logging.info(f"[subs] converted {fmt} subtitle to SRT (text-only mode)")
        fmt = 'srt'
    try:
        from subliminal.subtitle import FORMAT_TO_EXTENSION
        ext = FORMAT_TO_EXTENSION.get(fmt, '.srt')
    except Exception:
        ext = '.srt'
    return fmt, ext


def prepare_video(video_path, rating_key=None, name_hint=None, item=None, check_embedded=True):
    """Resolve the file, item, languages and subliminal Video for a search.

    Returns None when the file can't be identified, {'all_embedded': True}
    when every configured language is already an embedded track, else a
    context dict for search/save.
    """
    _probe = config.PROBE_FOR_EMBEDDED
    if not check_embedded:
        config.PROBE_FOR_EMBEDDED = False
    try:
        return _prepare_video(video_path, rating_key, name_hint, item)
    finally:
        config.PROBE_FOR_EMBEDDED = _probe


def _prepare_video(video_path, rating_key, name_hint, item):
    video_path = Path(video_path)

    from utilities.settings import get_setting
    is_plex_mode = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'

    # Check if file exists — skip for Plex mode (file is on FUSE mount, not local)
    if not is_plex_mode and not video_path.exists():
        logging.error(f"❌ File does not exist: {video_path}")
        return None

    # Check if it's a valid video file
    if not str(video_path).lower().endswith(config.VIDEO_EXTENSIONS):
        logging.error(f"❌ Not a valid video file: {video_path}")
        return None

    # Handle symlinks - keep track of both paths (skip in Plex mode)
    original_path = video_path
    real_path = None
    if not is_plex_mode and video_path.is_symlink():
        real_path = video_path.resolve()
        logging.info(f"🔗 Processing symlink: {video_path} -> {real_path}")
        video_path = real_path

    if item is None:
        item = _lookup_item(original_path)
    if item and not rating_key and is_plex_mode and item.get('ms_item_id'):
        rating_key = str(item['ms_item_id'])

    # Convert language codes to Language objects using expand_languages
    languages = expand_languages(config.SUBTITLE_LANGUAGES)

    if not languages:
        logging.error("❌ No valid languages configured")
        return None

    # Probe for embedded subtitles if enabled. Plex mode only uses the
    # add-time playability probe's result (no fresh read of the mount).
    _plex_cached = None
    if config.PROBE_FOR_EMBEDDED and is_plex_mode:
        try:
            from usenet.repair_engine import cached_subtitle_tracks
            _plex_cached = cached_subtitle_tracks(str(video_path))
        except Exception:
            _plex_cached = None
    if config.PROBE_FOR_EMBEDDED and (not is_plex_mode or _plex_cached is not None):
        embedded = get_embedded_subtitle_languages(video_path)
        if embedded:
            logging.info(f"🔍 Embedded subtitle languages detected: {embedded}")
            # Filter out languages already covered by an embedded track.
            # babelfish Language alpha3 (e.g. 'eng', 'fra') is compared against
            # ffprobe tags which are typically ISO-639-2 (also 3-letter).
            filtered = [
                lang for lang in languages
                if lang.alpha3 not in embedded and str(lang) not in embedded
            ]
            if not filtered:
                logging.info("✅ All configured languages already embedded — skipping external search")
                return {'all_embedded': True, 'item': item}
            removed = len(languages) - len(filtered)
            if removed:
                logging.info(f"ℹ️  Skipping {removed} language(s) already embedded; searching for remaining {len(filtered)}")
            languages = filtered

    # Configure in-memory cache for faster performance (only if not already configured)
    try:
        region.configure('dogpile.cache.memory', replace_existing_backend=True)
    except Exception:
        # Region already configured or other cache setup issue, which is fine
        pass

    video, parse_name = _build_video(original_path, real_path, name_hint, item)
    if video is None:
        logging.warning(f"❌ Could not identify {original_path.name} (parsed as {parse_name!r})")
        return None
    video.path = original_path  # subtitles are saved alongside the library file

    # Hash the real file: a hash match is the exact release, so in sync.
    file_hash, file_size = _compute_hash(video_path)
    if file_hash:
        video.size = file_size
        for key in ('opensubtitles', 'opensubtitlescom', 'opensubtitlesvip',
                    'opensubtitlescomvip', 'bsplayer'):
            video.hashes[key] = file_hash

    logging.info(f"🎬 Processing: {original_path.name} (parsing as: {parse_name}; "
                 f"group={video.release_group}, source={video.source}, "
                 f"ids={dict(video.external_ids)}, hash={'yes' if file_hash else 'no'})")
    return {'all_embedded': False, 'video': video, 'parse_name': parse_name,
            'languages': languages, 'original_path': original_path,
            'video_path': video_path, 'real_path': real_path, 'item': item,
            'rating_key': rating_key, 'is_plex_mode': is_plex_mode,
            'min_score': _min_score(video), 'hash': bool(file_hash)}


def _record_history(ctx, sub, lang, fmt, via, path, source):
    try:
        from subliminal.score import compute_score, get_scores
        from database import subtitle_history
        video = ctx['video']
        item = ctx.get('item') or {}
        try:
            score = int(compute_score(sub, video))
            matches = sub.get_matches(video)
        except Exception:
            score, matches = None, []
        title = item.get('title') or ctx['original_path'].stem
        if item.get('type') == 'episode' and item.get('season_number') is not None:
            title = f"{title} S{int(item['season_number']):02d}E{int(item.get('episode_number') or 0):02d}"
        subtitle_history.record(
            item.get('id'), title, lang, sub.provider_name, score, get_scores(video)['hash'],
            matches, _release_of(sub), fmt, via, path, source, sync=ctx.get('_sync_note'))
    except Exception as e:
        logging.debug(f"[subs] history record failed: {e}")


def _maybe_sync(ctx, sub, content, fmt, mode=None):
    """Sync a downloaded subtitle to the file's audio when the setting asks
    for it. Returns (content, note); content is unchanged unless synced."""
    mode = mode or getattr(config, 'SYNC_SUBTITLES', 'off')
    if mode not in ('unless_hash', 'always'):
        return content, None
    try:
        matches = sub.get_matches(ctx['video'])
    except Exception:
        matches = set()
    if mode == 'unless_hash' and 'hash' in matches:
        return content, 'skipped: hash match'
    return sync_content(ctx['video_path'], content, fmt)


def sync_content(video_path, content, fmt):
    """Run the sampled-audio sync; (content, note) with content replaced
    only when the sync found a clearly better timing."""
    try:
        from utilities import subtitle_sync
        t0 = time.time()
        new, info = subtitle_sync.sync_subtitle(
            str(video_path), content, fmt or 'srt',
            max_offset=float(getattr(config, 'SYNC_MAX_OFFSET', 60) or 60))
        note = subtitle_sync.describe(info)
        logging.info(f"[subsync] {os.path.basename(str(video_path))}: {note} ({time.time() - t0:.1f}s)")
        return (new or content), note
    except Exception as e:
        logging.warning(f"[subsync] failed for {video_path}: {e}")
        return content, f'not synced: {e}'


def save_subtitles(ctx, subs, source='auto'):
    """Save downloaded subtitles next to the file (sidecar), to Plex (read-only
    mount) or through cli_mount, and record each in the subtitle history.
    Returns the number saved."""
    original_path = ctx['original_path']
    rating_key = ctx['rating_key']
    video = ctx['video']
    subtitles = {video: list(subs)}
    symlink_dir = original_path.parent
    base_name = original_path.stem
    # Save mode is decided by WRITABILITY, not by provider:
    #   - media dir writable (symlink/local mode, Jellyfin/Emby, …)
    #       → write a sidecar .srt next to the file (media-server-neutral).
    #   - media dir NOT writable (read-only debrid/usenet mount in Plex mode)
    #       → upload to Plex via API keyed on rating_key (storage-agnostic;
    #         works the same for zurg/nzbdav/climount/future backends).
    can_write_sidecar = os.access(str(symlink_dir), os.W_OK)
    use_plex_upload = (not can_write_sidecar) and bool(rating_key)
    saved = 0
    for sub in subtitles[video]:
        lang = getattr(sub.language, 'ietf', None) or str(sub.language)
        fmt, ext = _final_format(sub)
        if fmt is None:
            continue
        content = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
        if not content:
            continue
        content, sync_note = _maybe_sync(ctx, sub, content, fmt)
        ctx['_sync_note'] = sync_note
        if use_plex_upload:
            lang3 = getattr(sub.language, 'alpha3', None) or lang
            if upload_subtitle_to_plex(rating_key, content, lang3, fmt=fmt):
                saved += 1
                _record_history(ctx, sub, lang, fmt, 'plex', None, source)
        elif can_write_sidecar:
            symlink_srt = symlink_dir / f"{base_name}.{lang}{ext}"
            try:
                with open(symlink_srt, 'wb') as f:
                    f.write(content)
                logging.info(f"Saved subtitle: {symlink_srt}")
                saved += 1
                _record_history(ctx, sub, lang, fmt, 'sidecar', str(symlink_srt), source)
            except Exception as e:
                logging.error(f"Failed to save subtitle {symlink_srt}: {e}")
        else:
            # The built-in mount takes subtitles as ordinary file
            # writes into job folders, so an unwritable folder there
            # is a real error: cli_mount's API won't help.
            try:
                from utilities import builtin_engine as _be
                if _be.selected():
                    logging.warning(f"[subs] {base_name}: {symlink_dir} is not writable "
                                    "(built-in mount: is the engine running?); subtitle not saved")
                    continue
            except Exception:
                pass
            # Fallback: cli_mount sidecar injection
            try:
                from usenet.climount_client import get_climount_client
                # cli_mount's inject API uses the job UUID (info_hash) as the folder
                # identifier, not the human-readable folder title. Look it up from
                # the DB via filled_by_file or location_on_disk matching.
                torrent_name = symlink_dir.name  # fallback
                try:
                    from database.core import get_db_connection as _get_dbc
                    _fname = original_path.name
                    _loc = str(original_path)
                    _conn = _get_dbc()
                    _row = _conn.execute(
                        "SELECT filled_by_torrent_id FROM media_items "
                        "WHERE state='Collected' AND ("
                        "  filled_by_file=? OR location_on_disk=? OR location_on_disk LIKE ?"
                        ") AND filled_by_torrent_id LIKE 'nzb:%' LIMIT 1",
                        (_fname, _loc, f'%{_fname}')
                    ).fetchone()
                    _conn.close()
                    if _row and _row[0]:
                        torrent_name = _row[0].replace('nzb:', '', 1)
                        logging.debug(f"[subs] Using job UUID for sidecar injection: {torrent_name}")
                except Exception as _dbl:
                    logging.debug(f"[subs] Could not resolve job UUID from DB: {_dbl}")
                srt_filename = f"{base_name}.{lang}{ext}"
                client = get_climount_client()
                if client.inject_sidecar_file(torrent_name, srt_filename, content):
                    logging.info(f"[subs] Injected sidecar subtitle via cli_mount: {torrent_name}/{srt_filename}")
                    saved += 1
                    _record_history(ctx, sub, lang, fmt, 'cli_mount', f"{torrent_name}/{srt_filename}", source)
                else:
                    logging.warning(f"[subs] {base_name}: cli_mount sidecar injection failed for {lang}")
            except Exception as _dce:
                logging.warning(f"[subs] {base_name}: media dir not writable, no Plex ratingKey, cli_mount injection failed: {_dce}")
    logging.info(f"💾 Saved/uploaded {saved}/{len(subtitles[video])} subtitle(s) for {base_name} "
                 f"({'Plex API' if use_plex_upload else 'sidecar' if can_write_sidecar else 'cli_mount sidecar'})")
    return saved


def download_subtitles_for_video(video_path, rating_key=None, name_hint=None, item=None):
    """
    Download subtitles for a single video file.

    Matching uses, best first: an OpenSubtitles file hash (exact release, so
    already in sync), the database IDs/title/season/episode, and the release
    name (group, source, streaming service). Results below an identity score
    (wrong episode/movie) are never downloaded.

    Args:
        video_path (str): Path to the video file
        rating_key (str, optional): Plex ratingKey. When the media directory is
            NOT writable (read-only mount), subtitles are uploaded to this Plex
            item via API instead of being written as a sidecar.
        name_hint (str, optional): descriptive release name when the on-disk
            filename is obfuscated.
        item (dict, optional): the media_items row; looked up by path if absent.

    Returns:
        bool: True if any subtitles were downloaded/uploaded successfully
    """
    try:
        ctx = prepare_video(video_path, rating_key=rating_key, name_hint=name_hint, item=item)
        if ctx is None:
            return False
        if ctx.get('all_embedded'):
            _record_attempt((ctx.get('item') or {}).get('id'), 'embedded')
            return True
        video = ctx['video']
        providers = selected_providers()
        logging.info(f"🔍 Searching {providers} for "
                     f"{[getattr(lang, 'ietf', None) or str(lang) for lang in ctx['languages']]} "
                     f"(min score {ctx['min_score']})")
        start_time = time.time()
        if not providers:
            logging.warning(f"❌ No subtitle provider available (paused: {blocked_providers()})")
            return False
        subtitles = download_best_subtitles(
            {video}, set(ctx['languages']), min_score=ctx['min_score'], providers=providers,
            provider_configs=build_provider_configs(), pool_class=_pool_class())
        logging.info(f"⏱️ Subtitle search took {time.time() - start_time:.2f} seconds")
        item_id = (ctx.get('item') or {}).get('id')
        if not subtitles[video]:
            logging.warning("❌ No subtitles found")
            _record_attempt(item_id, False)
            return False
        logging.info(f"✅ Found {len(subtitles[video])} subtitle(s): {subtitles[video]}")
        ok = save_subtitles(ctx, subtitles[video]) > 0
        _record_attempt(item_id, ok)
        return ok
    except Exception as e:
        logging.error(f"❌ Error downloading subtitles: {e}")
        return False


def _record_attempt(item_id, found):
    try:
        from database import subtitle_history
        subtitle_history.record_attempt(item_id, found)
    except Exception:
        pass


def _release_of(sub):
    for attr in ('release_name', 'release', 'releases', 'movie_release_name',
                 'filename', 'file_name', 'name'):
        rel = getattr(sub, attr, None)
        if rel:
            if isinstance(rel, (list, tuple, set)):
                rel = ', '.join(str(r) for r in rel)
            return str(rel)
    return ''


def search_candidates(ctx, languages=None):
    """Every candidate from every selected provider, scored, best first:
    [{'sub', 'score', 'matches', 'provider', 'release', 'language', 'hi'}]."""
    from subliminal.score import compute_score
    video = ctx['video']
    langs = set(languages or ctx['languages'])
    out = []
    with _pool_class()(providers=selected_providers(),
                       provider_configs=build_provider_configs()) as pool:
        for sub in pool.list_subtitles(video, langs):
            try:
                score = int(compute_score(sub, video))
                matches = sorted(sub.get_matches(video))
            except Exception:
                score, matches = 0, []
            out.append({'sub': sub, 'score': score, 'matches': matches,
                        'provider': sub.provider_name, 'release': _release_of(sub),
                        'language': getattr(sub.language, 'ietf', None) or str(sub.language),
                        'hi': bool(getattr(sub, 'hearing_impaired', False))})
    out.sort(key=lambda c: c['score'], reverse=True)
    return out


def download_candidate(ctx, sub):
    """Download one chosen candidate and save it. Returns subtitles saved."""
    with _pool_class()(providers=[sub.provider_name],
                       provider_configs=build_provider_configs()) as pool:
        if not pool.download_subtitle(sub) or not sub.content:
            return 0
    return save_subtitles(ctx, [sub], source='manual')


def main(specific_file=None, rating_key=None, name_hint=None, item=None):
    """
    Main function that processes a single video file.

    Args:
        specific_file (str, optional): Path to a specific file to process. Required.
        rating_key (str, optional): Plex ratingKey — used to upload subtitles via
            the Plex API when the media directory is read-only (Plex mode).
        name_hint (str, optional): Descriptive release name used for subtitle
            matching when the on-disk filename is obfuscated (debrid/usenet mounts).
        item (dict, optional): the media_items row (IDs, title, S/E, release name).
    """
    if not SUBLIMINAL_AVAILABLE:
        logging.warning("Subliminal not available, skipping subtitle download.")
        return

    # Reload configuration to pick up any changes
    config.reload()
    
    # Skip everything if subtitles are not enabled
    if not config.SUBTITLES_ENABLED:
        logging.info("Subtitle downloading is disabled in settings")
        return

    # Require a specific file
    if not specific_file:
        logging.error("No specific file provided")
        return

    # Download subtitles
    if download_subtitles_for_video(specific_file, rating_key=rating_key, name_hint=name_hint, item=item):
        logging.info(f"✅ Successfully processed: {specific_file}")
    else:
        logging.error(f"🚨 Failed to download subtitles for: {specific_file}")

if __name__ == "__main__":
    # Check if a specific file path is provided as a command-line argument
    if len(sys.argv) > 1:
        main(specific_file=sys.argv[1])
    else:
        logging.error("Usage: python downsub.py <video_file>")
        sys.exit(1)