"""Settings export / import ("share your setup").

An export holds only settings that make sense on someone else's install:
versions, scraping and queue behaviour, naming and folder layout, streaming
tuning, subtitle and notification preferences. It never holds secrets (API
keys, tokens, passwords), and never anything tied to one machine or one
person (URLs, paths, hosts, usernames, servers, accounts).

The same filter runs again on import, so a hand-edited or hostile file can
only ever change portable settings: it cannot point a script path, a Plex
URL or a scraper URL somewhere else. Imports merge; they never delete, and
keys the file does not carry keep their current value. A scraper, content
source or notification channel that is new to this install arrives turned
off, because the parts it needs to work (URL, key, account) never travel.
"""

import copy
import json
import logging
import os
import re
from datetime import datetime

from utilities.settings import get_config_dir, load_config, save_config
from utilities.settings_schema import SETTINGS_SCHEMA

FORMAT = 'cli_debrid-settings'
FORMAT_VERSION = 1

# (id, label, what travels). Order is the order the UI shows.
GROUPS = [
    ('versions', 'Versions',
     'Quality versions (filters, scoring, size and bitrate limits) and the version terms.'),
    ('scraping', 'Scraping, queues & load',
     'Scraping behaviour, upgrades, queue timing and system load regulation.'),
    ('scrapers', 'Scrapers',
     'Which scrapers you use, their priority and options. URLs and keys stay behind; '
     'new ones that need them arrive turned off.'),
    ('content_sources', 'Content sources',
     'Lists and their options. Account-linked sources stay behind; new ones arrive turned off.'),
    ('library', 'Naming & folders',
     'Symlink layout, folder names, file name templates and naming options.'),
    ('streaming', 'Mount & streaming',
     'Built-in mount tuning. Servers, paths, cache size and accounts stay behind.'),
    ('subtitles', 'Subtitles', 'Languages, providers and subtitle options. Logins and keys stay behind.'),
    ('notifications', 'Notifications',
     'Which events notify you. Webhooks, tokens and addresses stay behind; new channels arrive turned off.'),
    ('interface', 'Interface & extras',
     'Date/time format, home page limits, Discover, Library Manager, tags, box sets, overlays.'),
]
GROUP_IDS = [g[0] for g in GROUPS]

# Section -> rule. 'only': the keys that may travel (None = every key that
# passes the personal-key filter). 'drop': extra keys that never travel.
# Sections not listed here never travel at all (Plex, SSO, TMDB/TVDB, Trakt,
# MDBList, Scrob, Floppy, Simkl, Bazarr, AI Assistant, MCP, Custom
# Post-Processing, Metadata Battery, ...).
_SECTION_RULES = {
    'Scraping': {'group': 'scraping'},
    'Reverse Parser': {'group': 'versions'},
    # content_source_priority lists this install's source IDs, which differ elsewhere.
    'Queue': {'group': 'scraping', 'drop': {'content_source_priority'}},
    'Staleness Threshold': {'group': 'scraping'},
    'System Load Regulation': {'group': 'scraping'},
    'Debrid Provider': {'group': 'library', 'only': {
        'enable_debrid_naming', 'include_version_in_debrid_naming',
        'include_content_source_in_debrid_naming', 'ffprobe_all_debrid_additions'}},
    'Usenet Provider': {'group': 'library', 'only': {
        'enable_nzb_naming', 'include_version_in_nzb_naming',
        'include_content_source_in_nzb_naming', 'disable_nzb_season_packs', 'ffprobe_all_nzbs'}},
    'File Management': {'group': 'library', 'only': {
        'symlink_organize_by_type', 'symlink_organize_by_resolution',
        'symlink_organize_by_version', 'symlink_folder_order'}},
    'Debug': {'group': 'library', 'only': {
        'symlink_movie_template', 'symlink_episode_template', 'filename_filter_out_list',
        'enable_separate_anime_folders', 'enable_separate_documentary_folders',
        'movies_folder_name', 'tv_shows_folder_name', 'anime_movies_folder_name',
        'anime_tv_shows_folder_name', 'documentary_movies_folder_name',
        'documentary_tv_shows_folder_name'}},
    'Built-in Mount': {'group': 'streaming', 'drop': {
        'enabled', 'cache_size_gb', 'virtual_folders', 'primary_load_mode',
        'debug_logging', 'debrid_user_agent', 'plex_playback_protection', 'plex_prewarm',
        'allow_other', 'run_alongside', 'webdav_enabled', 'webdav_port'}},
    'Subtitle Settings': {'group': 'subtitles', 'drop': {
        'user_agent', 'apply_to_movies', 'apply_to_tv_shows', 'apply_to_anime_movies',
        'apply_to_anime_tv_shows'}},
    'UI Settings': {'group': 'interface', 'only': {
        'use_24hour_format', 'compact_view', 'date_format', 'time_format',
        'recently_added_limit', 'recently_upgraded_limit', 'upcoming_releases_start_limit',
        'upcoming_releases_end_limit', 'program_logo', 'enable_caching'}},
    'Discover Settings': {'group': 'interface'},
    'Library Manager': {'group': 'interface'},
    'Tags': {'group': 'interface'},
    # The on-switches of features that write to Plex or add movies stay behind:
    # importing someone's setup must not start changing this library.
    'Plex Movie Box Sets': {'group': 'interface', 'drop': {'enabled', 'grab_missing'}},
    'Plex Reported Issues': {'group': 'interface', 'drop': {'enabled'}},
    'Overlay Settings': {'group': 'interface', 'drop': {'overlays_enabled'}},
    'Plex Smart Collections': {'group': 'interface', 'only': {
        'poster_design', 'poster_accent', 'poster_eyebrow', 'poster_icon',
        'poster_overlay_opacity', 'poster_glow_opacity', 'poster_glow_radius'}},
}
# Keys of a section that belong to a different group than the section.
_KEY_GROUPS = {('Scraping', 'versions'): 'versions'}

_INSTANCE_SECTIONS = {'Scrapers': 'scrapers', 'Content Sources': 'content_sources',
                      'Notifications': 'notifications'}

# A key whose name has one of these words (split on '_') is personal or
# secret and never travels, whatever section it is in.
_PERSONAL_WORDS = {
    'url', 'urls', 'uri', 'path', 'dir', 'host', 'hosts', 'proxy', 'server', 'servers',
    'email', 'address', 'webhook', 'username', 'user', 'password', 'pass', 'passwd',
    'token', 'secret', 'key', 'keys', 'apikey', 'api', 'auth', 'uid', 'gid', 'umask',
    'login', 'credential', 'credentials', 'chat', 'friends', 'requesters', 'uuid',
    'location', 'db',
}
_SECRET_FRAGMENTS = ('token', 'secret', 'password', 'passwd', 'apikey', 'api_key',
                     'credential', 'private', 'webhook', 'passkey', 'bearer')

# Scrapers that work with no URL or key; every other type needs setup.
_SCRAPERS_WITHOUT_SETUP = {'Torrentio', 'Nyaa', 'OldNyaa'}
# 'enabled' never travels for scrapers, sources or channels: an import never
# turns one on or off.
_SCRAPER_KEYS = {'type', 'priority', 'opts'}
# Torrentio opts segments that carry a debrid key ("realdebrid=XXXX").
_TORRENTIO_KEY_OPTS = {'realdebrid', 'alldebrid', 'premiumize', 'debridlink', 'torbox',
                       'offcloud', 'putio', 'easydebrid', 'debrider', 'stremthru'}

# Content sources that point at one person's account or server; useless (or
# a privacy leak) anywhere else.
_ACCOUNT_SOURCES = {'Overseerr', 'Other Plex Watchlist', 'My Plex RSS Watchlist',
                    'My Friends Plex RSS Watchlist', 'Plex Friends Watchlist',
                    'Friends Trakt Watchlist', 'Silo Requests'}
# Public list identifiers that are the point of sharing a list source.
_PUBLIC_LIST_KEYS = {'MDBList': {'urls', 'username', 'listname', 'list_id'}}

_NOTIFICATION_KEYS = {'type', 'notify_on', 'priority'}

# Keys that say *which* list a content source is. Two sources of one type
# with the same values are the same source; a source type with none of these
# (My Plex Watchlist, Collected, ...) is one per install.
_SOURCE_IDENTITY_KEYS = ('urls', 'username', 'listname', 'list_id', 'trakt_lists',
                         'scrob_list_ids', 'floppy_list_ids', 'special_list_type',
                         'special_list_genres', 'lists', 'floppy_statuses', 'simkl_statuses')


def _words(key):
    return set(re.split(r'[_\-\s]+', str(key).lower()))


def _schema_sensitive(section, key):
    field = SETTINGS_SCHEMA.get(section, {}).get(key)
    return isinstance(field, dict) and bool(field.get('sensitive'))


def _personal_key(key):
    """True when a key name marks a secret or personal value."""
    kl = str(key).lower()
    if not kl.strip():
        return True
    if any(f in kl for f in _SECRET_FRAGMENTS):
        return True
    return bool(_words(kl) & _PERSONAL_WORDS)


def _key_travels(section, key, rule):
    if not str(key).strip():
        return False
    if rule.get('only') is not None and key not in rule['only']:
        return False
    if key in rule.get('drop', ()):
        return False
    if _schema_sensitive(section, key):
        return False
    if (section, key) in _KEY_GROUPS:
        return True
    # Only keys the current schema knows: leftovers of removed settings stay behind.
    known = SETTINGS_SCHEMA.get(section)
    if isinstance(known, dict) and key not in known:
        return False
    return not _personal_key(key)


def _group_of(section, key):
    if section in _INSTANCE_SECTIONS:
        return _INSTANCE_SECTIONS[section]
    return _KEY_GROUPS.get((section, key)) or _SECTION_RULES[section]['group']


def _clean_opts(opts):
    if not isinstance(opts, str):
        return ''
    kept = []
    for seg in opts.split('|'):
        name = seg.split('=', 1)[0].strip().lower()
        if not seg.strip() or name in _TORRENTIO_KEY_OPTS or _personal_key(name):
            continue
        kept.append(seg.strip())
    return '|'.join(kept)


def _clean_scraper(sid, cfg):
    if not isinstance(cfg, dict):
        return None
    stype = cfg.get('type') or str(sid).rsplit('_', 1)[0]
    out = {k: copy.deepcopy(cfg[k]) for k in _SCRAPER_KEYS if k in cfg}
    out['type'] = stype
    if 'opts' in out:
        out['opts'] = _clean_opts(out['opts'])
    if stype not in _SCRAPERS_WITHOUT_SETUP:
        out['needs_setup'] = True
    return out


def _clean_content_source(sid, cfg):
    if not isinstance(cfg, dict):
        return None
    stype = cfg.get('type') or str(sid).rsplit('_', 1)[0]
    if stype in _ACCOUNT_SOURCES:
        return None
    public = _PUBLIC_LIST_KEYS.get(stype, set())
    schema = SETTINGS_SCHEMA.get('Content Sources', {}).get('schema', {}).get(stype, {})
    out = {}
    for k, v in cfg.items():
        if not str(k).strip() or k == 'enabled':
            continue
        if k in public:
            out[k] = copy.deepcopy(v)
            continue
        f = schema.get(k)
        if (isinstance(f, dict) and f.get('sensitive')) or _personal_key(k):
            continue
        out[k] = copy.deepcopy(v)
    out['type'] = stype
    return out


def _clean_notification(nid, cfg):
    if not isinstance(cfg, dict):
        return None
    out = {k: copy.deepcopy(cfg[k]) for k in _NOTIFICATION_KEYS if k in cfg}
    if nid == 'General':
        out.pop('type', None)
    return out


def _clean_versions(versions, scraper_ids):
    if not isinstance(versions, dict):
        return {}
    vschema = SETTINGS_SCHEMA.get('Scraping', {}).get('versions', {}).get('schema', {})
    allowed = set(vschema) | {'display_name'}
    out = {}
    for name, v in versions.items():
        if not isinstance(v, dict) or not str(name).strip():
            continue
        out[name] = {k: copy.deepcopy(x) for k, x in v.items()
                     if k in allowed or k in scraper_ids}
    return out


def sanitize(config, groups=None):
    """Return the portable part of a config dict, limited to `groups`.

    Used for both export and import, so whatever a file claims, only these
    keys can ever be applied."""
    groups = set(GROUP_IDS if groups is None else groups) & set(GROUP_IDS)
    out = {}
    if not isinstance(config, dict):
        return out

    for section, rule in _SECTION_RULES.items():
        data = config.get(section)
        if not isinstance(data, dict):
            continue
        sec = {}
        for key, value in data.items():
            if _group_of(section, key) not in groups or not _key_travels(section, key, rule):
                continue
            sec[key] = copy.deepcopy(value)
        if sec:
            out[section] = sec

    scrapers = config.get('Scrapers') if isinstance(config.get('Scrapers'), dict) else {}
    if 'scrapers' in groups:
        cleaned = {sid: c for sid, c in ((sid, _clean_scraper(sid, cfg)) for sid, cfg in scrapers.items()) if c}
        if cleaned:
            out['Scrapers'] = cleaned
    if 'Scraping' in out and 'versions' in out['Scraping']:
        out['Scraping']['versions'] = _clean_versions(out['Scraping']['versions'], set(scrapers))

    if 'content_sources' in groups and isinstance(config.get('Content Sources'), dict):
        cleaned = {sid: c for sid, c in ((sid, _clean_content_source(sid, cfg))
                                         for sid, cfg in config['Content Sources'].items()) if c}
        if cleaned:
            out['Content Sources'] = cleaned

    if 'notifications' in groups and isinstance(config.get('Notifications'), dict):
        cleaned = {nid: c for nid, c in ((nid, _clean_notification(nid, cfg))
                                         for nid, cfg in config['Notifications'].items()) if c}
        if cleaned:
            out['Notifications'] = cleaned
    return out


def build_export(groups=None, app_version=None):
    groups = [g for g in (groups or GROUP_IDS) if g in GROUP_IDS]
    return {
        'format': FORMAT,
        'format_version': FORMAT_VERSION,
        'app_version': app_version,
        'exported_at': datetime.now().isoformat(timespec='seconds'),
        'groups': groups,
        'settings': sanitize(load_config(), groups),
    }


def parse_import(payload):
    """Validate an uploaded export. Returns (settings, groups_in_file) or raises ValueError."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:
            raise ValueError('The file is not valid JSON.')
    if not isinstance(payload, dict) or payload.get('format') != FORMAT:
        raise ValueError('This is not a Clyde settings export.')
    try:
        fv = int(payload.get('format_version') or 0)
    except (TypeError, ValueError):
        fv = 0
    if fv < 1 or fv > FORMAT_VERSION:
        raise ValueError('This export was made by a newer cli_debrid. Update first, then import it.')
    settings = payload.get('settings')
    if not isinstance(settings, dict):
        raise ValueError('The file has no settings in it.')
    file_groups = [g for g in (payload.get('groups') or GROUP_IDS) if g in GROUP_IDS]
    return settings, file_groups


def _fit(old, new):
    """(ok, value): `new` made to fit where `old` is now, or ok=False.

    Numbers saved as text ("30") and as numbers (30) both occur, so those are
    converted to match what this install has; any other change of kind is
    refused."""
    if old is None or new is None:
        return True, new
    if isinstance(old, bool) or isinstance(new, bool):
        return (isinstance(old, bool) and isinstance(new, bool)), new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return True, new
    if isinstance(old, str) and isinstance(new, (int, float)):
        return True, str(new)
    if isinstance(old, (int, float)) and isinstance(new, str):
        try:
            num = float(new)
        except ValueError:
            return False, new
        return True, (int(num) if isinstance(old, int) and num.is_integer() else num)
    for kind in (str, (list, tuple), dict):
        if isinstance(old, kind) and isinstance(new, kind):
            return True, new
    return False, new


def _source_identity(cfg):
    return tuple(json.dumps(cfg.get(k), sort_keys=True) for k in _SOURCE_IDENTITY_KEYS)


def _find_same_source(local_section, cfg):
    """Local ID of the content source that is the same list as `cfg`, if any."""
    ident = _source_identity(cfg)
    for lid, lcfg in local_section.items():
        if isinstance(lcfg, dict) and lcfg.get('type') == cfg.get('type') \
                and _source_identity(lcfg) == ident:
            return lid
    return None


def _free_id(local_section, stype):
    n = 1
    while f'{stype}_{n}' in local_section:
        n += 1
    return f'{stype}_{n}'


def _instance_merge(local_section, incoming, section, final_versions):
    """Merge scrapers/content sources/notification channels into this install.

    Scrapers and notification channels match by ID (a scraper ID carries its
    name, e.g. NZBGeek_1). Content source IDs are just numbered per type, so
    a source matches an existing one only when it is the same list;
    otherwise it is added under a free ID and never overwrites another list."""
    notes = []
    for iid, cfg in incoming.items():
        if not isinstance(cfg, dict):
            continue
        cfg = copy.deepcopy(cfg)
        needs_setup = bool(cfg.pop('needs_setup', False))
        target_id = iid
        if section == 'Content Sources':
            if 'versions' in cfg:
                cfg['versions'] = _fit_versions(cfg['versions'], final_versions)
            target_id = _find_same_source(local_section, cfg) or _free_id(local_section, cfg.get('type') or iid.rsplit('_', 1)[0])
        existing = local_section.get(target_id)
        if isinstance(existing, dict):
            if existing.get('type') and cfg.get('type') and existing['type'] != cfg['type']:
                notes.append(f'{section}: {iid} is a different type here, left alone')
                continue
            for k, v in cfg.items():
                ok, v = _fit(existing.get(k), v)
                if ok:
                    existing[k] = v
            continue
        if not (section == 'Notifications' and iid == 'General'):
            cfg['enabled'] = False
            if needs_setup:
                notes.append(f'{section}: {target_id} added turned off. Add its URL/key, then turn it on.')
            else:
                notes.append(f'{section}: {target_id} added turned off. Turn it on to use it.')
        local_section[target_id] = cfg
    return notes


def _fit_versions(value, final_versions):
    """Keep only version names that will exist after the import."""
    names = list(final_versions)
    if isinstance(value, dict):
        kept = {k: v for k, v in value.items() if k in final_versions}
        return kept or ({names[0]: True} if names else {})
    if isinstance(value, list):
        kept = [v for v in value if v in final_versions]
        return kept or names[:1]
    return value


def merge(local, incoming_settings, groups):
    """Return (new_config, notes): `local` with the portable `incoming` applied."""
    new = copy.deepcopy(local)
    clean = sanitize(incoming_settings, groups)
    notes = []
    version_fields = set(SETTINGS_SCHEMA.get('Scraping', {}).get('versions', {}).get('schema', {})) | {'display_name'}
    local_scrapers = local.get('Scrapers') if isinstance(local.get('Scrapers'), dict) else {}
    enabled_scrapers = {sid for sid, cfg in local_scrapers.items()
                        if isinstance(cfg, dict) and cfg.get('enabled')}

    for section, data in clean.items():
        if section in _INSTANCE_SECTIONS:
            continue
        target = new.setdefault(section, {})
        if not isinstance(target, dict):
            continue
        for key, value in data.items():
            if section == 'Scraping' and key == 'versions':
                versions = target.setdefault('versions', {})
                for name, vcfg in value.items():
                    # Per-scraper scores only for scrapers that are on here: the app
                    # strips scores of disabled scrapers, and imported scrapers arrive
                    # off, so any other score would come back on every import.
                    vcfg = {k: v for k, v in vcfg.items()
                            if k in version_fields or k in enabled_scrapers}
                    cur = versions.get(name)
                    versions[name] = {**cur, **vcfg} if isinstance(cur, dict) else vcfg
                continue
            if section == 'Reverse Parser' and key == 'version_terms' and isinstance(value, dict):
                cur = target.get('version_terms')
                target['version_terms'] = {**cur, **value} if isinstance(cur, dict) else value
                continue
            if section == 'Reverse Parser' and key == 'version_order' and isinstance(value, list):
                cur = target.get('version_order') if isinstance(target.get('version_order'), list) else []
                target['version_order'] = list(value) + [v for v in cur if v not in value]
                continue
            ok, value = _fit(target.get(key), value)
            if not ok:
                notes.append(f'{section}: {key} skipped (wrong kind of value)')
                continue
            target[key] = value

    final_versions = new.get('Scraping', {}).get('versions', {}) or {}
    for section in ('Scrapers', 'Content Sources', 'Notifications'):
        if section in clean:
            target = new.setdefault(section, {})
            notes += _instance_merge(target, clean[section], section, final_versions)
    return new, notes


_MISSING = object()


def _flatten(obj, prefix=()):
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            yield from _flatten(v, prefix + (str(k),))
    else:
        yield prefix, obj


def diff(old, new):
    """Changed leaf values, as [{'path', 'group', 'old', 'new'}]."""
    old_flat = dict(_flatten(old))
    changes = []
    for path, value in _flatten(new):
        before = old_flat.get(path, _MISSING)
        if before is not _MISSING and before == value:
            continue
        section = path[0] if path else ''
        if section in _INSTANCE_SECTIONS:
            group = _INSTANCE_SECTIONS[section]
        elif section in _SECTION_RULES and len(path) > 1:
            group = _group_of(section, path[1])
        else:
            group = ''
        changes.append({
            'path': ' › '.join(path),
            'group': group,
            'old': None if before is _MISSING else before,
            'new': value,
            'added': before is _MISSING,
        })
    return changes


def preview(payload, groups):
    settings, file_groups = parse_import(payload)
    groups = [g for g in groups if g in file_groups]
    local = load_config()
    new, notes = merge(local, settings, groups)
    return {'changes': diff(local, new), 'notes': notes,
            'file_groups': file_groups, 'groups': groups}


def backup_dir():
    path = os.path.join(get_config_dir(), 'settings_backups')
    os.makedirs(path, exist_ok=True)
    return path


def apply(payload, groups):
    """Back up the current config, then save the merged one. Returns a summary."""
    settings, file_groups = parse_import(payload)
    groups = [g for g in groups if g in file_groups]
    if not groups:
        raise ValueError('Pick at least one section to import.')
    local = load_config()
    new, notes = merge(local, settings, groups)
    changes = diff(local, new)
    if not changes:
        return {'changed': 0, 'notes': notes, 'backup': None}

    name = f"config-before-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with open(os.path.join(backup_dir(), name), 'w', encoding='utf-8') as fh:
        json.dump(local, fh, indent=2)
    save_config(new)
    logging.info(f"Settings import: {len(changes)} value(s) changed in {', '.join(groups)}; backup {name}")
    return {'changed': len(changes), 'notes': notes, 'backup': name}
