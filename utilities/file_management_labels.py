"""Display names for File Management modes.

The stored value of File Management.file_collection_management stays 'Plex' or
'Symlinked/Local' (configs, code and checks everywhere compare against them).
'Plex' is shown as "Direct Mount": the media server (Plex, Silo, Jellyfin or Emby) reads the
mount directly, instead of a symlink folder.
"""

DIRECT_MOUNT = 'Direct Mount'

FILE_MANAGEMENT_LABELS = {
    'Plex': DIRECT_MOUNT,
    'Symlinked/Local': 'Symlinked/Local',
}


def file_management_label(value) -> str:
    """The name to show for a stored file_collection_management value."""
    return FILE_MANAGEMENT_LABELS.get(value, value if value else '')
