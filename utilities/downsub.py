#!/usr/bin/env python3
import os
import sys
import json
import logging
import subprocess
import difflib
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

# stevedore logs every provider plugin at DEBUG each time a provider pool is made
logging.getLogger('stevedore').setLevel(logging.INFO)

if SUBLIMINAL_AVAILABLE:
    try:
        from utilities.subtitle_providers import register as _register_providers
        _register_providers()
    except Exception as _e:
        logging.debug(f"[subs] extra providers unavailable: {_e}")

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
        from utilities.subtitle_lang import parse as _parse_lang
        lang = _parse_lang(c)          # en, fre/ger (639-2/B), names, pob, zt, es-419...
        if lang is not None:
            out.append(lang)
        else:
            logging.warning(f"[subs] unknown subtitle language code: {c}")
    return out

#: Providers that work without a captcha. Order is only a tie-break: every
#: selected provider is queried and the best-scoring subtitle wins.
KNOWN_PROVIDERS = ('opensubtitlescom', 'subdl', 'subsource', 'gestdown', 'tvsubtitles',
                   'subtitulamos', 'bsplayer', 'opensubtitles')
DEFAULT_PROVIDERS = ['opensubtitlescom', 'gestdown', 'tvsubtitles']
#: Providers whose site is gone. Still in subliminal, so saved settings may
#: list them; they're skipped. podnapisi.net stopped resolving in Nov 2026
#: (Bazarr removed it too).
REMOVED_PROVIDERS = frozenset({'podnapisi'})

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


# provider -> (monotonic until, reason[, credentials fingerprint]). A provider
# that hit its download quota or failed its login is skipped until then, so a
# bulk run doesn't ask it again for every item. A login pause also records the
# credentials it failed with and ends as soon as the saved ones change.
_BLOCKED = {}

#: Subtitle Settings keys holding provider logins / API keys.
_CREDENTIAL_KEYS = ('opensubtitles_username', 'opensubtitles_password',
                    'opensubtitles_org_username', 'opensubtitles_org_password',
                    'subdl_api_key', 'subsource_api_key')


def _credentials_fingerprint():
    """Hash of the saved provider credentials, read from settings (not the
    cached config), so a fix saved mid-run is seen at once."""
    import hashlib
    from utilities.settings import get_setting
    raw = '\0'.join(str(get_setting('Subtitle Settings', k, '') or '') for k in _CREDENTIAL_KEYS)
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()


#: Prefix of the ServiceUnavailable a search raises when the site can't be
#: reached at all (DNS failure, connection refused): paused longer than an
#: ordinary outage, since every search would fail the same way.
UNREACHABLE = "can't reach the site"
TIMED_OUT = 'timed out'


def _block_seconds(exc, name=None):
    """(seconds, reason) to pause a provider after exc. By class, so the
    providers' own subclasses (OpenSubtitles Unauthorized, DownloadLimitReached)
    count too."""
    from subliminal.exceptions import (AuthenticationError, ConfigurationError,
                                       DownloadLimitExceeded, ServiceUnavailable)
    try:
        from utilities.subtitle_providers import RateLimited
    except Exception:
        RateLimited = ()
    if RateLimited and isinstance(exc, RateLimited):
        return 900, 'rate limited (too many requests)'
    if isinstance(exc, DownloadLimitExceeded):
        return 6 * 3600, 'daily download limit reached'
    if isinstance(exc, (AuthenticationError, ConfigurationError)):
        if name == 'opensubtitles':
            if (getattr(config, 'OPENSUBTITLES_ORG_USERNAME', '') or '').strip():
                return 3600, 'login failed (check the OpenSubtitles.org username/password)'
            # Using the .com login: the old .org API refuses logins that work
            # on OpenSubtitles.com, so the password is usually not the problem.
            return 6 * 3600, ('login refused by the old OpenSubtitles.org API, which refuses accounts '
                              'that work on OpenSubtitles.com; enter an OpenSubtitles.org login or '
                              'use OpenSubtitles.com instead')
        return 3600, 'login failed (check the OpenSubtitles username/password)'
    if isinstance(exc, ServiceUnavailable):
        if 'HTTP 403' in str(exc):
            return 6 * 3600, 'the site refuses requests (HTTP 403)'
        if UNREACHABLE in str(exc):
            return 3600, f'{UNREACHABLE} (DNS or connection failed)'
        if TIMED_OUT in str(exc):
            return 600, 'searches are timing out'
        return 600, 'service unavailable'
    return 600, f'error ({type(exc).__name__ if exc else "unknown"})'


def blocked_providers():
    now = time.monotonic()
    for k in [k for k, v in _BLOCKED.items() if v[0] <= now]:
        _BLOCKED.pop(k, None)
    login_paused = [k for k, v in _BLOCKED.items() if len(v) > 2 and v[2]]
    if login_paused:
        try:
            current = _credentials_fingerprint()
        except Exception:
            current = None
        changed = [k for k in login_paused if current and _BLOCKED[k][2] != current]
        if changed:
            # New login saved: try it on the next search instead of waiting
            # out the pause, with the new credentials loaded.
            for k in changed:
                _BLOCKED.pop(k, None)
            logging.info(f"[subs] credentials changed; resuming {', '.join(changed)}")
            try:
                config.reload()
            except Exception as e:
                logging.debug(f"[subs] config reload failed: {e}")
    return {k: v[1] for k, v in _BLOCKED.items()}


def _pool_class():
    """ProviderPool that remembers why a provider was discarded."""
    from subliminal.core import ProviderPool

    class _Recorder:
        """Delegates to a provider, remembering the error its last search
        raised: subliminal discards a provider after a failed search
        outside the except block, so the error is gone by then."""
        def __init__(self, prov, name, errors):
            self._prov, self._name, self._errors = prov, name, errors

        def list_subtitles(self, *a, **kw):
            try:
                return self._prov.list_subtitles(*a, **kw)
            except Exception as e:
                import requests
                from subliminal.exceptions import ServiceUnavailable
                if isinstance(e, (requests.Timeout, requests.ConnectionError)):
                    # A site that's down (podnapisi.net stopped resolving) or
                    # not answering: subliminal would log a traceback and
                    # retry it for every item. Paused instead.
                    why = TIMED_OUT if isinstance(e, requests.Timeout) else UNREACHABLE
                    err = ServiceUnavailable(f'{self._name}: {why}')
                    self._errors[self._name] = err
                    raise err from e
                code = getattr(getattr(e, 'response', None), 'status_code', None)
                if code in (403, 503):
                    # The site refuses us (tvsubtitles.net answers 403 to every
                    # search): subliminal would log a traceback per search and
                    # keep asking. As ServiceUnavailable it is paused instead.
                    err = ServiceUnavailable(f'{self._name}: HTTP {code}')
                    self._errors[self._name] = err
                    raise err from e
                self._errors[self._name] = e
                raise

        def __getattr__(self, attr):
            return getattr(self._prov, attr)

    class _TrackingPool(ProviderPool):
        def __getitem__(self, name):
            errors = self.__dict__.setdefault('_last_errors', {})
            try:
                return _Recorder(super().__getitem__(name), name, errors)
            except Exception as e:   # login/initialize failed
                errors[name] = e
                raise

        def _discard_provider(self, name):
            super()._discard_provider(name)
            exc = sys.exc_info()[1] or self.__dict__.get('_last_errors', {}).get(name)
            secs, reason = _block_seconds(exc, name)
            if name not in _BLOCKED:
                logging.warning(f"[subs] pausing provider {name} for {secs // 60} min: {reason}")
            fingerprint = None
            from subliminal.exceptions import AuthenticationError, ConfigurationError
            if isinstance(exc, (AuthenticationError, ConfigurationError)):
                try:
                    fingerprint = _credentials_fingerprint()
                except Exception:
                    fingerprint = None
            _BLOCKED[name] = (time.monotonic() + secs, reason, fingerprint)
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
    out = [p for p in chosen if p in available and p not in REMOVED_PROVIDERS]
    if not out:
        out = [p for p in DEFAULT_PROVIDERS if p in available]
    if include_blocked:
        return out
    blocked = blocked_providers()
    return [p for p in out if p not in blocked]


def build_provider_configs():
    """Credentials per provider. The legacy OpenSubtitles.org provider uses its
    own login when one is set (.org and .com accounts can differ, and the old
    API refuses .com logins), else the OpenSubtitles.com one."""
    pc = {}
    user = config.OPENSUBTITLES_USERNAME
    pwd = config.OPENSUBTITLES_PASSWORD
    if user and pwd:
        pc['opensubtitles'] = {'username': user, 'password': pwd}
        pc['opensubtitlescom'] = {'username': user, 'password': pwd}
    org_user = (getattr(config, 'OPENSUBTITLES_ORG_USERNAME', '') or '').strip()
    org_pwd = getattr(config, 'OPENSUBTITLES_ORG_PASSWORD', '') or ''
    if org_user and org_pwd:
        pc['opensubtitles'] = {'username': org_user, 'password': org_pwd}
    if getattr(config, 'SUBDL_API_KEY', ''):
        pc['subdl'] = {'api_key': config.SUBDL_API_KEY}
    if getattr(config, 'SUBSOURCE_API_KEY', ''):
        pc['subsource'] = {'api_key': config.SUBSOURCE_API_KEY}
    return pc


def test_provider(name, cfg):
    """Check one provider's login or API key (onboarding's Test buttons).
    OpenSubtitles.com: logs in. SubDL/SubSource: searches a well-known movie
    with the key. Uses no download quota. Returns (ok, message, warning)."""
    if not SUBLIMINAL_AVAILABLE:
        return False, 'the subtitle library (subliminal) is not installed', False
    from subliminal.exceptions import AuthenticationError, ConfigurationError, DownloadLimitExceeded
    try:
        from utilities.subtitle_providers import RateLimited
    except Exception:
        RateLimited = DownloadLimitExceeded
    try:
        from subliminal.extensions import provider_manager
        plugin = provider_manager[name].plugin
    except Exception:
        return False, f'{name} is not available in this install', False
    try:
        region.configure('dogpile.cache.memory', replace_existing_backend=True)
    except Exception:
        pass
    prov = None
    try:
        prov = plugin(**cfg)
        prov.initialize()
        if name == 'opensubtitlescom':
            prov.login()
            if not getattr(prov, 'token', None):
                return False, 'OpenSubtitles.com gave no login token; check the username and password', False
            return True, 'logged in to OpenSubtitles.com', False
        video = Movie('The.Matrix.1999.1080p.BluRay.x264.mkv', 'The Matrix', year=1999)
        video.external_ids['imdb_id'] = 'tt0133093'
        video.external_ids['tmdb_id'] = '603'
        found = prov.list_subtitles(video, {Language('eng')})
        return True, f'key works ({len(found)} English subtitles for a test movie)', False
    except (AuthenticationError, ConfigurationError) as e:
        return False, f'refused: {e}', False
    except (RateLimited, DownloadLimitExceeded) as e:
        return True, f'the key was accepted but the service is rate limiting right now ({e})', True
    except Exception as e:
        return False, f'{type(e).__name__}: {e}', False
    finally:
        if prov is not None:
            try:
                prov.terminate()
            except Exception:
                pass


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
    from utilities.subtitle_lang import counts_as_full
    for t in tracks:
        if not counts_as_full(t):
            continue        # forced/signs-only (a forced-flagged full track still counts)
        if text_only and t.get('codec') not in TEXT_SUBTITLE_CODECS:
            continue
        from utilities.subtitle_lang import key as _lkey
        k = _lkey(t.get('language') or '')
        if k:
            langs.add(k)
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
                '-show_entries', 'stream=codec_name:stream_tags:stream_disposition=forced',
                '-of', 'json',
                str(file_path),
            ],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
        from utilities.subtitle_lang import track_frames
        return [{'codec': (st.get('codec_name') or '').lower(),
                 'language': ((st.get('tags') or {}).get('language') or '').strip().lower(),
                 'forced': bool((st.get('disposition') or {}).get('forced')),
                 'title': (st.get('tags') or {}).get('title') or '',
                 'frames': track_frames(st.get('tags'))}
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
_VIDEO_EXTS = (
    '.mkv', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.m4v', '.mpg', '.mpeg', '.ts', '.m2ts', '.vob', '.ogv',
)


def _has_video_ext(name):
    return os.path.splitext((name or '').strip().lower())[1] in _VIDEO_EXTS


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
    library name, which has the title but no group/source.

    For episodes, a filled_by_title (or name_hint) with a video extension
    (e.g. a season pack named after one of its files) is demoted behind the
    item's own video file name so searches aren't steered to another episode.
    """
    item = item or {}
    is_episode = item.get('type') == 'episode' or (
        not item.get('type') and bool(_EP_RE.search(original_path.name))
    )
    filled_file = os.path.basename(item['filled_by_file']) if item.get('filled_by_file') else None
    file_cands = [filled_file, real_path.name if real_path else None, original_path.name]
    raw = [name_hint, item.get('filled_by_title'), item.get('original_scraped_torrent_title'),
           item.get('original_filename'),
           file_cands[0], file_cands[1],
           item.get('debrid_folder_name'), file_cands[2]]
    if is_episode and (_has_video_ext(item.get('filled_by_title')) or _has_video_ext(name_hint)):
        demoted = [n for n in (name_hint, item.get('filled_by_title')) if _has_video_ext(n)]
        kept = [n for n in raw if n not in demoted]
        raw = file_cands + kept + demoted
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


_IMPORTANT_TOKENS = {'480p', '576p', '720p', '1080p', '2160p', 'web', 'webdl', 'webrip', 'hdtv', 'bluray',
                     'bdrip', 'brrip', 'remux', 'dvd', 'dvdrip', 'x264', 'x265', 'h264', 'h265', 'hevc', 'avc',
                     'aac', 'ac3', 'eac3', 'ddp', 'ddp5', 'dts', 'atmos', 'hdr', 'hdr10', 'dv', 'amzn', 'nf',
                     'dsnp', 'hmax', 'atvp', 'hulu', 'pcok', 'max', 'repack', 'proper', 'extended', 'uncut',
                     'directors', 'theatrical', 'imax'}


def _tokens(text):
    return {t for t in re.sub(r'[\W_]+', ' ', (text or '').lower()).split() if len(t) > 1}


def release_similarity(target, candidate):
    """0-100ish: share of the file's release-name words in the subtitle's
    release name, with a bonus for words that decide timing/encode (source,
    resolution, codec, service, cut)."""
    t, c = _tokens(target), _tokens(candidate)
    if not t or not c:
        return 0.0
    return len(t & c) / len(t) * 100 + sum(2 for x in t & c if x in _IMPORTANT_TOKENS)


def _download_count(sub):
    for attr in ('download_count', 'downloads'):
        v = getattr(sub, attr, None)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


_SE_IN_NAME = re.compile(r'(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,4})(?:[ ._-]?(?:-|e|-e)(\d{1,4}))?(?![0-9])', re.I)


def release_episodes(release):
    """Every (season, episode) a release name states, ranges expanded
    (S03E01-E02 -> both); empty when it states none."""
    out = set()
    for m in _SE_IN_NAME.finditer(release or ''):
        s, a = int(m.group(1)), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        if b < a or b - a > 5:
            b = a
        out.update((s, e) for e in range(a, b + 1))
    return out


def allowed_episodes(ctx):
    """Numberings that name this episode: the show's (database), the file's
    own and whichever the search is using now."""
    video = ctx.get('video')
    out = set(ctx.get('allowed_se') or ())
    if isinstance(video, Episode) and video.episodes:
        out.update((video.season, e) for e in video.episodes)
    return out


def names_other_episode(sub, allowed):
    """True when the subtitle's release name states a season/episode and
    none of them is this episode. Providers match on their own numbering
    (OpenSubtitles files Dragon Ball under a different season split), so a
    'season + episode' match can be another episode; the uploader's release
    name gives that away. A name without S/E proves nothing either way."""
    if not allowed:
        return False
    stated = release_episodes(_release_of(sub))
    return bool(stated) and not (stated & allowed)


def identity_ok(sub, video, matches=None):
    """The subtitle is for this movie/episode, not only a good score: an
    episode needs season and episode (series + year + country alone reach
    the score floor), a movie its title. A file-hash match is enough. An
    ID match stands in for the name as in subliminal's compute_score, so a
    subtitle filed under another title (foreign or alternate name) found by
    IMDb/TMDB/TVDB ID still counts."""
    try:
        m = set(matches if matches is not None else sub.get_matches(video))
    except Exception:
        return False
    if 'hash' in m:
        return True
    if isinstance(video, Episode):
        if m & {'imdb_id', 'tmdb_id', 'tvdb_id'}:
            return True
        if m & {'series_imdb_id', 'series_tmdb_id', 'series_tvdb_id'}:
            m.add('series')
        return {'series', 'season', 'episode'} <= m
    return bool(m & {'title', 'imdb_id', 'tmdb_id'})


def _ranker(target_release, allowed=None):
    """compute_score for picking: subliminal's score first, then how close
    the subtitle's release name is to the file's, then popularity. Scaled
    by 10000 so the subliminal score still decides and still gates min_score.
    A subtitle whose release names another episode scores 0 (never picked)."""
    import math
    from subliminal.score import compute_score

    def score(sub, video, **kwargs):
        if names_other_episode(sub, allowed() if callable(allowed) else allowed) or not identity_ok(sub, video):
            return 0
        base = compute_score(sub, video, **kwargs)
        sim = min(release_similarity(target_release, _release_of(sub)), 120.0)
        pop = min(int(math.log10(_download_count(sub) + 1) * 100), 999)
        return base * 10000 + int(sim * 70) + pop
    return score


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


def _label_of(item, path=None):
    """'Show S01E02' / 'Movie (2020)' for log lines, else the file name."""
    item = item or {}
    if item.get('title'):
        t = item['title']
        if item.get('type') == 'episode' and item.get('season_number') is not None:
            t += f" S{int(item['season_number']):02d}E{int(item.get('episode_number') or 0):02d}"
        elif item.get('year'):
            t += f" ({item['year']})"
        return t
    return os.path.basename(str(path or '')) or '?'


def _strip_override_tags(sub):
    """Remove ASS/SSA override blocks some SRT files carry (e.g.
    {\\an8}, {\\pos(273,402)}); several players show them as literal text."""
    try:
        raw = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
        if b'{\\' not in raw:
            return
        text = raw.decode('utf-8', errors='replace')
        sub.content = re.sub(r'\{\\[^}]*\}', '', text).encode('utf-8')
    except Exception as e:
        logging.debug(f"[subs] tag strip failed: {e}")


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
    if fmt == 'srt' and getattr(config, 'TEXT_SUBTITLES_ONLY', False):
        _strip_override_tags(sub)
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
    # Passed down, not set on the shared config: the page, the bulk lanes
    # and post-processing prepare videos from different threads.
    return _prepare_video(video_path, rating_key, name_hint, item,
                          probe=None if check_embedded else False)


def _plex_embedded_langs(item):
    """Good embedded languages (text, not forced, not commentary) from the
    media server's scan of the file (Plex: asked now if not cached;
    Jellyfin/Emby: from the cached library scan), or None if unknown."""
    try:
        from utilities import plex_subtitle_info as psi
        if not item or not psi.available():
            return None
        key = psi.cache_key(item)
        if not key:
            return None
        tracks = psi.cached_tracks([key]).get(key)
        if tracks is None and psi.server() == 'plex':
            rk = psi.rating_key(item)
            if not rk:
                return None
            psi.fetch_tracks([(key, rk)])
            tracks = psi.cached_tracks([key]).get(key)
        return psi.good_embedded_langs(tracks) if tracks is not None else None
    except Exception as e:
        logging.debug(f"[subs] Plex embedded lookup failed: {e}")
        return None


def _initial_allowed(video, item, parse_name, original_path):
    if not isinstance(video, Episode) or not video.episodes:
        return set()
    out = {(video.season, e) for e in video.episodes}
    fse = _file_numbering({'item': item, 'parse_name': parse_name, 'original_path': original_path})
    if fse:
        out.add(fse)
    return out


def _prepare_video(video_path, rating_key, name_hint, item, probe=None):
    """probe: check embedded tracks (None = the probe_for_embedded setting)."""
    if probe is None:
        probe = config.PROBE_FOR_EMBEDDED
    video_path = Path(video_path)

    from utilities.settings import get_setting
    is_plex_mode = get_setting('File Management', 'file_collection_management', 'Plex') == 'Plex'

    # Check if file exists — skip for Plex mode (file is on FUSE mount, not local)
    if not is_plex_mode and not video_path.exists():
        if video_path.is_symlink():
            logging.warning(f"[subs] {_label_of(item, video_path)}: library file is missing: the symlink points "
                            f"to {os.readlink(video_path)}, which isn't on the mount")
        else:
            logging.warning(f"[subs] {_label_of(item, video_path)}: library file is missing ({video_path})")
        return None

    # Check if it's a valid video file
    if not str(video_path).lower().endswith(config.VIDEO_EXTENSIONS):
        logging.error(f"[subs] {video_path}: not a video file")
        return None

    # Handle symlinks - keep track of both paths (skip in Plex mode)
    original_path = video_path
    real_path = None
    if not is_plex_mode and video_path.is_symlink():
        real_path = video_path.resolve()
        logging.debug(f"[subs] symlink {video_path} -> {real_path}")
        video_path = real_path

    if item is None:
        item = _lookup_item(original_path)
    if item and not rating_key and is_plex_mode and item.get('ms_item_id'):
        try:
            from utilities.plex_subtitle_info import rating_key as _rk
            rating_key = _rk(item)
        except Exception:
            rating_key = None

    # Convert language codes to Language objects using expand_languages
    languages = expand_languages(config.SUBTITLE_LANGUAGES)

    if not languages:
        logging.error("[subs] no valid subtitle languages configured")
        return None

    # Probe for embedded subtitles if enabled. Plex mode only uses the
    # add-time playability probe's result (no fresh read of the mount).
    _plex_cached = None
    if probe and is_plex_mode:
        try:
            from usenet.repair_engine import cached_subtitle_tracks
            _plex_cached = cached_subtitle_tracks(str(video_path))
        except Exception:
            _plex_cached = None
    if getattr(config, 'IGNORE_EMBEDDED', False):
        plex_embedded = None
    else:
        plex_embedded = _plex_embedded_langs(item) if probe else None
    if not getattr(config, 'IGNORE_EMBEDDED', False) and (
            plex_embedded is not None or (probe and (not is_plex_mode or _plex_cached is not None))):
        # Plex already scanned the file: use its track list instead of
        # probing the file on the mount
        embedded = plex_embedded if plex_embedded is not None else get_embedded_subtitle_languages(video_path)
        if embedded:
            logging.info(f"[subs] {_label_of(item, original_path)}: embedded languages {sorted(embedded)}")
            # Filter out languages already covered by an embedded track.
            # babelfish Language alpha3 (e.g. 'eng', 'fra') is compared against
            # ffprobe tags which are typically ISO-639-2 (also 3-letter).
            from utilities.subtitle_lang import covers as _covers, key as _lkey
            filtered = [lang for lang in languages if not _covers(_lkey(lang), embedded)]
            if not filtered:
                logging.info(f"[subs] {_label_of(item, original_path)}: every language already embedded, nothing to search")
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
        logging.warning(f"[subs] {_label_of(item, original_path)}: could not identify the file (parsed as {parse_name!r})")
        return None
    video.path = original_path  # subtitles are saved alongside the library file

    # Hash the real file: a hash match is the exact release, so in sync.
    file_hash, file_size = _compute_hash(video_path)
    if file_hash:
        video.size = file_size
        for key in ('opensubtitles', 'opensubtitlescom', 'opensubtitlesvip',
                    'opensubtitlescomvip', 'bsplayer'):
            video.hashes[key] = file_hash

    logging.info(f"[subs] {_label_of(item, original_path)}: matching as {parse_name} "
                 f"(group={video.release_group}, source={video.source}, "
                 f"ids={dict(video.external_ids)}, file hash={'yes' if file_hash else 'no'})")
    return {'all_embedded': False, 'video': video, 'parse_name': parse_name,
            'languages': languages, 'original_path': original_path,
            'video_path': video_path, 'real_path': real_path, 'item': item,
            'rating_key': rating_key, 'is_plex_mode': is_plex_mode,
            'min_score': _min_score(video), 'hash': bool(file_hash),
            'allowed_se': _initial_allowed(video, item, parse_name, original_path)}


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
            matches, _release_of(sub), fmt, via, path, source, sync=ctx.get('_sync_note'),
            media_key=subtitle_history.media_key_of(item))
    except Exception as e:
        logging.debug(f"[subs] history record failed: {e}")


def _mark_checked(content, result):
    try:
        import hashlib
        from database import subtitle_history
        subtitle_history.mark_sync_checked(hashlib.sha1(content).hexdigest(), result)
    except Exception:
        pass


HASH_PROVIDERS = ('opensubtitlescom', 'opensubtitlescomvip', 'opensubtitles', 'opensubtitlesvip', 'bsplayer')


def hash_matches(ctx, languages):
    """Subtitles made for this exact file (OpenSubtitles hash match), best
    first, from the selected providers that support hashes and aren't paused.
    [] without a file hash. Searching costs no download quota."""
    if not ctx.get('hash'):
        return []
    providers = [p for p in selected_providers() if p in HASH_PROVIDERS]
    if not providers:
        return []
    from subliminal.score import compute_score
    video = ctx['video']
    out = []
    with _pool_class()(providers=providers, provider_configs=build_provider_configs()) as pool:
        for sub in pool.list_subtitles(video, set(languages)):
            try:
                if 'hash' in sub.get_matches(video):
                    out.append((int(compute_score(sub, video)), sub))
            except Exception:
                continue
    out.sort(key=lambda x: x[0], reverse=True)
    return [sub for _, sub in out]


def _same_master(video, sub, m):
    """Same disc/stream master, so the same timing: Blu-ray rips of the same
    title share the disc's timing, and WEB-DLs from the same streaming
    service share the service's. Broadcast (HDTV) captures don't: ad breaks
    are cut differently."""
    src = getattr(video, 'source', None)
    if 'source' not in m or src not in ('Blu-ray', 'Web', 'Ultra HD Blu-ray'):
        return False
    if src == 'Web':
        return bool(getattr(video, 'streaming_service', None)) and 'streaming_service' in m
    return True


def release_matches(ctx, languages):
    """Subtitles for this release, best first, from all selected providers:
    the same release group (and source when known), else the same master
    (Blu-ray, or WEB from the same streaming service). These are what
    players pick and are in sync by convention. Returns [(sub, kind)]."""
    video = ctx['video']
    from subliminal.score import compute_score
    out = []
    with _pool_class()(providers=selected_providers(), provider_configs=build_provider_configs()) as pool:
        for sub in pool.list_subtitles(video, set(languages)):
            try:
                if names_other_episode(sub, allowed_episodes(ctx)):
                    continue
                m = sub.get_matches(video)
                if not identity_ok(sub, video, m):
                    continue
                if getattr(video, 'release_group', None) and 'release_group' in m and \
                        (not getattr(video, 'source', None) or 'source' in m):
                    kind, rank = 'release match', 0
                elif _same_master(video, sub, m):
                    kind, rank = 'same-source match', 1
                else:
                    continue
                score = int(compute_score(sub, video))
                if score >= ctx['min_score']:
                    sim = release_similarity(ctx.get('parse_name') or '', _release_of(sub))
                    out.append((rank, -score, -sim, -_download_count(sub), kind, sub))
            except Exception:
                continue
    out.sort(key=lambda x: x[:4])
    return [(x[5], x[4]) for x in out]


def fetch_subtitle(sub):
    """Download one subtitle's content; False on failure/quota (the provider
    is then paused)."""
    with _pool_class()(providers=[sub.provider_name], provider_configs=build_provider_configs()) as pool:
        return bool(pool.download_subtitle(sub) and sub.content)


def _mark_release_match(sub, video, content):
    """A subtitle made for this release (file hash, or the same release group)
    is in sync by construction: mark it checked so Fix existing leaves it
    alone. Returns the note for the history, or None. Reads nothing."""
    try:
        matches = sub.get_matches(video)
    except Exception:
        matches = set()
    if 'hash' in matches:
        _mark_checked(content, 'hash match')
        return 'hash match'
    if 'release_group' in matches:
        _mark_checked(content, 'release match')
        return 'release match (same release group)'
    return None


def _direct_mount_subtitle_server():
    """Direct Mount run with Jellyfin/Emby or Silo: how to reach that server for
    subtitles ({name, label, find, upload, rescan}), else None (Plex,
    Symlinked/Local, or the server isn't configured)."""
    try:
        from utilities.direct_mount import direct_mount_server
        which = direct_mount_server()
        if which == 'jellyfin':
            from utilities import jellyfin_direct as jd
            if jd.is_jellyfin_direct():
                return {'name': 'jellyfin', 'label': 'Jellyfin/Emby', 'find': jd.find_item_id,
                        'upload': lambda path, content, lang, fmt, fid, release: jd.upload_subtitle(
                            path, content, lang, fmt, item_id=fid),
                        'rescan': jd.pick_up_sidecar}
        if which == 'silo':
            from utilities import silo_functions as silo
            if silo.is_silo_mode():
                return {'name': 'silo', 'label': 'Silo', 'find': silo.find_file_id,
                        'upload': lambda path, content, lang, fmt, fid, release: silo.upload_subtitle(
                            path, content, lang, fmt, file_id=fid, release_name=release),
                        'rescan': silo.pick_up_sidecar}
    except Exception as e:
        logging.debug(f"[subs] Direct Mount server lookup failed: {e}")
    return None


def _choose_sidecar_path(symlink_dir, base_name, lang, ext, cli_mount_folder=None):
    """Pick a sidecar path that never overwrites a release/user subtitle (regular
    file or symlink). Re-writing a file Clyde itself wrote earlier (recorded in
    subtitle_history) is allowed.

    Plex local-subtitle naming ("Adding Local Subtitles to Your Media", 200471133)
    reads the ISO language code from the dot-separated segment immediately before
    the extension (or before `.forced`/`.sdh`/`.hi`/`.cc`). Putting a custom tag
    AFTER the language code (`{stem}.{lang}.clyde{ext}`) makes Plex show the track
    as "Unknown"; putting it BEFORE the language code (`{stem}.clyde.{lang}{ext}`)
    keeps the language in both Plex and Jellyfin/Emby (which also uses the extra
    segment as a track label) and in Clyde's own `sidecar_lang()`.
    """
    from database import subtitle_history
    n = 0
    while True:
        if n == 0:
            filename = f"{base_name}.{lang}{ext}"
        elif n == 1:
            filename = f"{base_name}.clyde.{lang}{ext}"
        else:
            filename = f"{base_name}.clyde{n}.{lang}{ext}"
        full_path = symlink_dir / filename
        if not os.path.lexists(full_path):
            return full_path, filename
        if not os.path.islink(full_path):
            if cli_mount_folder is None:
                if subtitle_history.is_clyde_path(str(full_path), saved_via='sidecar'):
                    return full_path, filename
            else:
                if subtitle_history.is_clyde_path(f"{cli_mount_folder}/{filename}", saved_via='cli_mount'):
                    return full_path, filename
        n += 1


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
    # Direct Mount run with Jellyfin/Emby or Silo: sidecars when the folder
    # takes them (then a folder rescan so the server lists them), else an
    # upload through its API, else cli_mount injection below
    server = _direct_mount_subtitle_server()
    use_plex_upload = (not can_write_sidecar) and bool(rating_key) and server is None
    use_server_upload = (not can_write_sidecar) and server is not None
    server_file_id = None
    server_uploaded = 0
    saved = 0
    for sub in subtitles[video]:
        lang = getattr(sub.language, 'ietf', None) or str(sub.language)
        fmt, ext = _final_format(sub)
        if fmt is None:
            continue
        content = sub.content if isinstance(sub.content, bytes) else (sub.content or '').encode('utf-8')
        if not content:
            continue
        ctx['_sync_note'] = _mark_release_match(sub, ctx['video'], content)
        if use_server_upload:
            lang3 = getattr(sub.language, 'alpha3', None) or lang
            if server_file_id is None:
                server_file_id = server['find'](str(original_path)) or ''
            if server_file_id and server['upload'](str(original_path), content, lang3, fmt, server_file_id,
                                                   getattr(sub, 'release_info', None)):
                saved += 1
                server_uploaded += 1
                _record_history(ctx, sub, lang, fmt, server['name'], None, source)
                continue
            # not uploaded: try cli_mount injection below
        if use_plex_upload:
            lang3 = getattr(sub.language, 'alpha3', None) or lang
            if upload_subtitle_to_plex(rating_key, content, lang3, fmt=fmt):
                saved += 1
                _record_history(ctx, sub, lang, fmt, 'plex', None, source)
        elif can_write_sidecar:
            symlink_srt, _ = _choose_sidecar_path(symlink_dir, base_name, lang, ext)
            try:
                with open(symlink_srt, 'wb') as f:
                    f.write(content)
                logging.info(f"[subs] {_label_of(ctx.get('item'), original_path)}: saved {lang} as {symlink_srt}")
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
                _, srt_filename = _choose_sidecar_path(
                    symlink_dir, base_name, lang, ext, cli_mount_folder=torrent_name)
                client = get_climount_client()
                if client.inject_sidecar_file(torrent_name, srt_filename, content):
                    logging.info(f"[subs] Injected sidecar subtitle via cli_mount: {torrent_name}/{srt_filename}")
                    saved += 1
                    _record_history(ctx, sub, lang, fmt, 'cli_mount', f"{torrent_name}/{srt_filename}", source)
                else:
                    logging.warning(f"[subs] {base_name}: cli_mount sidecar injection failed for {lang}")
            except Exception as _dce:
                logging.warning(f"[subs] {base_name}: media dir not writable, no Plex ratingKey, cli_mount injection failed: {_dce}")
    if server is not None and saved > server_uploaded:
        # Sidecar files (written or injected): rescan the folder so the server lists them
        try:
            server['rescan'](str(original_path), (ctx.get('item') or {}).get('type') or '')
        except Exception as e:
            logging.warning(f"[subs] {server['label']} rescan after saving subtitles failed: {e}")
    how = ('uploaded to Plex' if use_plex_upload else
           f"uploaded to {server['label']}" if use_server_upload and server_uploaded == saved else
           'sidecar file' if can_write_sidecar else 'cli_mount sidecar')
    logging.info(f"[subs] {_label_of(ctx.get('item'), original_path)}: saved {saved}/{len(subtitles[video])} ({how})")
    return saved


_LAST_NOTE = {}   # item id -> short result of its last search, for the bulk lane


def last_result_note(item):
    return _LAST_NOTE.get((item or {}).get('id'))


def _note(ctx_or_item, text):
    item = ctx_or_item.get('item') if isinstance(ctx_or_item, dict) and 'item' in ctx_or_item else ctx_or_item
    iid = (item or {}).get('id')
    if iid is not None:
        if len(_LAST_NOTE) > 5000:
            _LAST_NOTE.clear()
        _LAST_NOTE[iid] = text


# ---- other numberings of the same episode ----------------------------------
# The database numbers episodes in the show's order (TVDB aired, DVD,
# absolute, a TMDB group, ...). Subtitle sites mostly follow IMDb/TMDB
# numbering, and release names often use absolute numbers, so a show in
# another order can find nothing. When a search finds nothing, these
# numberings are tried, but only ones proven to be the same episode: the
# file's own name, and TMDB's episode on the same air date (with the same
# title when dates are a day apart). Never a guess.

_TMDB_EPISODES = {}            # tmdb show id -> (monotonic, [(season, episode, air_date, title)])
_TMDB_EPISODES_TTL = 24 * 3600.0


def _norm_title(t):
    return re.sub(r'[^a-z0-9]+', '', str(t or '').lower())


def _file_numbering(ctx):
    """(season, episode) the file's own name gives, or None."""
    item = ctx.get('item') or {}
    try:
        from guessit import guessit
    except Exception:
        return None
    for name in (item.get('filled_by_file'), ctx.get('parse_name'), os.path.basename(str(ctx.get('original_path') or ''))):
        if not name:
            continue
        try:
            g = guessit(str(name), {'type': 'episode'})
        except Exception:
            continue
        s, e = g.get('season'), g.get('episode')
        if isinstance(s, int) and isinstance(e, int):
            return s, e
    return None


def _tmdb_show_episodes(tmdb_id, before_date):
    """TMDB's episodes of a show from the seasons that started by
    before_date (the latest three): [(season, episode, air_date, title)]."""
    import requests
    from utilities.settings import get_setting
    key = (get_setting('TMDB', 'api_key', '') or '').strip()
    if not key or not str(tmdb_id).isdigit():
        return []
    hit = _TMDB_EPISODES.get(str(tmdb_id))
    if hit and time.monotonic() - hit[0] < _TMDB_EPISODES_TTL:
        return hit[1]
    base = f'https://api.themoviedb.org/3/tv/{tmdb_id}'
    try:
        show = requests.get(base, params={'api_key': key}, timeout=15).json()
    except Exception as e:
        logging.debug(f"[subs] TMDB show {tmdb_id}: {e}")
        return []
    seasons = [x for x in show.get('seasons') or [] if x.get('season_number') and x.get('air_date')]
    seasons = sorted((x for x in seasons if x['air_date'] <= before_date), key=lambda x: x['air_date'])[-3:]
    out = []
    for x in seasons:
        try:
            data = requests.get(f"{base}/season/{x['season_number']}", params={'api_key': key}, timeout=15).json()
        except Exception as e:
            logging.debug(f"[subs] TMDB season {tmdb_id}/{x['season_number']}: {e}")
            continue
        for ep in data.get('episodes') or []:
            out.append((x['season_number'], ep.get('episode_number'), ep.get('air_date') or '', ep.get('name') or ''))
    if len(_TMDB_EPISODES) > 500:
        _TMDB_EPISODES.clear()
    _TMDB_EPISODES[str(tmdb_id)] = (time.monotonic(), out)
    return out


def _tmdb_numbering(item):
    """TMDB's (season, episode) for this episode, matched on air date: the
    only episode that day, or the one with the same title within a day."""
    from datetime import date, timedelta
    aired = str(item.get('release_date') or '')[:10]
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', aired) or not item.get('tmdb_id'):
        return None
    day = date.fromisoformat(aired)
    eps = _tmdb_show_episodes(item['tmdb_id'], (day + timedelta(days=1)).isoformat())
    same_day = [e for e in eps if e[2] == aired]
    title = _norm_title(item.get('episode_title'))
    if len(same_day) == 1:
        other = _norm_title(same_day[0][3])
        if not title or not other or other == title or difflib.SequenceMatcher(None, other, title).ratio() >= 0.6:
            return same_day[0][0], same_day[0][1]
    near = {(day + timedelta(days=d)).isoformat() for d in (-1, 0, 1)}
    titled = [e for e in eps if e[2] in near and title and _norm_title(e[3]) == title]
    if len(titled) == 1:
        return titled[0][0], titled[0][1]
    return None


def alternative_numberings(ctx):
    """[(season, episode, why)] other than the one searched, same episode."""
    video = ctx.get('video')
    if not isinstance(video, Episode) or not video.episodes:
        return []
    have = {(video.season, video.episodes[0])}
    out = []
    for finder, why in ((lambda: _file_numbering(ctx), 'the file name'),
                        (lambda: _tmdb_numbering(ctx.get('item') or {}), 'TMDB, same air date')):
        try:
            se = finder()
        except Exception as e:
            logging.debug(f"[subs] numbering via {why} failed: {e}")
            se = None
        if se and se not in have:
            have.add(se)
            out.append((se[0], se[1], why))
    return out


def _use_numbering(video, season, episode):
    video.season, video.episodes = season, [episode]


def get_mode_symlinked():
    """Symlinked/Local file management (files are checked locally)."""
    from utilities.settings import get_setting
    return get_setting('File Management', 'file_collection_management', 'Plex') != 'Plex'


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
            p = Path(str(video_path))
            missing = not p.exists() and get_mode_symlinked()
            _note(item, 'library file is missing (broken symlink)' if missing and p.is_symlink()
                  else 'library file is missing' if missing else 'could not identify the file')
            return False
        if ctx.get('all_embedded'):
            _record_attempt((ctx.get('item') or {}).get('id'), 'embedded')
            _note(ctx, 'every language already embedded in the file')
            return True
        video = ctx['video']
        name = _label_of(ctx.get('item'), ctx['original_path'])
        providers = selected_providers()
        paused = blocked_providers()
        logging.info(f"[subs] {name}: searching {', '.join(providers) or 'nothing'} for "
                     f"{', '.join(getattr(lang, 'ietf', None) or str(lang) for lang in ctx['languages'])} "
                     f"(min score {ctx['min_score']})"
                     + (f"; paused: {', '.join(f'{k} ({v})' for k, v in paused.items())}" if paused else ''))
        start_time = time.time()
        if not providers:
            logging.warning(f"[subs] {name}: no subtitle provider available")
            _note(ctx, 'no provider available (paused: ' + ', '.join(paused) + ')')
            return False
        subtitles = download_best_subtitles(
            {video}, set(ctx['languages']), min_score=ctx['min_score'] * 10000, providers=providers,
            provider_configs=build_provider_configs(), pool_class=_pool_class(),
            compute_score=_ranker(ctx.get('parse_name') or '', lambda: allowed_episodes(ctx)))
        if not subtitles[video]:
            first = (getattr(video, 'season', None), (getattr(video, 'episodes', None) or [None])[0])
            for season, episode, why in alternative_numberings(ctx):
                logging.info(f"[subs] {name}: nothing as S{first[0]:02d}E{first[1]:02d}; "
                             f"trying S{season:02d}E{episode:02d} ({why})")
                _use_numbering(video, season, episode)
                ctx.setdefault('allowed_se', set()).add((season, episode))
                subtitles = download_best_subtitles(
                    {video}, set(ctx['languages']), min_score=ctx['min_score'] * 10000, providers=providers,
                    provider_configs=build_provider_configs(), pool_class=_pool_class(),
                    compute_score=_ranker(ctx.get('parse_name') or '', lambda: allowed_episodes(ctx)))
                if subtitles[video]:
                    break
            else:
                if first[0] is not None:
                    _use_numbering(video, *first)
        took = time.time() - start_time
        item_id = (ctx.get('item') or {}).get('id')
        if not subtitles[video]:
            # Providers paused during this search never answered it: say so
            # instead of listing them as searched.
            now_paused = blocked_providers()
            failed = [p for p in providers if p in now_paused]
            searched = [p for p in providers if p not in now_paused]
            logging.info(f"[subs] {name}: nothing passed the match threshold ({took:.1f}s)")
            note = (f"nothing passed the match threshold ({', '.join(searched)})" if searched
                    else 'no provider answered')
            if failed:
                note += '; ' + ', '.join(f'{p} paused: {now_paused[p]}' for p in failed)
            _note(ctx, note)
            _record_attempt(item_id, False)
            return False
        for sub in subtitles[video]:
            logging.info(f"[subs] {name}: picked {getattr(sub.language, 'ietf', None) or sub.language} "
                         f"from {sub.provider_name}, {_score_text(sub, video)} ({took:.1f}s)")
        ok = save_subtitles(ctx, subtitles[video]) > 0
        picks = '; '.join(f"{getattr(sub.language, 'ietf', None) or sub.language} from {sub.provider_name}, "
                          f"{_score_text(sub, video)}" for sub in subtitles[video])
        _note(ctx, picks if ok else f'found ({picks}) but saving failed, see logs')
        _record_attempt(item_id, ok)
        return ok
    except Exception as e:
        logging.error(f"[subs] {video_path}: error downloading subtitles: {e}")
        return False


def _score_text(sub, video):
    """'score 918/971, matched on hash' / '... on group, source' for logs."""
    try:
        from subliminal.score import compute_score, get_scores
        m = sub.get_matches(video)
        keys = [k for k in ('hash', 'release_group', 'streaming_service', 'source', 'resolution') if k in m]
        what = ', '.join({'release_group': 'group', 'streaming_service': 'service'}.get(k, k) for k in keys)
        return f"score {int(compute_score(sub, video))}/{get_scores(video)['hash']}, matched on {what or 'title/episode only'}"
    except Exception:
        return 'score ?'


def _record_attempt(item_id, found):
    try:
        from database import subtitle_history
        subtitle_history.record_attempt(item_id, found)
    except Exception:
        pass


def _release_of(sub):
    for attr in ('release_name', 'release', 'releases', 'movie_release_name', 'release_info',
                 'release_group', 'version', 'filename', 'file_name', 'name'):
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
            other = names_other_episode(sub, allowed_episodes(ctx))
            if other or not identity_ok(sub, video, matches):
                score = 0
            out.append({'sub': sub, 'score': score, 'matches': matches, 'other_episode': other,
                        'similarity': round(release_similarity(ctx.get('parse_name') or '', _release_of(sub))),
                        'downloads': _download_count(sub),
                        'provider': sub.provider_name, 'release': _release_of(sub),
                        'language': getattr(sub.language, 'ietf', None) or str(sub.language),
                        'hi': bool(getattr(sub, 'hearing_impaired', False))})
    if not any(c['score'] >= ctx['min_score'] for c in out) and isinstance(video, Episode) and not ctx.get('_alt'):
        # Nothing usable under the show's numbering: list what the same
        # episode's other numberings find (the ctx keeps the one that did,
        # so a pick is saved and scored under it).
        first = (video.season, video.episodes[0])
        for season, episode, why in alternative_numberings(ctx):
            _use_numbering(video, season, episode)
            ctx.setdefault('allowed_se', set()).add((season, episode))
            ctx['_alt'] = True
            more = search_candidates(ctx, languages)
            ctx['_alt'] = False
            if any(c['score'] >= ctx['min_score'] for c in more):
                for c in more:
                    c['numbering'] = f"S{season:02d}E{episode:02d} ({why})"
                ctx['numbering'] = f"S{season:02d}E{episode:02d} ({why})"
                return more
        _use_numbering(video, *first)
    out.sort(key=lambda c: (c['score'], c['similarity'], c['downloads']), reverse=True)
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
        logging.debug(f"[subs] done: {specific_file}")
    else:
        logging.info(f"[subs] no subtitles saved for {specific_file}")

if __name__ == "__main__":
    # Check if a specific file path is provided as a command-line argument
    if len(sys.argv) > 1:
        main(specific_file=sys.argv[1])
    else:
        logging.error("Usage: python downsub.py <video_file>")
        sys.exit(1)