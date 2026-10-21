#!/usr/bin/env python3
"""
nzbdav migration doctor for cli-debrid.

Preflight checker that encodes the things you otherwise learn the hard way when
switching cli-debrid's usenet backend from cli_mount to NzbDAV. It is read-only
by default and works for ANY cli-debrid install (it reads your config and probes
your nzbdav), not one specific setup.

What it checks
--------------
  1. nzbdav reachability + version (SAB API at /api?mode=version)
  2. nzbdav categories: which categories cli-debrid will route grabs into are
     MISSING from nzbdav's api.categories (missing cats => nzbdav rejects the
     submit => items loop in Wanted). Optional --fix adds them via the nzbdav
     sqlite DB.
  3. cli-debrid 'Usenet Provider' config sanity (provider/url/api_token/
     download_folder/mounted_file_location).
  4. The nzbdav mount path is visible from where cli-debrid runs (folder lookups
     walk the filesystem — nzbdav has no /browse API).
  5. File-management mode note (Plex vs Symlinked) for subtitle sidecar writes.

What it deliberately does NOT do
--------------------------------
  Bulk re-import of your existing cli_mount library into nzbdav. In practice that
  is unreliable: cli_mount trims old .nzb files (a large fraction are not
  replayable), replays can double-nest folders, and a hard mount-swap can make
  Plex purge "missing" items from its DB. The robust path is to run nzbdav in
  PARALLEL and let new grabs fill it, rather than a big-bang replay. This tool
  helps you get that parallel setup correct; it does not move data.

Usage
-----
  # auto-detect cli-debrid config, probe nzbdav from the config values:
  python3 nzbdav_migrate.py

  # explicit:
  python3 nzbdav_migrate.py --url http://192.168.1.x:3000 --apikey KEY

  # also add any missing categories to nzbdav (needs the nzbdav sqlite db path):
  python3 nzbdav_migrate.py --nzbdav-db /path/to/nzbdav/config/db.sqlite --fix

Exit code 0 = ready, 1 = issues found.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

# Categories cli-debrid's title heuristic can route video grabs into, plus the
# catch-all fallback. These must exist in nzbdav or submits are rejected. 'music'
# is included as recommended because the heuristic can emit it, though cli-debrid
# rarely grabs music (Lidarr usually owns that).
DEFAULT_REQUIRED = [
    'movies', 'shows',
    'movies_1080p', 'shows_1080p',
    'movies_2160p', 'shows_2160p',
    'movies_1080p_remux', 'movies_2160p_remux',
    'anime_movies', 'anime_shows',
    '__unplayable__',
]
RECOMMENDED = ['music']

# Common cli-debrid config locations (host or in-container).
CONFIG_CANDIDATES = [
    '/user/config/config.json',
    os.path.expanduser('~/.config/cli_debrid/config.json'),
    './config.json',
]

GREEN, RED, YEL, DIM, RST = '\033[32m', '\033[31m', '\033[33m', '\033[2m', '\033[0m'
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = RST = ''


def ok(m):   print(f'{GREEN}  OK {RST} {m}')
def bad(m):  print(f'{RED} FAIL{RST} {m}')
def warn(m): print(f'{YEL} WARN{RST} {m}')
def info(m): print(f'{DIM}  -  {m}{RST}')


def http_json(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': 'nzbdav-migrate/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def load_config(path_arg):
    paths = [path_arg] if path_arg else CONFIG_CANDIDATES
    for p in paths:
        if p and os.path.isfile(p):
            try:
                with open(p) as f:
                    return json.load(f), p
            except Exception as e:
                warn(f'Could not parse config {p}: {e}')
    return None, None


def sab(url, key, mode, **extra):
    q = {'mode': mode, 'apikey': key}
    q.update(extra)
    return http_json(f'{url.rstrip("/")}/api?{urllib.parse.urlencode(q)}')


def get_categories(url, key):
    try:
        data = sab(url, key, 'get_cats')
        cats = data.get('categories', [])
        return [c if isinstance(c, str) else c.get('name', '') for c in cats]
    except Exception as e:
        bad(f'get_cats failed: {e}')
        return None


def add_categories_to_db(db_path, missing):
    """Append missing categories to nzbdav's ConfigItems.api.categories. Returns True on change."""
    import sqlite3
    if not os.path.isfile(db_path):
        bad(f'nzbdav db not found: {db_path}')
        return False
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT ConfigValue FROM ConfigItems WHERE ConfigName='api.categories'")
        row = cur.fetchone()
        current = [c.strip() for c in (row[0] if row else '').split(',') if c.strip()]
        to_add = [c for c in missing if c not in current]
        if not to_add:
            info('All required categories already present in DB.')
            return False
        new_val = ','.join(current + to_add)
        con.execute("UPDATE ConfigItems SET ConfigValue=? WHERE ConfigName='api.categories'", (new_val,))
        con.commit()
        ok(f'Added to nzbdav DB: {", ".join(to_add)}')
        warn('Restart nzbdav for the new categories to take effect (docker restart nzbdav).')
        return True
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description='nzbdav migration doctor for cli-debrid')
    ap.add_argument('--config', help='Path to cli-debrid config.json (auto-detected if omitted)')
    ap.add_argument('--url', help='nzbdav base URL (overrides config)')
    ap.add_argument('--apikey', help='nzbdav SAB api key (overrides config)')
    ap.add_argument('--categories', help='Comma-separated required categories (overrides default)')
    ap.add_argument('--nzbdav-db', help='Path to nzbdav sqlite db (enables --fix)')
    ap.add_argument('--fix', action='store_true', help='Add missing categories to the nzbdav DB')
    args = ap.parse_args()

    issues = 0
    print('== nzbdav migration doctor ==\n')

    cfg, cfg_path = load_config(args.config)
    up = (cfg or {}).get('Usenet Provider', {}) if cfg else {}
    if cfg_path:
        info(f'cli-debrid config: {cfg_path}')
    else:
        warn('No cli-debrid config found — relying on --url/--apikey only.')

    url = args.url or up.get('url', '')
    key = args.apikey or up.get('api_token', '')
    required = ([c.strip() for c in args.categories.split(',') if c.strip()]
                if args.categories else list(DEFAULT_REQUIRED))
    dl_folder = up.get('download_folder', '')
    if dl_folder and dl_folder not in required:
        required.append(dl_folder)

    # Also include tag categories from content sources
    try:
        content_sources = (cfg or {}).get('Content Sources', {}) if cfg else {}
        for source_id, source_cfg in content_sources.items():
            if isinstance(source_cfg, dict):
                for tag in source_cfg.get('tags', []):
                    t = str(tag).strip().lower().replace(' ', '_')
                    if t and t not in required:
                        required.append(t)
                        info(f'  + tag category from content source {source_id!r}: {t!r}')
    except Exception as e:
        warn(f'Could not read tag categories from config: {e}')

    # --- 1. config sanity --------------------------------------------------
    print('\n[1] cli-debrid Usenet Provider config')
    if cfg:
        if up.get('provider') == 'nzbdav':
            ok("provider = nzbdav")
        else:
            bad(f"provider = {up.get('provider')!r} (set it to 'nzbdav')"); issues += 1
        if up.get('enabled'):
            ok('enabled = true')
        else:
            warn('enabled = false (usenet downloads are off)')
        if not url:
            bad('url is empty'); issues += 1
        if not key:
            warn('api_token is empty (ok only if nzbdav auth is disabled)')
        mfl = up.get('mounted_file_location', '')
        if mfl:
            ok(f'mounted_file_location = {mfl}')
            if os.path.isdir(mfl) or os.path.isdir(os.path.join(mfl, 'content')):
                ok(f'mount path visible from here ({mfl})')
            else:
                warn(f'mount path not visible from where this script runs: {mfl} '
                     '(fine if you run the doctor outside the cli-debrid container; '
                     'inside the container it must exist and contain content/<cat>/)')
        else:
            warn("mounted_file_location empty — nzbdav folder lookups need it "
                 "(recommend e.g. /mnt/nzbdav). A trailing /__all__ is stripped automatically.")
        fm = (cfg.get('File Management', {}) or {}).get('file_collection_management', '')
        info(f'file_collection_management = {fm or "unknown"}')
        if fm and fm != 'Symlinked/Local':
            info('Plex mode: subtitle sidecars are written next to the media file, '
                 'which is read-only on a WebDAV/rclone mount. Subtitle download '
                 'reliably writes only in Symlinked/Local mode. (Provider-independent.)')
    else:
        info('(skipped — no config)')

    # --- 2. nzbdav reachability -------------------------------------------
    print('\n[2] nzbdav reachability')
    if not url:
        bad('No nzbdav URL — pass --url or set Usenet Provider.url'); print(); return 1
    try:
        v = sab(url, key, 'version')
        if v.get('version'):
            ok(f'nzbdav reachable at {url} (version {v["version"]})')
        else:
            bad(f'unexpected version response: {v}'); issues += 1
    except Exception as e:
        bad(f'cannot reach nzbdav at {url}: {e}'); issues += 1
        print(); return 1

    # --- 3. categories -----------------------------------------------------
    print('\n[3] categories cli-debrid needs')
    cats = get_categories(url, key)
    if cats is None:
        issues += 1
    else:
        present = set(cats)
        missing = [c for c in required if c not in present]
        for c in required:
            (ok if c in present else bad)(f'{c}{"" if c in present else "  <-- MISSING"}')
        for c in RECOMMENDED:
            if c not in present:
                warn(f'{c} (recommended; the title heuristic can emit it)')
        if missing:
            issues += 1
            print()
            warn('Missing categories will cause nzbdav to reject those grabs.')
            if args.nzbdav_db and args.fix:
                add_categories_to_db(args.nzbdav_db, missing + [c for c in RECOMMENDED if c not in present])
            else:
                info('Add them with --nzbdav-db <db.sqlite> --fix, or manually:')
                joined = ','.join(missing)
                info(f"  sqlite3 <nzbdav>/db.sqlite \"UPDATE ConfigItems SET "
                     f"ConfigValue=ConfigValue||',{joined}' WHERE ConfigName='api.categories'\"")
                info('  docker restart nzbdav')

    # --- 4. Plex guidance --------------------------------------------------
    print('\n[4] Plex library paths (manual)')
    info('Add the nzbdav category folders to your existing Plex libraries so grabs are visible:')
    info('  Movies -> <nzbdav-mount>/content/movies and /content/movies_1080p_264')
    info('  Shows  -> <nzbdav-mount>/content/shows  and /content/shows_1080p_264')
    info('Disable Plex auto-scan on these FUSE paths (inotify does not propagate); cli-debrid triggers scans itself.')

    print()
    if issues:
        bad(f'{issues} issue(s) found — resolve the FAIL lines above, then re-run.')
        return 1
    ok('No blocking issues — cli-debrid is ready to use nzbdav.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
