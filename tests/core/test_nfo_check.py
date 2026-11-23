"""Tools -> NFO Files -> Check a show (utilities/nfo_check.py) and the
per-show NFO override (utilities/nfo_writer.py)."""
import datetime
import os
import tempfile
import unittest
from unittest import mock

from utilities import nfo_check, nfo_writer as nw


class Ep:
    def __init__(self, s, e, title, aired=None):
        self.seasonNumber, self.index, self.title = s, e, title
        self.originallyAvailableAt = datetime.datetime.fromisoformat(aired) if aired else None


class Show:
    title = 'Dragon Ball Z Kai'

    def __init__(self, eps):
        self.eps = eps

    def episodes(self):
        return self.eps


class CheckShowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rows = []
        for s, e, t, d in ((1, 1, 'Prologue to Battle!', '2009-04-05'), (5, 3, "Videl's Crisis?", '2017-04-26'),
                           (2, 1, 'Raven Black (1)', '2014-03-11'), (9, 9, 'Not in Plex', '2020-01-01')):
            path = os.path.join(self.tmp, f'S{s:02d}E{e:02d}.mkv')
            open(path, 'w').close()
            self.rows.append({'imdb_id': 'tt1', 'season_number': s, 'episode_number': e, 'episode_title': t,
                              'release_date': d, 'location_on_disk': path})
        plex = Show([Ep(1, 1, 'Prologue to Battle!', '2009-04-05'),
                     Ep(5, 3, 'Gohan the Teacher!', '2014-04-20'),        # the wrong episode
                     Ep(2, 1, 'Raven Black - Part 1', '2014-03-11')])     # Plex's wording, same episode
        for p in (mock.patch.object(nw, '_episode_rows', return_value=self.rows),
                  mock.patch.object(nw, 'wants_show', return_value=False),
                  mock.patch('utilities.plex_order_metadata._plex_show', return_value=(plex, None))):
            p.start()
            self.addCleanup(p.stop)

    def test_counts_right_wrong_and_missing(self):
        r = nfo_check.check_show('tt1')
        self.assertEqual((r['checked'], r['right']), (4, 2))
        self.assertEqual([w['episode'] for w in r['wrong']], ['S05E03'])
        self.assertEqual(r['wrong'][0]['plex'], 'Gohan the Teacher!')
        self.assertEqual([m['episode'] for m in r['missing']], ['S09E09'])

    def test_show_not_in_plex(self):
        with mock.patch('utilities.plex_order_metadata._plex_show', return_value=(None, 'show not in Plex yet')):
            self.assertEqual(nfo_check.check_show('tt1')['error'], 'show not in Plex yet')


class OverrideTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {'USER_DB_CONTENT': d})
        p.start()
        self.addCleanup(p.stop)
        for p in (mock.patch.object(nw, 'enabled', return_value=True),
                  mock.patch('cli_battery.app.episode_order.show_episode_order', return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_override_beats_the_mode(self):
        with mock.patch.object(nw, 'mode', return_value='changed'):
            self.assertFalse(nw.wants_show('tt1'))
            nw.set_show_override('tt1', 'full')
            self.assertTrue(nw.wants_show('tt1'))
        with mock.patch.object(nw, 'mode', return_value='all'):
            nw.set_show_override('tt1', 'none')
            self.assertFalse(nw.wants_show('tt1'))
            nw.set_show_override('tt1', 'auto')
            self.assertTrue(nw.wants_show('tt1'))
            self.assertEqual(nw.show_override('tt1'), 'auto')

    def test_bad_value_refused(self):
        with self.assertRaises(ValueError):
            nw.set_show_override('tt1', 'sometimes')

    def test_full_override_writes_details_for_aligned_episodes(self):
        from utilities import nfo_sources as ns
        tm = {(1, 1): {'name': 'Prologue to Battle!', 'air_date': '2009-04-05', 'still_path': '/a.jpg'}}
        ep = {'season': 1, 'episode': 1, 'title': 'Prologue to Battle!', 'aired': '2009-04-05'}
        with mock.patch.object(ns, 'tmdb_episodes', return_value=tm), \
                mock.patch.object(ns, 'tvdb_episode', return_value=None):
            self.assertEqual(ns.plan_episodes([ep], 1)[0]['mode'], 'minimal')
            full = ns.plan_episodes([ep], 1, force_full=True)[0]
        self.assertEqual((full['mode'], full['thumb']), ('full', 'https://image.tmdb.org/t/p/original/a.jpg'))


if __name__ == '__main__':
    unittest.main()
