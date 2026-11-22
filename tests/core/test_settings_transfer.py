"""Settings export/import: nothing secret or personal leaves, a file can only
change portable settings, and imports merge without removing or enabling."""
import copy
import json
import os
import unittest
from unittest import mock

from utilities import settings_transfer as st

SECRET = 'SUPERSECRETVALUE123'

SOURCE = {
    'Plex': {'url': 'http://192.168.1.5:32400', 'token': SECRET},
    'SSO': {'client_secret': SECRET},
    'Custom Post-Processing': {'enable_custom_script': True, 'custom_script_path': '/opt/x.sh'},
    'Debrid Provider': {'provider': 'RealDebrid', 'api_key': SECRET, 'enable_debrid_naming': True},
    'File Management': {'original_files_path': '/mnt/zurg', 'symlinked_files_path': '/media',
                        'symlink_organize_by_type': True, 'file_collection_management': 'Symlinked/Local'},
    'Debug': {'symlink_movie_template': '{title} ({year})/{title}', 'logging_level': 'DEBUG',
              'movies_folder_name': 'Films'},
    'Queue': {'wake_limit': '24', 'blacklist_duration': '30', 'content_source_priority': 'MDBList_1'},
    'Built-in Mount': {'mount_path': '/mnt/e', 'cache_dir': '/cache', 'servers': '[{"host":"x"}]',
                       'read_ahead_mb': 64, 'uid': 1000, 'debrid_download_api_keys': SECRET},
    'Subtitle Settings': {'opensubtitles_username': 'me', 'opensubtitles_password': SECRET,
                          'subdl_api_key': SECRET, 'subtitle_languages': ['en']},
    'Scraping': {'enable_upgrading': True, 'versions': {
        '1080p': {'max_resolution': '1080p', 'filter_out': ['CAM'], 'Torrentio_1': 5, 'bogus_key': 1},
    }},
    'Scrapers': {
        'Torrentio_1': {'type': 'Torrentio', 'enabled': True, 'priority': 5,
                        'opts': f'sort=qualitysize|realdebrid={SECRET}|qualityfilter=cam'},
        'NZBGeek_1': {'type': 'Newznab', 'enabled': True, 'url': 'https://api.nzbgeek.info',
                      'api_key': SECRET, 'priority': 10},
    },
    'Content Sources': {
        'MDBList_1': {'type': 'MDBList', 'enabled': True, 'urls': 'https://mdblist.com/lists/a/top',
                      'versions': ['1080p'], 'media_type': 'All'},
        'Overseerr_1': {'type': 'Overseerr', 'enabled': True, 'url': 'http://os', 'api_key': SECRET},
        'Other Plex Watchlist_1': {'type': 'Other Plex Watchlist', 'username': 'friend', 'token': SECRET},
    },
    'Notifications': {
        'General': {'enabled_content_sources': 'MDBList_1', 'notify_on': {'collected': True}},
        'Discord_1': {'type': 'Discord', 'enabled': True, 'webhook_url': SECRET,
                      'notify_on': {'collected': False}},
    },
}

LOCAL = {
    'Plex': {'url': 'http://mine', 'token': 'MINE'},
    'Debrid Provider': {'provider': 'Torbox', 'api_key': 'MINEKEY', 'enable_debrid_naming': False},
    'Queue': {'wake_limit': '10', 'blacklist_duration': 7},
    'Custom Post-Processing': {'enable_custom_script': False, 'custom_script_path': ''},
    'Scraping': {'enable_upgrading': False, 'versions': {'Default': {'max_resolution': '2160p'}}},
    'Scrapers': {'Torrentio_1': {'type': 'Torrentio', 'enabled': False, 'priority': 0, 'opts': ''}},
    'Content Sources': {'MDBList_1': {'type': 'MDBList', 'enabled': True,
                                      'urls': 'https://mdblist.com/lists/me/mine', 'versions': ['Default']}},
}


class ExportTests(unittest.TestCase):
    def test_no_secret_or_personal_value_is_exported(self):
        out = json.dumps(st.sanitize(SOURCE))
        for bad in (SECRET, '192.168.1.5', '/mnt/zurg', '/media', '/opt/x.sh', '/mnt/e', '/cache',
                    'nzbgeek.info', 'http://os', 'friend', '"host"', 'RealDebrid'):
            self.assertNotIn(bad, out)

    def test_portable_settings_are_exported(self):
        out = st.sanitize(SOURCE)
        self.assertEqual(out['Debug']['symlink_movie_template'], '{title} ({year})/{title}')
        self.assertNotIn('logging_level', out['Debug'])
        self.assertEqual(out['Built-in Mount'], {'read_ahead_mb': 64})
        self.assertEqual(out['Subtitle Settings'], {'subtitle_languages': ['en']})
        self.assertNotIn('content_source_priority', out['Queue'])
        self.assertEqual(out['Scrapers']['Torrentio_1']['opts'], 'sort=qualitysize|qualityfilter=cam')
        self.assertTrue(out['Scrapers']['NZBGeek_1']['needs_setup'])
        self.assertNotIn('enabled', out['Scrapers']['Torrentio_1'])
        self.assertEqual(set(out['Content Sources']), {'MDBList_1'})
        self.assertNotIn('enabled', out['Content Sources']['MDBList_1'])
        self.assertEqual(out['Notifications']['Discord_1'], {'type': 'Discord', 'notify_on': {'collected': False}})
        version = out['Scraping']['versions']['1080p']
        self.assertEqual(version['Torrentio_1'], 5)
        self.assertNotIn('bogus_key', version)

    def test_groups_limit_the_export(self):
        out = st.sanitize(SOURCE, ['versions'])
        self.assertEqual(set(out), {'Scraping'})
        self.assertEqual(set(out['Scraping']), {'versions'})


class ImportTests(unittest.TestCase):
    def _merge(self, incoming, groups=None):
        return st.merge(copy.deepcopy(LOCAL), incoming, groups or st.GROUP_IDS)

    def test_hostile_file_cannot_touch_personal_settings(self):
        evil = copy.deepcopy(SOURCE)
        evil['Plex'] = {'url': 'http://evil'}
        evil['Custom Post-Processing'] = {'enable_custom_script': True, 'custom_script_path': '/evil'}
        evil['Debrid Provider']['api_key'] = 'EVIL'
        evil['Scrapers']['Torrentio_1']['url'] = 'http://evil'
        new, _ = self._merge(evil)
        self.assertNotIn('evil', json.dumps(new).lower())
        self.assertEqual(new['Debrid Provider']['api_key'], 'MINEKEY')
        self.assertEqual(new['Debrid Provider']['provider'], 'Torbox')
        self.assertFalse(new['Custom Post-Processing']['enable_custom_script'])

    def test_merge_keeps_local_and_adds_new_things_turned_off(self):
        new, notes = self._merge(st.sanitize(SOURCE))
        self.assertEqual(set(new['Scraping']['versions']), {'Default', '1080p'})
        self.assertTrue(new['Debrid Provider']['enable_debrid_naming'])
        # number stored as text here vs number there: fitted to the local kind
        self.assertEqual(new['Queue']['blacklist_duration'], 30)
        # existing Torrentio keeps its on/off state; options travel
        self.assertFalse(new['Scrapers']['Torrentio_1']['enabled'])
        self.assertEqual(new['Scrapers']['Torrentio_1']['priority'], 5)
        self.assertFalse(new['Scrapers']['NZBGeek_1']['enabled'])
        self.assertNotIn('needs_setup', new['Scrapers']['NZBGeek_1'])
        # a different MDBList never overwrites the local one
        self.assertEqual(new['Content Sources']['MDBList_1']['urls'], 'https://mdblist.com/lists/me/mine')
        self.assertEqual(new['Content Sources']['MDBList_2']['urls'], 'https://mdblist.com/lists/a/top')
        self.assertFalse(new['Content Sources']['MDBList_2']['enabled'])
        self.assertFalse(new['Notifications']['Discord_1']['enabled'])
        self.assertTrue(any('NZBGeek_1' in n for n in notes))

    def test_content_source_versions_fit_this_install(self):
        new, _ = self._merge(st.sanitize(SOURCE), ['content_sources'])
        self.assertEqual(new['Content Sources']['MDBList_2']['versions'], ['Default'])

    def test_import_is_idempotent(self):
        once, _ = self._merge(st.sanitize(SOURCE))
        twice, _ = st.merge(copy.deepcopy(once), st.sanitize(SOURCE), st.GROUP_IDS)
        self.assertEqual(st.diff(once, twice), [])

    def test_version_scraper_scores_only_for_enabled_scrapers(self):
        new, _ = self._merge(st.sanitize(SOURCE), ['versions'])
        self.assertNotIn('Torrentio_1', new['Scraping']['versions']['1080p'])
        local = copy.deepcopy(LOCAL)
        local['Scrapers']['Torrentio_1']['enabled'] = True
        new, _ = st.merge(local, st.sanitize(SOURCE), ['versions'])
        self.assertEqual(new['Scraping']['versions']['1080p']['Torrentio_1'], 5)

    def test_wrong_kind_is_refused(self):
        bad = {'Queue': {'blacklist_duration': 'soon'}, 'Scraping': {'enable_upgrading': 'yes'}}
        new, notes = self._merge(bad)
        self.assertEqual(new['Queue']['blacklist_duration'], 7)
        self.assertFalse(new['Scraping']['enable_upgrading'])
        self.assertEqual(len(notes), 2)

    def test_parse_import_validates_the_wrapper(self):
        with self.assertRaises(ValueError):
            st.parse_import('not json')
        with self.assertRaises(ValueError):
            st.parse_import({'format': 'other'})
        with self.assertRaises(ValueError):
            st.parse_import({'format': st.FORMAT, 'format_version': st.FORMAT_VERSION + 1, 'settings': {}})
        settings, groups = st.parse_import({'format': st.FORMAT, 'format_version': 1,
                                            'groups': ['versions', 'nope'], 'settings': {}})
        self.assertEqual(groups, ['versions'])

    def test_apply_backs_up_then_saves(self):
        saved = {}
        payload = {'format': st.FORMAT, 'format_version': 1, 'settings': st.sanitize(SOURCE)}
        with mock.patch.object(st, 'load_config', return_value=copy.deepcopy(LOCAL)), \
                mock.patch.object(st, 'save_config', side_effect=lambda c: saved.update(c)):
            res = st.apply(payload, ['versions'])
        self.assertGreater(res['changed'], 0)
        self.assertIn('1080p', saved['Scraping']['versions'])
        backup = os.path.join(st.backup_dir(), res['backup'])
        with open(backup) as fh:
            self.assertEqual(json.load(fh), LOCAL)


if __name__ == '__main__':
    unittest.main()
