"""SubDL and SubSource as subliminal providers.

Both have large catalogs (strong for TV and for releases OpenSubtitles has
little for) and need a free API key. They plug into the same ProviderPool,
scoring, pausing and history as subliminal's own providers; matching uses the
release name each subtitle was made for.
"""
import io
import logging
import re
import zipfile

import requests
from babelfish import Language
from subliminal.exceptions import AuthenticationError, DiscardingError, ServiceUnavailable


class RateLimited(DiscardingError):
    """HTTP 429: too many requests for now. Not the daily download quota,
    so the provider is paused briefly."""
from subliminal.matches import guess_matches
from subliminal.providers import Provider
from subliminal.subtitle import Subtitle
from subliminal.utils import sanitize
from subliminal.video import Episode, Movie

try:
    from guessit import guessit
except Exception:  # pragma: no cover
    guessit = None

UA = 'cli_debrid'
# ISO 639-1 -> provider codes
_SUBDL = {'en': 'EN', 'es': 'ES', 'fr': 'FR', 'de': 'DE', 'it': 'IT', 'pt': 'PT', 'nl': 'NL', 'pl': 'PL',
          'ru': 'RU', 'ja': 'JA', 'ko': 'KO', 'zh': 'ZH', 'ar': 'AR', 'he': 'HE', 'sv': 'SV', 'no': 'NO',
          'da': 'DA', 'fi': 'FI', 'tr': 'TR', 'el': 'EL', 'hu': 'HU', 'cs': 'CS', 'ro': 'RO', 'th': 'TH',
          'vi': 'VI', 'hr': 'HR', 'sr': 'SR', 'bs': 'BS', 'id': 'ID', 'ms': 'MS', 'fa': 'FA', 'bg': 'BG'}
_SUBSOURCE = {'en': 'english', 'es': 'spanish', 'fr': 'french', 'de': 'german', 'it': 'italian',
              'pt': 'portuguese', 'nl': 'dutch', 'pl': 'polish', 'ru': 'russian', 'ja': 'japanese',
              'ko': 'korean', 'zh': 'chinese', 'ar': 'arabic', 'he': 'hebrew', 'sv': 'swedish',
              'no': 'norwegian', 'da': 'danish', 'fi': 'finnish', 'tr': 'turkish', 'el': 'greek',
              'hu': 'hungarian', 'cs': 'czech', 'ro': 'romanian', 'th': 'thai', 'vi': 'vietnamese',
              'hr': 'croatian', 'sr': 'serbian', 'bs': 'bosnian', 'id': 'indonesian', 'fa': 'farsi_persian',
              'bg': 'bulgarian'}


def _langs(codes):
    out = set()
    for c in codes:
        try:
            out.add(Language.fromalpha2(c))
        except Exception:
            pass
    return frozenset(out)


_EP_RES = (re.compile(r'\bS(\d{1,2})[ ._-]?E(\d{1,3})\b', re.I), re.compile(r'\b(\d{1,2})x(\d{1,3})\b', re.I))


_EP_ONLY = re.compile(r'(?<![a-z0-9])(?:episode|ep|e)[ ._-]?(\d{1,4})(?![0-9])', re.I)


def episode_in(text, season, episode):
    """True / False when the text names a season+episode (or, failing that,
    an episode number: "Ep6", "Episode 6", "E06"), None when it names none
    (e.g. a whole-season pack)."""
    for rx in _EP_RES:
        found = [(int(a), int(b)) for a, b in rx.findall(text or '')]
        if found:
            return (season, episode) in found
    if episode is not None:
        eps = [int(x) for x in _EP_ONLY.findall(text or '')]
        if eps:
            return int(episode) in eps
    return None


def season_pack_of(text, season):
    """The text names this season without an episode ("Show S03 1080p")."""
    return season is not None and re.search(r'(?<![a-z0-9])(?:s|season[ ._-]?)0*%d(?![0-9e])' % int(season),
                                            text or '', re.I) is not None


def pick_from_zip(content, season=None, episode=None):
    """The subtitle file in a zip: right episode first, then .srt over others."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return content
    exts = {'.srt': 40, '.ass': 20, '.ssa': 10, '.vtt': 30}
    names = [n for n in zf.namelist() if any(n.lower().endswith(e) for e in exts)]
    best = None
    for name in names:
        low = name.lower()
        score = exts[next(e for e in exts if low.endswith(e))]
        if season is not None and episode is not None:
            hit = episode_in(name, season, episode)
            if hit is False:
                continue
            if not hit and len(names) > 1:
                continue     # a pack: only the file that names this episode
            score += 100 if hit else 0
        if best is None or score > best[0]:
            best = (score, name)
    return zf.read(best[1]) if best else b''


class ReleaseSubtitle(Subtitle):
    """A subtitle known by the release name it was made for."""

    provider_name = 'release'

    def __init__(self, language, subtitle_id, *, release_name='', hearing_impaired=False, page_link=None,
                 download_count=0, video_kind='movie', title=None, year=None, season=None, episode=None,
                 download=None):
        super().__init__(language, subtitle_id, hearing_impaired=hearing_impaired, page_link=page_link)
        self.release_name = release_name or ''
        self.download_count = download_count or 0
        self.video_kind = video_kind
        self.title = title
        self.year = year
        self.season = season
        self.episode = episode
        self.download = download or {}

    @property
    def id(self):
        return str(self.subtitle_id)

    @property
    def info(self):
        return self.release_name or self.id

    def get_matches(self, video):
        matches = set()
        if isinstance(video, Episode):
            matches |= guess_matches(video, {'title': self.title, 'season': self.season,
                                             'episode': self.episode, 'year': self.year})
            if self.title is None:     # found by the series' IMDb id
                matches |= {'series', 'year', 'country'}
        elif isinstance(video, Movie):
            matches |= guess_matches(video, {'title': self.title, 'year': self.year})
            if self.title is None:
                matches |= {'title', 'year', 'country'}
        if self.release_name and guessit is not None:
            for part in re.split(r'[,|]\s*', self.release_name):
                try:
                    guess = guessit(part, {'type': 'episode' if isinstance(video, Episode) else 'movie'})
                except Exception:
                    continue
                m = guess_matches(video, guess, partial=True)
                # the release text decides group/source/codec; identity comes from the ids above
                matches |= m & {'release_group', 'source', 'streaming_service', 'resolution',
                                'video_codec', 'audio_codec'}
        return matches


class SubdlSubtitle(ReleaseSubtitle):
    provider_name = 'subdl'


class SubsourceSubtitle(ReleaseSubtitle):
    provider_name = 'subsource'


class _KeyProvider(Provider):
    video_types = (Episode, Movie)

    def __init__(self, api_key=None, timeout=15):
        self.api_key = (api_key or '').strip()
        self.timeout = timeout
        self.session = None

    def initialize(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA

    def terminate(self):
        if self.session:
            self.session.close()

    def _ids(self, video):
        if isinstance(video, Episode):
            return video.external_ids.get('series_imdb_id'), video.season, video.episode
        return video.external_ids.get('imdb_id'), None, None

    def _check(self, r):
        if r.status_code in (401, 403):
            raise AuthenticationError(f'{self.__class__.__name__}: API key refused')
        if r.status_code == 429:
            raise RateLimited(f'{self.__class__.__name__}: rate limited (HTTP 429)')
        if r.status_code >= 500:
            raise ServiceUnavailable(f'{self.__class__.__name__}: HTTP {r.status_code}')
        r.raise_for_status()

    def download_subtitle(self, subtitle):
        content = self._fetch(subtitle)
        if content:
            subtitle.content = pick_from_zip(content, subtitle.season, subtitle.episode)


class SubdlProvider(_KeyProvider):
    """subdl.com (free API key: subdl.com → account → API)."""

    languages = _langs(_SUBDL)
    subtitle_class = SubdlSubtitle
    API = 'https://api.subdl.com/api/v1/subtitles'
    DL = 'https://dl.subdl.com'

    def list_subtitles(self, video, languages):
        if not self.api_key:
            return []
        imdb, season, episode = self._ids(video)
        params = {'api_key': self.api_key, 'subs_per_page': '30', 'releases': '1', 'hi': '1', 'unpack': '1',
                  'languages': ','.join(_SUBDL[lang.alpha2] for lang in languages if getattr(lang, 'alpha2', None) in _SUBDL),
                  'type': 'tv' if isinstance(video, Episode) else 'movie'}
        if imdb and str(imdb).startswith('tt'):
            params['imdb_id'] = imdb
        else:
            params['film_name'] = video.series if isinstance(video, Episode) else video.title
            if getattr(video, 'year', None):
                params['year'] = str(video.year)
        if season is not None:
            params['season_number'] = str(season)
            params['episode_number'] = str(episode)
        r = self.session.get(self.API, params=params, timeout=self.timeout)
        self._check(r)
        data = r.json()
        if not data.get('status'):
            return []
        back = {v: k for k, v in _SUBDL.items()}
        out = []
        for sub in data.get('subtitles') or []:
            code = back.get(str(sub.get('language') or sub.get('lang') or '').upper()[:2])
            try:
                lang = Language.fromalpha2(code) if code else next(iter(languages))
            except Exception:
                continue
            files = sub.get('unpack_files') or [sub]
            for f in files:
                url = f.get('url') or sub.get('url')
                if not url:
                    continue
                if url.startswith('/'):
                    url = self.DL + url
                rel = f.get('release_name') or sub.get('release_name') or f.get('name') or sub.get('name') or ''
                if season is not None:
                    hit = episode_in(f.get('name') or rel, season, episode)
                    if hit is False:
                        continue
                out.append(SubdlSubtitle(
                    lang, url, release_name=rel, hearing_impaired=bool(f.get('hi', sub.get('hi', False))),
                    page_link=url, download_count=int(sub.get('downloads') or sub.get('download_count') or 0),
                    video_kind='episode' if season is not None else 'movie',
                    title=None if imdb else params.get('film_name'), year=getattr(video, 'year', None),
                    season=season, episode=episode, download={'url': url}))
        return out

    def _fetch(self, subtitle):
        r = self.session.get(subtitle.download['url'], params={'api_key': self.api_key},
                             headers={'x-api-key': self.api_key}, timeout=self.timeout)
        self._check(r)
        return r.content


class SubsourceProvider(_KeyProvider):
    """subsource.net (free API key: subsource.net → profile → API)."""

    languages = _langs(_SUBSOURCE)
    subtitle_class = SubsourceSubtitle
    API = 'https://api.subsource.net/api/v1'

    def _get(self, path, params):
        r = self.session.get(self.API + path, params=params, headers={'X-API-Key': self.api_key},
                             timeout=self.timeout)
        self._check(r)
        return r.json()

    def list_subtitles(self, video, languages):
        if not self.api_key:
            return []
        imdb, season, episode = self._ids(video)
        title = video.series if isinstance(video, Episode) else video.title
        found = []
        if imdb and str(imdb).startswith('tt'):
            found = self._get('/movies/search', {'searchType': 'imdb', 'imdb': imdb}).get('data') or []
        if not found and title:
            found = [m for m in (self._get('/movies/search', {'searchType': 'text', 'q': title}).get('data') or [])
                     if sanitize(str(m.get('title') or '')) == sanitize(title)
                     and (not getattr(video, 'year', None) or not m.get('releaseYear')
                          or abs(int(m['releaseYear']) - int(video.year)) <= 1)]
        if season is not None:
            with_season = [m for m in found if m.get('season') is not None]
            if with_season:
                found = [m for m in with_season if int(m.get('season') or -1) == int(season)]
        out = []
        back = {v: k for k, v in _SUBSOURCE.items()}
        for m in found[:3]:
            for lang in languages:
                name = _SUBSOURCE.get(getattr(lang, 'alpha2', None))
                if not name:
                    continue
                for sub in self._get('/subtitles', {'movieId': m.get('movieId'), 'language': name}).get('data') or []:
                    sid = sub.get('subtitleId')
                    if not sid:
                        continue
                    rel = sub.get('releaseInfo') or []
                    rel = ', '.join(str(x) for x in rel if str(x).strip()) if isinstance(rel, list) else str(rel)
                    s_ep = (season, episode)
                    if season is not None:
                        hit = episode_in(rel, season, episode)
                        if hit is False:
                            continue
                        if hit is None and not season_pack_of(rel, season):
                            # SubSource lists every subtitle of the show: a
                            # release naming no episode (and not this season's
                            # pack) isn't known to be this episode.
                            s_ep = (None, None)
                    code = back.get(str(sub.get('language') or name).lower(), getattr(lang, 'alpha2', 'en'))
                    out.append(SubsourceSubtitle(
                        Language.fromalpha2(code), sid, release_name=rel,
                        hearing_impaired=bool(sub.get('hearingImpaired')), page_link=sub.get('link'),
                        download_count=int(sub.get('downloads') or 0),
                        video_kind='episode' if season is not None else 'movie', title=None,
                        year=getattr(video, 'year', None), season=s_ep[0], episode=s_ep[1],
                        download={'id': sid}))
        return out

    def _fetch(self, subtitle):
        r = self.session.get(f"{self.API}/subtitles/{subtitle.download['id']}/download",
                             headers={'X-API-Key': self.api_key}, timeout=self.timeout)
        self._check(r)
        return r.content


def _patch_opensubtitles_org_terminate():
    """subliminal's OpenSubtitles.org provider ends with self.server.close(),
    which xmlrpc sends to the server as a remote 'close' method; the server
    answers 'method not found' and every subtitle run logs a traceback
    ("Provider opensubtitles improperly terminated"). Log out, then close the
    connection locally."""
    try:
        from subliminal.providers.opensubtitles import OpenSubtitlesProvider, checked
    except Exception:
        return
    if getattr(OpenSubtitlesProvider.terminate, '_clyde_patched', False):
        return

    def terminate(self):
        try:
            if self.token:
                checked(self.server.LogOut(self.token))
        finally:
            try:
                self.server('close')()
            except Exception:
                pass
            self.token = None

    terminate._clyde_patched = True
    OpenSubtitlesProvider.terminate = terminate


class _NotFoundIsInfo(logging.Filter):
    """Gestdown (Addic7ed) logs a show it doesn't carry as an ERROR
    ("No show id found for 'Pokemon'"). That is a normal miss: log it as INFO."""
    def filter(self, record):
        if record.levelno >= logging.ERROR and str(record.msg).startswith('No show id found for'):
            record.levelno, record.levelname = logging.INFO, 'INFO'
        return True


def _quiet_gestdown_misses():
    log = logging.getLogger('subliminal.providers.gestdown')
    if not any(isinstance(f, _NotFoundIsInfo) for f in log.filters):
        log.addFilter(_NotFoundIsInfo())


def register():
    """Make 'subdl' and 'subsource' available to subliminal's provider manager."""
    _patch_opensubtitles_org_terminate()
    _quiet_gestdown_misses()
    try:
        from subliminal.extensions import provider_manager
        names = set(provider_manager.names())
        for name, cls in (('subdl', 'SubdlProvider'), ('subsource', 'SubsourceProvider')):
            if name not in names:
                provider_manager.register(f'{name} = utilities.subtitle_providers:{cls}')
    except Exception as e:
        logging.debug(f"[subs] could not register SubDL/SubSource: {e}")
