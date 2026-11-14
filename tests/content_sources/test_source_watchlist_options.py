"""Watchlist removal and partial-Seerr options are per content source now."""
import unittest
from unittest import mock

from utilities import source_options_migration as mig


class MigrationTests(unittest.TestCase):
    def _run(self, config):
        saved = []
        with mock.patch.object(mig, 'load_config', return_value=config), \
                mock.patch.object(mig, 'save_config', side_effect=saved.append):
            mig.migrate_watchlist_options_to_sources()
        return saved

    def test_global_values_copied_to_every_matching_source(self):
        config = {
            'Debug': {'plex_watchlist_removal': True, 'plex_watchlist_keep_series': False,
                      'trakt_watchlist_removal': True, 'allow_partial_overseerr_requests': True},
            'Content Sources': {
                'My Plex Watchlist_1': {'type': 'My Plex Watchlist'},
                'Other Plex Watchlist_2': {'type': 'Other Plex Watchlist'},
                'My Plex RSS Watchlist_1': {'type': 'My Plex RSS Watchlist'},
                'Trakt Watchlist_1': {'type': 'Trakt Watchlist'},
                'Overseerr_1': {'type': 'Overseerr'},
                'Overseerr_2': {'type': 'Overseerr'},
                'MDBList_1': {'type': 'MDBList'},
            },
        }
        saved = self._run(config)
        self.assertEqual(len(saved), 1)
        cs = config['Content Sources']
        for sid in ('My Plex Watchlist_1', 'Other Plex Watchlist_2', 'My Plex RSS Watchlist_1'):
            self.assertIs(cs[sid]['remove_when_collected'], True)
            self.assertIs(cs[sid]['keep_series'], False)
        self.assertIs(cs['Trakt Watchlist_1']['remove_when_collected'], True)
        self.assertNotIn('keep_series', cs['Trakt Watchlist_1'])  # global never set
        self.assertIs(cs['Overseerr_1']['allow_partial_requests'], True)
        self.assertIs(cs['Overseerr_2']['allow_partial_requests'], True)
        self.assertEqual(cs['MDBList_1'], {'type': 'MDBList'})
        self.assertEqual(config['Debug'], {})

    def test_existing_source_value_wins(self):
        config = {'Debug': {'plex_watchlist_removal': True},
                  'Content Sources': {'My Plex Watchlist_1': {'type': 'My Plex Watchlist', 'remove_when_collected': False}}}
        self._run(config)
        self.assertIs(config['Content Sources']['My Plex Watchlist_1']['remove_when_collected'], False)

    def test_nothing_to_move_saves_nothing(self):
        self.assertEqual(self._run({'Debug': {}, 'Content Sources': {'Overseerr_1': {'type': 'Overseerr'}}}), [])


if __name__ == '__main__':
    unittest.main()
