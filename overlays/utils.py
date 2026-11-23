"""
Overlay System Utilities

Shared helpers for the overlay system.
"""

from utilities.settings import get_setting


def is_jellyfin_mode() -> bool:
    """
    Return True when overlays should use Jellyfin/Emby instead of Plex.

    Jellyfin/Emby overlay mode is active when:
      file_collection_management == 'Symlinked/Local'
      AND media_server_type == 'jellyfin'

    In all other cases (Plex direct, Symlinked/Local + Plex) the existing
    Plex overlay path is used.
    """
    fcm = get_setting('File Management', 'file_collection_management', 'Plex')
    mst = get_setting('File Management', 'media_server_type', 'plex')
    return fcm == 'Symlinked/Local' and mst == 'jellyfin'


def overlays_active() -> bool:
    """Overlays are on and the media server takes them. Silo draws its own
    live badges in every client and never stores burned-in posters, so
    cli_debrid's overlays stay off there."""
    if not get_setting('Overlay Settings', 'overlays_enabled', False):
        return False
    from utilities.silo_functions import is_silo_selected
    return not is_silo_selected()


def get_jellyfin_url() -> str:
    """Return the configured Jellyfin/Emby server URL (stripped of trailing slash)."""
    return get_setting('Debug', 'emby_jellyfin_url', default='').rstrip('/')


def get_jellyfin_token() -> str:
    """Return the configured Jellyfin/Emby API token."""
    return get_setting('Debug', 'emby_jellyfin_token', default='')
