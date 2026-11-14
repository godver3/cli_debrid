"""Which Plex server URL/token each file-management mode scans with.

Plex mode's scans went through the symlink-mode fields only, so a Plex-mode
config with just Plex.url/token (older configs, or the URL edited in Settings)
never asked Plex to scan, and on a FUSE mount nothing else makes Plex look.
"""
import pytest

import database  # noqa: F401  (app import order)
from utilities.plex_functions import plex_connection_settings

MAIN = {'url': 'http://main:32400/', 'token': 'main-token'}
SYMLINK = {'plex_url_for_symlink': 'http://symlink:32400', 'plex_token_for_symlink': 'symlink-token'}


def _config(app_settings, mode, plex=None, symlink=None):
    app_settings['Plex'] = dict(plex or {})
    app_settings['File Management'] = {'file_collection_management': mode, **(symlink or {})}


@pytest.mark.parametrize('plex, symlink, expected', [
    (MAIN, SYMLINK, ('http://main:32400', 'main-token')),
    (MAIN, {}, ('http://main:32400', 'main-token')),
    ({}, SYMLINK, ('http://symlink:32400', 'symlink-token')),
    ({'url': 'http://main:32400'}, SYMLINK, ('http://symlink:32400', 'symlink-token')),  # no token
    ({}, {}, ('', '')),
])
def test_plex_mode(app_settings, plex, symlink, expected):
    _config(app_settings, 'Plex', plex, symlink)
    assert plex_connection_settings() == expected


@pytest.mark.parametrize('plex, symlink, expected', [
    (MAIN, SYMLINK, ('http://symlink:32400', 'symlink-token')),
    # Blank symlink fields mean "no Plex scans" in symlink mode, even with Plex.url set.
    (MAIN, {}, ('', '')),
])
def test_symlink_mode(app_settings, plex, symlink, expected):
    _config(app_settings, 'Symlinked/Local', plex, symlink)
    assert plex_connection_settings() == expected
