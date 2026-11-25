"""One way to read a language code, wherever it comes from.

The same language arrives as 'en', 'eng', 'English' (setting, sidecar file
names), 'fre'/'ger'/'chi' (ISO 639-2/B: Plex, Jellyfin, ffprobe tags),
'fra'/'deu'/'zho' (639-2/T: babelfish), 'en-US', 'pt-BR', 'pob' or 'zh-Hant'.
key() turns all of these into 'fra' / 'por-BR' so they compare.

Country rule: a wanted language without a country ('en', 'pt') is matched by
any variant ('en-US', 'pt-BR'); a wanted 'pt-BR' needs a Brazilian one (a
plain 'pt' subtitle doesn't count), as in Bazarr.
"""
import re

from functools import lru_cache

_ALIASES = {
    'pob': 'pt-BR', 'pb': 'pt-BR', 'ptbr': 'pt-BR', 'pt_br': 'pt-BR', 'brazilian': 'pt-BR',
    'zt': 'zh-TW', 'zht': 'zh-TW', 'zhs': 'zh-CN', 'chs': 'zh-CN', 'cht': 'zh-TW',
    'zh-hant': 'zh-TW', 'zh-hans': 'zh-CN', 'jp': 'ja', 'gr': 'el', 'cz': 'cs', 'dk': 'da', 'se': 'sv',
    'iw': 'he', 'in': 'id', 'ji': 'yi', 'mo': 'ro', 'sh': 'sr',
}
# words in sidecar names that are flags, not languages
FLAG_WORDS = {'hi', 'sdh', 'cc', 'forced', 'default', 'full', 'sub', 'subs', 'srt', 'ass', 'ssa', 'vtt',
              'en-forced', 'foreign', 'signs', 'songs', 'commentary', 'dub'}


@lru_cache(maxsize=4096)
def parse(code):
    """babelfish Language for any common way of writing it, or None."""
    from babelfish import Language
    c = (code or '').strip()
    if not c or c.lower() in ('und', 'unknown', 'mul', 'zxx', 'mis'):
        return None
    c = _ALIASES.get(c.lower(), c)
    import re
    c = re.sub(r'[-_]\d{3}$', '', c)          # numeric regions: es-419 -> es
    attempts = []
    if '-' in c or '_' in c:
        attempts.append(lambda: Language.fromietf(c.replace('_', '-')))
    low = c.lower()
    if len(low) == 2:
        attempts.append(lambda: Language.fromalpha2(low))
    if len(low) == 3:
        attempts += [lambda: Language(low), lambda: Language.fromalpha3b(low)]
    attempts += [lambda: Language.fromname(c.title()), lambda: Language.fromietf(c)]
    for a in attempts:
        try:
            return a()
        except Exception:
            continue
    return None


def key(code_or_lang):
    """'fra' / 'por-BR' / None. Accepts a code string or a babelfish Language."""
    lang = code_or_lang if hasattr(code_or_lang, 'alpha3') else parse(str(code_or_lang or ''))
    if lang is None:
        return None
    country = getattr(lang, 'country', None)
    return f"{lang.alpha3}-{country.alpha2}" if country else lang.alpha3


_KEY_RE = None


def _as_key(x):
    """A key as is ('fra', 'por-BR'), or anything else normalised by key()."""
    global _KEY_RE
    import re
    if _KEY_RE is None:
        _KEY_RE = re.compile(r'^[a-z]{3}(-[A-Z]{2})?$')
    x = x if hasattr(x, 'alpha3') else str(x or '')
    if isinstance(x, str) and _KEY_RE.match(x):
        base, _, cc = x.partition('-')
        k = key(base)                       # normalises 'fre' -> 'fra', 'pob' -> 'por-BR'
        if k:
            return k if (cc == '' or '-' in k) else f"{k}-{cc}"
    return key(x)


def matches(wanted, have):
    """Does a subtitle in `have` cover the wanted language? (keys or codes)"""
    w, h = _as_key(wanted), _as_key(have)
    if not w or not h:
        return False
    w3, _, wc = w.partition('-')
    h3, _, hc = h.partition('-')
    if w3 != h3:
        return False
    return not wc or wc == hc


def covers(wanted, haves):
    return any(matches(wanted, h) for h in haves or ())


def sidecar_lang(name_middle):
    """Language and flags from the part of a sidecar name between the video
    stem and the extension, e.g. 'en', 'eng.hi', 'English.forced', 'pt-BR'.
    Returns (key or None, set of flag words)."""
    parts = [p for p in (name_middle or '').replace('_', '.').split('.') if p]
    if len(parts) == 1 and parts[0].lower() == 'hi':
        return key('hi'), set()          # 'Movie.hi.srt' alone is Hindi, not a flag
    flags = {p.lower() for p in parts if p.lower() in FLAG_WORDS}
    for p in parts:
        if p.lower() in FLAG_WORDS:
            continue
        k = key(p)
        if k:
            return k, flags
    return None, flags


# A forced track only carries the foreign-language parts: a few dozen lines
# per episode. Anime releases often flag their full dialogue track forced
# by mistake ("Stylized Subtitles", "Full", hundreds of lines). Such a track
# still covers the language; a genuine forced/signs track never does.
_FORCED_TITLE = re.compile(r'sign|song|forced|foreign|karaoke|lyrics|commentary|\bfs\b', re.I)
_FULL_TITLE = re.compile(r'full|dialog|dialogue|subtitles|subs\b|stylized|styled|\bcc\b|sdh|complete', re.I)
FULL_TRACK_MIN_LINES = 250


def track_frames(tags):
    """Line count mkvmerge records for a subtitle track (NUMBER_OF_FRAMES,
    possibly with a '-eng' suffix), or None."""
    for k, v in (tags or {}).items():
        if str(k).upper().startswith('NUMBER_OF_FRAMES'):
            try:
                return int(str(v).strip())
            except (TypeError, ValueError):
                return None
    return None


def counts_as_full(track):
    """Does an embedded subtitle track cover its language? Not forced: yes.
    Forced: only when its title doesn't say signs/songs/forced and it is
    clearly full (titled full/dialogue/subtitles, or 250+ lines)."""
    if not track.get('forced'):
        return True
    title = str(track.get('title') or '')
    if _FORCED_TITLE.search(title):
        return False
    frames = track.get('frames')
    return bool(_FULL_TITLE.search(title)) or (isinstance(frames, int) and frames >= FULL_TRACK_MIN_LINES)
