"""Which media server Direct Mount (file_collection_management 'Plex') uses.

File Management.plex_mode_media_server picks it: 'plex' (default), 'silo' or
'jellyfin' (Jellyfin or Emby; both speak the same API). Plex-only features
(labels, overlays, collections, Plex watch history, reported issues, subtitle
upload to the Plex item) are off unless it is Plex.
"""
from typing import Optional

from utilities.settings import get_setting

SERVERS = ('plex', 'silo', 'jellyfin')


def direct_mount_server(get=None) -> Optional[str]:
    """'plex', 'silo' or 'jellyfin' in Direct Mount, None in Symlinked/Local.
    `get` is a get_setting-like (section, key, default) callable."""
    get = get or get_setting
    if get('File Management', 'file_collection_management', 'Plex') != 'Plex':
        return None
    server = (get('File Management', 'plex_mode_media_server', 'plex') or 'plex').lower()
    return server if server in SERVERS else 'plex'


def is_direct_mount_plex(get=None) -> bool:
    """Direct Mount with Plex as its server (the original Plex mode)."""
    return direct_mount_server(get) == 'plex'


def is_direct_mount_jellyfin(get=None) -> bool:
    """Direct Mount with Jellyfin or Emby as its server."""
    return direct_mount_server(get) == 'jellyfin'


def is_direct_mount_without_plex(get=None) -> bool:
    """Direct Mount whose server isn't Plex (Silo, Jellyfin or Emby): Plex's own
    connection, sections and features don't apply."""
    return direct_mount_server(get) in ('silo', 'jellyfin')


def server_label(get=None) -> str:
    return {'plex': 'Plex', 'silo': 'Silo', 'jellyfin': 'Jellyfin/Emby'}.get(direct_mount_server(get) or '', 'Plex')
