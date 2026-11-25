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


if __name__ == '__main__':
    unittest.main()
