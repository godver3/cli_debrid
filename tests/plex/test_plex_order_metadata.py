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


class PinRecordTests(unittest.TestCase):
    """TMDB numbering Clyde sets for NFO shows is recorded and put back when
    the show no longer has NFO files, or NFO files / the agent are turned off."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(pom, '_pins_path', return_value=os.path.join(self.tmp.name, 'pins.json'))
        p.start()
        self.addCleanup(p.stop)

    def _show(self, ordering='tvdbDvd', key=42):
        show = Show([Ep(1, 1, 'x')], ordering=ordering)
        show.ratingKey = key
        return show

    def test_set_up_plex_agent_with_nfo_mode_off_pins_nothing(self):
        show = self._show()
        with mock.patch.object(pom, 'shows_with_chosen_order', return_value=[('tt1', 'tvdb:dvd')]), \
                mock.patch('utilities.nfo_writer.wants_show', return_value=False), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            self.assertEqual(pom.release_all_for_nfo(plex=mock.Mock()), 0)
        self.assertEqual(show.ordering, 'tvdbDvd')
        self.assertEqual(pom._load_pins(), {})

    def test_nfo_show_is_pinned_and_put_back_by_unpin_all(self):
        show = self._show()
        with mock.patch.object(pom, 'shows_with_chosen_order', return_value=[('tt1', 'tvdb:dvd')]), \
                mock.patch('utilities.nfo_writer.wants_show', return_value=True), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            self.assertEqual(pom.release_all_for_nfo(plex=mock.Mock()), 1)
        self.assertEqual(show.ordering, 'tmdbAiring')
        self.assertEqual(pom._load_pins()['42']['previous'], 'tvdbDvd')
        plex = mock.Mock(fetchItem=mock.Mock(return_value=show))
        self.assertEqual(pom.unpin_all(plex=plex), 1)
        self.assertEqual(show.ordering, 'tvdbDvd')
        self.assertEqual(pom._load_pins(), {})

    def test_second_pin_keeps_the_first_previous_ordering(self):
        show = self._show()
        pom.pin_tmdb_ordering(show, 'tt1')
        show.ordering = 'aired'   # something else changed it in between
        pom.pin_tmdb_ordering(show, 'tt1')
        self.assertEqual(pom._load_pins()['42']['previous'], 'tvdbDvd')

    def test_ordering_changed_since_is_kept(self):
        show = self._show()
        pom.pin_tmdb_ordering(show, 'tt1')
        show.ordering = 'aired'   # the user (or an order change) set another one
        self.assertEqual(pom.unpin_show(show), '')
        self.assertEqual(show.ordering, 'aired')
        self.assertEqual(pom._load_pins(), {})

    def test_show_leaving_nfo_files_on_order_change_gets_its_ordering_back(self):
        show = self._show()
        pom.pin_tmdb_ordering(show, 'tt1')
        with mock.patch.object(pom, '_nfo_mode', return_value=False), \
                mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            note = pom.sync_show('tt1', 'default')
        self.assertEqual(show.ordering, 'tvdbDvd')
        self.assertIn('put back', note)

    def test_unpin_imdb_needs_no_plex_call_for_unpinned_show(self):
        with mock.patch.object(pom, '_plex_show') as find:
            self.assertEqual(pom.unpin_imdb('tt9'), '')
        find.assert_not_called()

    def test_plex_unreachable_keeps_the_record(self):
        show = self._show()
        pom.pin_tmdb_ordering(show, 'tt1')
        plex = mock.Mock(fetchItem=mock.Mock(side_effect=ConnectionError('down')))
        self.assertEqual(pom.unpin_all(plex=plex), 0)
        self.assertIn('42', pom._load_pins())

    def test_show_gone_from_plex_drops_the_record(self):
        from plexapi.exceptions import NotFound
        show = self._show()
        pom.pin_tmdb_ordering(show, 'tt1')
        plex = mock.Mock(fetchItem=mock.Mock(side_effect=NotFound('gone')))
        self.assertEqual(pom.unpin_all(plex=plex), 0)
        self.assertEqual(pom._load_pins(), {})


if __name__ == '__main__':
    unittest.main()
