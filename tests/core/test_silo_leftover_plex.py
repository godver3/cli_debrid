"""With Silo as the media server: removals rescan by full path, and a Plex URL
left over from before Silo isn't used (Now Playing, episode order sync)."""
import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from utilities import silo_functions as silo
from utilities import plex_removal_cache as prc
from utilities import mount_now_playing as np

SILO = {
    ('File Management', 'file_collection_management'): 'Symlinked/Local',
    ('File Management', 'media_server_type'): 'silo',
    ('Debug', 'silo_url'): 'http://silo:8090',
    ('Debug', 'silo_api_key'): 'sa_test',
    ('File Management', 'plex_url_for_symlink'): 'http://old-plex:32400',
    ('File Management', 'plex_token_for_symlink'): 'old-token',
    ('Plex', 'url'): 'http://old-plex:32400',
    ('Plex', 'token'): 'old-token',
}


def _get(values):
    return lambda section, key=None, default=None: values.get((section, key), default)


class SiloRemovalTest(unittest.TestCase):
    def test_bare_file_name_is_not_rescanned(self):
        with mock.patch.object(silo, 'scan_path') as scan:
            self.assertFalse(silo.remove_file_from_silo('Show', 'Show.S01E01.mkv', 'Pilot'))
        scan.assert_not_called()

    def test_cache_rescans_gone_full_path_now(self):
        with tempfile.TemporaryDirectory() as d:
            gone = os.path.join(d, 'Movies', 'Film (2020)', 'Film.mkv')
            here = os.path.join(d, 'here.mkv')
            open(here, 'w').close()
            with mock.patch.object(silo, 'get_setting', _get(SILO)), \
                 mock.patch.object(silo, 'remove_file_from_silo') as rm, \
                 mock.patch.object(prc, 'get_setting', lambda *a, **k: True), \
                 mock.patch.object(prc, '_load_cache', lambda: {}), \
                 mock.patch.object(prc, '_save_cache', lambda c: None):
                prc.cache_plex_removal('Film', gone)
                prc.cache_plex_removal('Film', here)          # still on disk: nothing to drop
                prc.cache_plex_removal('Film', 'Film.mkv')    # bare name: no folder
            rm.assert_called_once_with('Film', gone, None)

    def test_cache_without_silo_does_not_rescan(self):
        plex = {**SILO, ('File Management', 'media_server_type'): 'plex'}
        with mock.patch.object(silo, 'get_setting', _get(plex)), \
             mock.patch.object(silo, 'remove_file_from_silo') as rm, \
             mock.patch.object(prc, 'get_setting', lambda *a, **k: True), \
             mock.patch.object(prc, '_load_cache', lambda: {}), \
             mock.patch.object(prc, '_save_cache', lambda c: None):
            prc.cache_plex_removal('Film', '/gone/Film.mkv')
        rm.assert_not_called()


class LeftoverPlexTest(unittest.TestCase):
    def test_now_playing_ignores_leftover_plex_under_silo(self):
        url, token, _, _ = np.plex_server_source(_get(SILO))
        self.assertEqual((url, token), ('', ''))

    def test_now_playing_keeps_mount_page_plex_under_silo(self):
        vals = {**SILO, ('Built-in Mount', 'plex_url'): 'http://mine:32400', ('Built-in Mount', 'plex_token'): 't'}
        url, token, src, _ = np.plex_server_source(_get(vals))
        self.assertEqual((url, token, src), ('http://mine:32400', 't', 'Mount page'))

    def test_now_playing_unchanged_without_silo(self):
        vals = {**SILO, ('File Management', 'media_server_type'): 'plex'}
        url, token, src, _ = np.plex_server_source(_get(vals))
        self.assertEqual((url, src), ('http://old-plex:32400', 'Settings → Plex'))

    def test_episode_order_sync_skips_plex_under_silo(self):
        from utilities import episode_order_change as eoc
        with mock.patch.object(silo, 'get_setting', _get(SILO)), \
             mock.patch('plexapi.server.PlexServer') as server:
            self.assertIsNone(eoc._plex_server())
        server.assert_not_called()


if __name__ == '__main__':
    unittest.main()
