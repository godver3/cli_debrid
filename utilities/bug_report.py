"""Setup summary for the top bar's bug report: what a maintainer needs to know
before reading the logs (version, install, file management, media server,
providers, and which mount serves the files). Nothing personal: no URLs,
paths, keys or account names."""
import logging
import os
import platform
import sys

from utilities.settings import get_setting


def _install_type() -> str:
    if getattr(sys, 'frozen', False):
        return 'Windows exe' if sys.platform.startswith('win') else 'Packaged'
    if os.path.exists('/.dockerenv') or os.environ.get('container'):
        return 'Docker'
    return 'Source'


def _media_server() -> str:
    from utilities.silo_functions import silo_kind
    if silo_kind():
        return 'Silo'
    mode = get_setting('File Management', 'file_collection_management', 'Plex')
    if mode == 'Plex':
        from utilities.direct_mount import is_direct_mount_jellyfin
        return 'Jellyfin/Emby' if is_direct_mount_jellyfin() else 'Plex'
    kind = (get_setting('File Management', 'media_server_type', 'plex') or 'plex').lower()
    if kind == 'jellyfin' and get_setting('Debug', 'emby_jellyfin_url', ''):
        return 'Jellyfin/Emby'
    if kind == 'plex' and get_setting('File Management', 'plex_url_for_symlink', ''):
        return 'Plex'
    return 'None'


def _debrid() -> str:
    try:
        from debrid import get_debrid_providers
        names = [p.PROVIDER_NAME for p in get_debrid_providers()]
        return ', '.join(names) or 'None'
    except Exception:
        return 'None'


def _external_climount_version(client) -> str:
    try:
        r = client and client.base_url and __import__('requests').get(
            f'{client.base_url}/version', headers=client._headers(), timeout=5)
        if r is not None and r is not False and r.status_code == 200:
            try:
                body = r.json()
                return str(body.get('version') or body.get('Version') or '').strip() or 'unknown version'
            except ValueError:
                return r.text.strip()[:40] or 'unknown version'
        return 'not reachable'
    except Exception as e:
        logging.debug(f'[bug report] cli_mount version: {e}')
        return 'not reachable'


def mount_kind() -> str:
    """'builtin', 'external' (cli_mount) or '' (neither)."""
    from utilities import builtin_engine
    try:
        settings = {'Usenet Provider': get_setting('Usenet Provider') or {},
                    'Built-in Mount': get_setting('Built-in Mount') or {}}
        if builtin_engine.selected(settings):
            return 'builtin'
    except Exception:
        pass
    usenet = get_setting('Usenet Provider') or {}
    if usenet.get('enabled') and (usenet.get('provider') or 'climount').strip().lower() == 'climount':
        return 'external'
    return ''


def log_sources() -> list:
    """Which mount logs a bug report uploads next to cli_debrid's: 'engine'
    whenever the built-in mount runs (backend, debrid-only or alongside),
    'climount' whenever cli_mount is the usenet provider. Both can apply."""
    out = []
    try:
        from utilities import builtin_engine
        settings = {'Usenet Provider': get_setting('Usenet Provider') or {},
                    'Built-in Mount': get_setting('Built-in Mount') or {}}
        if builtin_engine.wanted(settings):
            out.append('engine')
    except Exception:
        pass
    usenet = get_setting('Usenet Provider') or {}
    if usenet.get('enabled') and (usenet.get('provider') or 'climount').strip().lower() == 'climount':
        out.append('climount')
    return out


def _usenet_and_mount():
    usenet = get_setting('Usenet Provider') or {}
    try:
        from usenet import get_usenet_provider_display_name
        usenet_name = get_usenet_provider_display_name() if usenet.get('enabled') else 'None'
    except Exception:
        usenet_name = 'None'
    kind = mount_kind()
    if kind == 'builtin':
        mount = 'Built-in mount'
        try:
            from utilities import builtin_engine
            if builtin_engine.alongside():
                mount += ' (running alongside cli_mount)'
        except Exception:
            pass
    elif kind == 'external':
        from usenet import get_usenet_client
        mount = f'External cli_mount ({_external_climount_version(get_usenet_client())})'
    else:
        mount = 'None' if usenet_name == 'None' else usenet_name
    return usenet_name, mount, kind


def build_context() -> dict:
    from utilities.version import get_app_version
    try:
        from routes.base_routes import get_current_branch
        branch = get_current_branch()
    except Exception:
        branch = 'unknown'
    usenet_name, mount, kind = _usenet_and_mount()
    rows = [
        ('Version', f'{get_app_version()} ({branch})'),
        ('Install', f'{_install_type()}, {platform.system()} {platform.machine()}'.strip(', ')),
        ('File management', get_setting('File Management', 'file_collection_management', 'Plex')),
        ('Media server', _media_server()),
        ('Debrid', _debrid()),
        ('Usenet', usenet_name),
        ('Mount', mount),
    ]
    return {'rows': [{'label': k, 'value': v} for k, v in rows], 'mount_kind': kind,
            'log_sources': log_sources()}
