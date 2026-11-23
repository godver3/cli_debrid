#!/usr/bin/env python3
"""Plex-side handling of chosen episode orders (utilities/plex_order_metadata.py)."""

import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import plex_order_metadata as pom


class Field:
    def __init__(self, name, locked=True):
        self.name, self.locked = name, locked


class Ep:
    def __init__(self, s, e, title, summary='', date=None, locked=()):
        self.seasonNumber, self.index, self.title, self.summary = s, e, title, summary
        self.originallyAvailableAt = datetime.datetime.strptime(date, '%Y-%m-%d') if date else None
        self.fields = [Field(f) for f in locked]
        self.calls = []

    def batchEdits(self):
        self.calls.append('batch')

    def editTitle(self, t, locked):
        self.calls.append(('title', t, locked))

    def editSummary(self, t, locked):
        self.calls.append(('summary', t, locked))

    def editOriginallyAvailable(self, d, locked):
        self.calls.append(('date', d, locked))

    def saveEdits(self):
        self.calls.append('save')

    def edit(self, **kw):
        self.calls.append(('edit', kw))


class Show:
    title = 'Firefly'

    def __init__(self, eps, ordering='aired'):
        self.eps, self.refreshed = eps, False
        self.ordering = ordering

    def preferences(self):
        return [mock.Mock(id='showOrdering', value=self.ordering,
                          enumValues={'tmdbAiring': 'TMDB', 'aired': 'TheTVDB', 'tvdbDvd': 'DVD'})]

    def editAdvanced(self, **kw):
        self.ordering = kw.get('showOrdering', self.ordering)

    def episodes(self):
        return self.eps

    def refresh(self):
        self.refreshed = True


class PlexOrderMetadataTests(unittest.TestCase):
    def test_release_unlocks_and_refreshes(self):
        a, b = Ep(1, 1, 'x', locked=('title', 'summary')), Ep(1, 2, 'y')
        show = Show([a, b])
        self.assertEqual(pom.release_plex_show(show), 1)
        self.assertEqual(a.calls, [('edit', {'title.locked': 0, 'summary.locked': 0})])
        self.assertTrue(show.refreshed)

    def test_without_nfo_files_an_order_change_only_releases_old_locks(self):
        # 0.8.7: no more locking; locks left by 0.8.0-0.8.6 are released
        for order in ('tvdb:dvd', 'default'):
            locked, plain = Ep(1, 1, 'x', locked=('title',)), Ep(1, 2, 'y')
            show = Show([locked, plain])
            with mock.patch.object(pom, '_plex_show', return_value=(show, None)):
                self.assertIn('unlocked for 1', pom.sync_show('tt1', order))
            self.assertEqual(locked.calls, [('edit', {'title.locked': 0})])
            self.assertEqual(plain.calls, [], 'nothing written or locked')
            self.assertEqual(show.ordering, 'aired', 'Plex ordering untouched')
        with mock.patch.object(pom, '_plex_show', return_value=(Show([Ep(1, 1, 'x')]), None)):
            self.assertEqual(pom.sync_show('tt1', 'tvdb:dvd'), '')
        with mock.patch.object(pom, '_plex_show', return_value=(None, 'show not in Plex yet')):
            self.assertEqual(pom.sync_show('tt1', 'tvdb:dvd'), '')

    def test_with_nfo_files_an_order_change_releases_instead_of_locking(self):
        # NFOs carry the order: locked titles would stop Plex taking them
        locked = Ep(1, 1, 'wrong', locked=('title',))
        show = Show([locked])
        with mock.patch.object(pom, '_nfo_mode', return_value=True), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)), \
                mock.patch('cli_battery.app.episode_order.fetch_seasons_for_order') as fetch:
            note = pom.sync_show('tt1', 'tvdb:dvd')
        fetch.assert_not_called()
        self.assertEqual(locked.calls, [('edit', {'title.locked': 0})])
        self.assertTrue(show.refreshed)
        self.assertIn("NFO", note)

    def test_with_nfo_files_unlocked_show_is_still_refreshed(self):
        show = Show([Ep(1, 1, 'x')])
        with mock.patch.object(pom, '_nfo_mode', return_value=True), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            pom.sync_show('tt1', 'default')
        self.assertTrue(show.refreshed)

    def test_nfo_hand_over_numbers_the_show_by_tmdb(self):
        show = Show([Ep(1, 1, 'x', locked=('title',))], ordering='tvdbDvd')
        with mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            note = pom.release_for_nfo('tt1')
        self.assertEqual(show.ordering, 'tmdbAiring')
        self.assertIn('numbered by TMDB', note)
        self.assertTrue(show.refreshed)

    def test_new_episodes_of_shows_without_nfo_files_are_left_alone(self):
        with mock.patch.object(pom, '_nfo_mode', return_value=False), \
                mock.patch.object(pom, '_plex_show') as find:
            self.assertEqual(pom.pin_new_nfo_shows(['tt1']), {})
        find.assert_not_called()

    def test_new_episodes_of_an_nfo_show_pin_tmdb_ordering(self):
        show = Show([], ordering='aired')      # new to Plex: the library's TheTVDB ordering
        with mock.patch.object(pom, '_nfo_mode', return_value=True), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            notes = pom.pin_new_nfo_shows(['tt1', None, 'tt1'])
        self.assertEqual((show.ordering, show.refreshed), ('tmdbAiring', True))
        self.assertEqual(notes, {'tt1': 'numbered by TMDB in Plex'})


if __name__ == '__main__':
    unittest.main()
