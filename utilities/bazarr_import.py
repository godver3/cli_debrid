"""One-click import of a Bazarr setup into Subtitle Settings.

Reads Bazarr's own API (/api/system/settings and /api/system/languages/profiles,
X-API-KEY header) and maps what cli_debrid supports: languages from the
default profiles, the providers both support, the OpenSubtitles.com login,
embedded-subtitle handling, ignored PGS/ASS tracks (= text-only mode) and
subsync. Everything else is listed as not carried over.
"""
import logging

import requests

# Bazarr provider name -> cli_debrid (subliminal) provider name
PROVIDER_MAP = {
    'opensubtitlescom': 'opensubtitlescom',
    'podnapisi': 'podnapisi',
    'gestdown': 'gestdown',
    'tvsubtitles': 'tvsubtitles',
    'subtitulamostv': 'subtitulamos',
    'subtitulamos': 'subtitulamos',
    'bsplayer': 'bsplayer',
    'opensubtitles': 'opensubtitles',
    'subdl': 'subdl',
    'subsource': 'subsource',
}
# Not search providers here: they describe embedded-track handling
NON_PROVIDERS = {'embeddedsubtitles'}


class BazarrError(Exception):
    pass


def _get(base, key, path):
    try:
        r = requests.get(base + path, headers={'X-API-KEY': key, 'Accept': 'application/json'}, timeout=10)
    except requests.RequestException as e:
        raise BazarrError(f"Could not reach Bazarr at {base}: {e.__class__.__name__}") from None
    if r.status_code in (401, 403):
        raise BazarrError('Bazarr refused the API key (Settings → General → Security in Bazarr)')
    if r.status_code != 200:
        raise BazarrError(f'Bazarr answered HTTP {r.status_code} for {path}')
    try:
        return r.json()
    except ValueError:
        raise BazarrError(f'Bazarr returned something that is not JSON for {path} (wrong URL?)') from None


def _lang_code(code):
    """Bazarr language code (ISO 639-1, or pt-BR style) -> cli_debrid code."""
    from babelfish import Language
    c = (code or '').strip()
    if not c:
        return None
    if c == 'zt':                 # Bazarr's Chinese Traditional
        return 'zho'
    if '-' in c:
        try:
            Language.fromietf(c)
            return c
        except Exception:
            return None
    try:
        return Language.fromalpha2(c).alpha3
    except Exception:
        pass
    try:
        return Language(c).alpha3
    except Exception:
        return None


def fetch_plan(url, api_key):
    base = (url or '').strip().rstrip('/')
    if not base.startswith(('http://', 'https://')):
        raise BazarrError('Bazarr URL must start with http:// or https://')
    if not (api_key or '').strip():
        raise BazarrError('Bazarr API key is required')
    key = api_key.strip()
    settings = _get(base, key, '/api/system/settings')
    profiles = _get(base, key, '/api/system/languages/profiles')
    if not isinstance(settings, dict) or 'general' not in settings:
        raise BazarrError('Unexpected settings reply from Bazarr (is this Bazarr 1.x?)')
    if isinstance(profiles, dict):
        profiles = profiles.get('data') or []
    g = settings.get('general') or {}

    # languages: the default series/movie profiles, else every profile
    wanted_ids = set()
    for kind in ('serie', 'movie'):
        if g.get(f'{kind}_default_enabled') and g.get(f'{kind}_default_profile') not in (None, ''):
            try:
                wanted_ids.add(int(g[f'{kind}_default_profile']))
            except (TypeError, ValueError):
                pass
    used = [p for p in profiles if not wanted_ids or p.get('profileId') in wanted_ids]
    langs, skipped_langs, flags = [], [], set()
    for p in used:
        for it in p.get('items') or []:
            code = _lang_code(it.get('language'))
            if code and code not in langs:
                langs.append(code)
            elif not code:
                skipped_langs.append(it.get('language'))
            if str(it.get('hi')).lower() == 'true':
                flags.add('hearing-impaired preference')
            if str(it.get('forced')).lower() == 'true':
                flags.add('forced-only languages')

    enabled = list(g.get('enabled_providers') or [])
    providers, unsupported = [], []
    for name in enabled:
        if name in NON_PROVIDERS:
            continue
        mapped = PROVIDER_MAP.get(name)
        if mapped:
            if mapped not in providers:
                providers.append(mapped)
        else:
            unsupported.append(name)

    osc = settings.get('opensubtitlescom') or {}
    sync = settings.get('subsync') or {}
    text_only = bool(g.get('ignore_pgs_subs')) and bool(g.get('ignore_ass_subs'))
    probe = bool(g.get('use_embedded_subs')) or 'embeddedsubtitles' in enabled

    not_carried = []
    if unsupported:
        not_carried.append('providers: ' + ', '.join(unsupported))
    if skipped_langs:
        not_carried.append('languages: ' + ', '.join(str(x) for x in skipped_langs))
    if flags:
        not_carried.append(', '.join(sorted(flags)) + ' (cli_debrid picks the best match per language)')
    if g.get('minimum_score') or g.get('minimum_score_movie'):
        not_carried.append(f"minimum scores {g.get('minimum_score')}%/{g.get('minimum_score_movie')}% "
                           '(cli_debrid requires the right episode/movie; hash and release matches rank first)')
    if g.get('upgrade_subs'):
        not_carried.append('subtitle upgrades')
    if len(profiles) > len(used):
        not_carried.append(f'{len(profiles) - len(used)} non-default language profile(s)')

    plan = {
        '_key': key,
        'bazarr_url': base,
        'version': (settings.get('general') or {}).get('version') or '',
        'profiles': [p.get('name') for p in used],
        'subtitle_languages': ','.join(langs),
        'subtitle_providers': providers,
        'opensubtitles_username': osc.get('username') or '',
        'opensubtitles_password': osc.get('password') or '',
        'probe_for_embedded_subtitles': probe,
        'text_subtitles_only': text_only,
        'sync_subtitles': 'unless_hash' if sync.get('use_subsync') else 'off',
        'sync_max_offset': int(sync.get('max_offset_seconds') or 60),
        'subdl_api_key': ((settings.get('subdl') or {}).get('api_key') or '').strip(),
        'subsource_api_key': ((settings.get('subsource') or {}).get('apikey') or '').strip(),
        'not_carried': not_carried,
    }
    return plan


def public_plan(plan):
    """The plan without the password, for the preview."""
    out = {k: v for k, v in plan.items() if not k.startswith('_')}
    out['opensubtitles_password'] = 'set' if plan.get('opensubtitles_password') else ''
    out['subdl_api_key'] = 'set' if plan.get('subdl_api_key') else ''
    out['subsource_api_key'] = 'set' if plan.get('subsource_api_key') else ''
    try:
        from utilities.settings import get_setting
        out['current'] = {
            'subtitle_languages': get_setting('Subtitle Settings', 'subtitle_languages', ''),
            'subtitle_providers': get_setting('Subtitle Settings', 'subtitle_providers', []),
            'enable_subtitles': bool(get_setting('Subtitle Settings', 'enable_subtitles', False)),
            'bazarr_spoof_enabled': bool(get_setting('Bazarr Integration', 'enabled', False)),
        }
    except Exception:
        out['current'] = {}
    return out


def apply_plan(plan, enable=True, stop_bazarr_feed=False):
    from utilities.settings import set_setting
    applied = []

    def put(key, value, label):
        set_setting('Subtitle Settings', key, value)
        applied.append(label)

    if plan.get('subtitle_languages'):
        put('subtitle_languages', plan['subtitle_languages'], f"languages: {plan['subtitle_languages']}")
    if plan.get('subtitle_providers'):
        put('subtitle_providers', plan['subtitle_providers'], 'providers: ' + ', '.join(plan['subtitle_providers']))
    if plan.get('opensubtitles_username') and plan.get('opensubtitles_password'):
        put('opensubtitles_username', plan['opensubtitles_username'], 'OpenSubtitles.com login')
        set_setting('Subtitle Settings', 'opensubtitles_password', plan['opensubtitles_password'])
    for k, label in (('subdl_api_key', 'SubDL API key'), ('subsource_api_key', 'SubSource API key')):
        if plan.get(k):
            set_setting('Subtitle Settings', k, plan[k])
            applied.append(label)
    put('probe_for_embedded_subtitles', bool(plan.get('probe_for_embedded_subtitles')),
        f"skip embedded languages: {'on' if plan.get('probe_for_embedded_subtitles') else 'off'}")
    put('text_subtitles_only', bool(plan.get('text_subtitles_only')),
        f"avoid transcoding formats: {'on' if plan.get('text_subtitles_only') else 'off'}")
    put('sync_subtitles', plan.get('sync_subtitles') or 'off', f"sync: {plan.get('sync_subtitles') or 'off'}")
    put('sync_max_offset', int(plan.get('sync_max_offset') or 60), f"sync max offset: {plan.get('sync_max_offset')}s")
    if enable:
        put('enable_subtitles', True, 'subtitle downloads: on')
    try:
        marked, missing = mark_handled(plan['bazarr_url'], plan['_key'])
        if marked:
            applied.append(f'{marked} subtitles Bazarr already synced/matched will be skipped by Sync existing')
    except Exception as e:
        logging.warning(f"[subs] Bazarr history import failed: {e}")
    if stop_bazarr_feed:
        set_setting('Bazarr Integration', 'enabled', False)
        applied.append('stopped feeding new items to Bazarr')
    logging.info(f"[subs] imported Bazarr settings from {plan.get('bazarr_url')}: {applied}")
    return applied


def _history(base, key):
    rows = []
    for path in ('/api/episodes/history?start=0&length=-1', '/api/movies/history?start=0&length=-1'):
        try:
            data = _get(base, key, path)
        except BazarrError as e:
            logging.info(f"[subs] Bazarr history {path}: {e}")
            continue
        rows += (data.get('data') if isinstance(data, dict) else data) or []
    return rows


def _score_pct(score):
    try:
        return float(str(score).rstrip('%'))
    except (TypeError, ValueError):
        return None


def handled_subtitles(base, key):
    """{subtitles_path: reason} for subtitles whose CURRENT file Bazarr synced
    or downloaded at >= 90% (the score above which Bazarr's subsync skips
    them, i.e. a release match). Bazarr logs a sync just before the download
    it belongs to, so a sync counts for the latest download only when it
    comes after the previous download of that path; an older sync of a since
    replaced subtitle doesn't count."""
    # The API reply has no row ids; each list is newest first, so the
    # position gives the order (episodes and movies are separate lists, and a
    # subtitle path only ever appears in one of them).
    per_path = {}
    for seq, r in enumerate(_history(base, key)):
        path = r.get('subtitles_path')
        if path:
            per_path.setdefault(path, []).append(dict(r, id=-seq))
    out = {}
    for path, rows in per_path.items():
        rows.sort(key=lambda x: x['id'])
        downloads = [r for r in rows if r.get('action') in (1, 2, 3, 4)]
        if not downloads:
            continue
        latest = downloads[-1]
        prev_id = downloads[-2]['id'] if len(downloads) > 1 else float('-inf')
        if any(r.get('action') == 5 and r['id'] > prev_id for r in rows):
            out[path] = 'synced by Bazarr'
            continue
        pct = _score_pct(latest.get('score'))
        if pct is not None and pct >= 90:
            out[path] = f'Bazarr match {pct:.0f}%'
    return out


def mark_handled(base, key):
    """Mark subtitles Bazarr already synced or matched as checked, so 'Sync
    existing subtitles' skips them. Returns (marked, not_found)."""
    import hashlib
    import os
    from database import subtitle_history
    marked = missing = 0
    for path, why in handled_subtitles(base, key).items():
        try:
            if not os.path.isfile(path):
                missing += 1
                continue
            with open(path, 'rb') as f:
                digest = hashlib.sha1(f.read()).hexdigest()
        except OSError:
            missing += 1
            continue
        subtitle_history.mark_sync_checked(digest, why)
        marked += 1
    logging.info(f"[subs] Bazarr import: {marked} subtitle(s) Bazarr already synced/matched marked as checked"
                 f"{f', {missing} not found on disk' if missing else ''}")
    return marked, missing
