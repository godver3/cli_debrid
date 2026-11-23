#!/usr/bin/env python3
"""Chosen episode orders kept in Plex (utilities/plex_order_metadata.py)."""

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

    def __init__(self, eps):
        self.eps, self.refreshed = eps, False

    def episodes(self):
        return self.eps

    def refresh(self):
        self.refreshed = True


DVD = {1: {'episodes': {1: {'title': 'Serenity', 'overview': 'Pilot.', 'first_aired': '2002-12-20T00:00:00Z'},
                        2: {'title': 'The Train Job', 'overview': '', 'first_aired': '2002-09-20T00:00:00Z'}}}}


class PlexOrderMetadataTests(unittest.TestCase):
    def test_writes_and_locks_the_orders_metadata(self):
        wrong = Ep(1, 1, 'The Train Job', 'Heist.', '2002-09-20')
        right = Ep(1, 2, 'The Train Job', '', '2002-09-20', locked=('title', 'originallyAvailableAt'))
        extra = Ep(1, 9, 'Unknown')
        c = pom.apply_to_plex_show(Show([wrong, right, extra]), DVD)
        self.assertEqual(c, {'updated': 1, 'unchanged': 1, 'not_in_order': 1, 'errors': 0})
        self.assertEqual(wrong.calls, ['batch', ('title', 'Serenity', True), ('summary', 'Pilot.', True),
                                       ('date', '2002-12-20', True), 'save'])
        self.assertEqual(right.calls, [], 'already right and locked')
        self.assertEqual(extra.calls, [])

    def test_unlocked_but_equal_gets_locked(self):
        ep = Ep(1, 2, 'The Train Job', '', '2002-09-20')
        pom.apply_to_plex_show(Show([ep]), DVD)
        self.assertEqual(ep.calls, ['batch', ('title', 'The Train Job', True), ('date', '2002-09-20', True), 'save'])

    def test_release_unlocks_and_refreshes(self):
        a, b = Ep(1, 1, 'x', locked=('title', 'summary')), Ep(1, 2, 'y')
        show = Show([a, b])
        self.assertEqual(pom.release_plex_show(show), 1)
        self.assertEqual(a.calls, [('edit', {'title.locked': 0, 'summary.locked': 0})])
        self.assertTrue(show.refreshed)

    def test_sync_show_default_order_releases(self):
        show = Show([Ep(1, 1, 'x', locked=('title',))])
        with mock.patch.object(pom, '_plex_show', return_value=(show, None)):
            self.assertIn('unlocked for 1', pom.sync_show('tt1', 'default'))
        with mock.patch.object(pom, '_plex_show', return_value=(None, 'show not in Plex yet')):
            self.assertIn('not in Plex yet', pom.sync_show('tt1', 'tvdb:dvd'))

    def test_sync_show_chosen_order_applies(self):
        show = Show([Ep(1, 1, 'wrong')])
        with mock.patch.object(pom, '_plex_show', return_value=(show, None)), \
                mock.patch('cli_battery.app.episode_order.fetch_seasons_for_order', return_value=DVD) as fetch:
            note = pom.sync_show('tt1', 'tvdb:dvd')
        fetch.assert_called_once_with('tt1', 'tvdb:dvd')
        self.assertIn('1 updated', note)

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

    def test_new_episodes_of_chosen_order_shows_get_locked(self):
        orders = {'tt_dvd': 'tvdb:dvd', 'tt_default': None, 'tt_nfo': 'tvdb:absolute'}
        with mock.patch('cli_battery.app.episode_order.show_episode_order', side_effect=orders.get), \
                mock.patch.object(pom, '_nfo_mode', side_effect=lambda i: i == 'tt_nfo'), \
                mock.patch.object(pom, 'sync_show', return_value='1 updated') as sync:
            notes = pom.lock_new_episodes(['tt_dvd', 'tt_default', 'tt_nfo', None, 'tt_dvd'])
        sync.assert_called_once_with('tt_dvd', 'tvdb:dvd')   # default order and NFO shows left alone
        self.assertEqual(notes, {'tt_dvd': '1 updated'})

    def test_only_the_new_unlocked_episode_is_edited(self):
        old = Ep(1, 1, 'Serenity', 'Pilot.', '2002-12-20', locked=('title', 'summary', 'originallyAvailableAt'))
        new = Ep(1, 2, 'Wrong TMDB title')
        c = pom.apply_to_plex_show(Show([old, new]), DVD)
        self.assertEqual((c['updated'], c['unchanged']), (1, 1))
        self.assertEqual(old.calls, [])
        self.assertIn(('title', 'The Train Job', True), new.calls)


if __name__ == '__main__':
    unittest.main()
