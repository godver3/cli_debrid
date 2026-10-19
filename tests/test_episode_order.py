"""Per-show episode order: provider data, the remap planner, and the preview.

A show can be switched from its default numbering (TVDB aired, or Trakt/TMDB)
to a TVDB season type or a TMDB episode group. Library rows are moved to the
new numbers using a map built from fresh provider data for both orders.
"""
import importlib
import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relpath):
    """A module straight from its file (other tests may leave package stubs around)."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ep(title, aired=None, tvdb=None, tmdb=None, src=None):
    ep = {'title': title, 'first_aired': aired, 'tvdb_id': tvdb, 'tmdb_id': tmdb}
    if src:
        ep['source_season'], ep['source_episode'] = src
    return ep


def _seasons(layout):
    return {s: {'episode_count': len(eps), 'episodes': dict(eps)} for s, eps in layout.items()}


class PlanRemapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _load('episode_order_remap_under_test', 'utilities/episode_order_remap.py')

    def test_tvdb_ids_across_season_types(self):
        # Aired order with a movie season; the DVD order drops the movie and
        # renumbers the next season.
        aired = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('B', tvdb=2)},
                          2: {1: _ep('Movie', tvdb=3)},
                          3: {1: _ep('C', tvdb=4)}})
        dvd = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('B', tvdb=2)},
                        2: {1: _ep('C', tvdb=4), 2: _ep('Extra', tvdb=9)}})
        plan = self.r.plan_remap(aired, dvd)
        self.assertEqual(plan['map'], {(1, 1): (1, 1), (1, 2): (1, 2), (3, 1): (2, 1)})
        self.assertEqual(plan['unmatched_current'], [(2, 1)])
        self.assertEqual(plan['unmatched_target'], [(2, 2)])
        self.assertEqual(plan['joined_by'], {'tvdb_id': 3})
        self.assertEqual(plan['titles'][(2, 1)], 'C')

    def test_tmdb_group_position_only_from_tmdb_default(self):
        current = _seasons({1: {1: _ep('A'), 2: _ep('B')}})
        group = _seasons({1: {1: _ep('B', src=(1, 2))}, 2: {1: _ep('A', src=(1, 1))}})
        self.assertEqual(self.r.plan_remap(current, group, current_is_tmdb_default=True)['map'],
                         {(1, 1): (2, 1), (1, 2): (1, 1)})
        # From TVDB numbering the TMDB positions mean nothing
        self.assertEqual(self.r.plan_remap(current, group)['map'], {})

    def test_air_date_and_title_then_air_date_alone(self):
        current = _seasons({1: {1: _ep('Pilot', '2009-04-05T10:00:00Z', tvdb=1),
                                2: _ep('Part 2', '2009-04-12T10:00:00Z', tvdb=2),
                                3: _ep('Double A', '2009-04-19T10:00:00Z', tvdb=3),
                                4: _ep('Double B', '2009-04-19T10:30:00Z', tvdb=4)}})
        target = _seasons({1: {1: _ep('Pilot!', '2009-04-05', tmdb=11),
                               2: _ep('Part Two', '2009-04-12', tmdb=12),
                               3: _ep('Double A', '2009-04-19', tmdb=13),
                               4: _ep('Something else', '2009-04-19', tmdb=14)}})
        plan = self.r.plan_remap(current, target)
        # 'Pilot' vs 'Pilot!' normalise equal and 'Double A' matches by date+title;
        # 'Part 2' by its unique date, and once 'Double A' is paired 'Double B' is
        # the only one left on its date on each side
        self.assertEqual(plan['map'], {(1, 1): (1, 1), (1, 2): (1, 2), (1, 3): (1, 3), (1, 4): (1, 4)})
        self.assertEqual(plan['joined_by'], {'air_date_title': 2, 'air_date': 2})

    def test_shared_air_date_without_titles_stays_unmatched(self):
        current = _seasons({1: {1: _ep('Part A', '2009-04-19', tvdb=1), 2: _ep('Part B', '2009-04-19', tvdb=2)}})
        target = _seasons({1: {1: _ep('One', '2009-04-19', tmdb=1), 2: _ep('Two', '2009-04-19', tmdb=2)}})
        plan = self.r.plan_remap(current, target)
        self.assertEqual(plan['map'], {})
        self.assertEqual(plan['unmatched_current'], [(1, 1), (1, 2)])

    def test_absent_only_when_target_carries_that_id(self):
        current = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('Movie', tvdb=3), 3: _ep('C', '2001-01-01')}})
        target = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('B', tvdb=2)}})
        self.assertEqual(self.r.plan_remap(current, target)['absent'], [(1, 2)])
        # A TMDB group carries no TVDB IDs: unmatched means unknown, not absent
        group = _seasons({1: {1: _ep('A', tmdb=10), 2: _ep('B', tmdb=11)}})
        self.assertEqual(self.r.plan_remap(current, group)['absent'], [])

    def test_each_target_used_once(self):
        current = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('A again', tvdb=1)}})
        target = _seasons({1: {1: _ep('A', tvdb=1)}})
        plan = self.r.plan_remap(current, target)
        self.assertEqual(len(plan['map']), 1)


class TmdbEpisodeGroupTests(unittest.TestCase):
    def test_seasons_from_group(self):
        groups = _load_groups()
        data = {'id': 'g', 'type': 3, 'groups': [
            {'order': 2, 'name': 'Season 2', 'episodes': [
                {'id': 21, 'order': 0, 'season_number': 1, 'episode_number': 27, 'name': 'X', 'air_date': '2009-10-04'}]},
            {'order': 1, 'name': 'Season 1', 'episodes': [
                {'id': 11, 'order': 1, 'season_number': 1, 'episode_number': 2, 'name': 'B', 'air_date': '2009-04-12'},
                {'id': 10, 'order': 0, 'season_number': 1, 'episode_number': 1, 'name': 'A', 'air_date': '2009-04-05'}]},
        ]}
        seasons = groups.seasons_from_episode_group(data)
        self.assertEqual(sorted(seasons), [1, 2])
        self.assertEqual(seasons[1]['episodes'][2]['title'], 'B')
        ep = seasons[2]['episodes'][1]
        self.assertEqual((ep['tmdb_id'], ep['source_season'], ep['source_episode']), ('21', 1, 27))
        self.assertEqual(ep['first_aired'], '2009-10-04T00:00:00.000Z')
        self.assertIsNone(ep['absolute'])

    def test_absolute_group_numbers_straight_through(self):
        groups = _load_groups()
        data = {'id': 'g', 'type': 2, 'groups': [{'order': 1, 'name': 'All', 'episodes': [
            {'id': 5, 'order': 98, 'season_number': 2, 'episode_number': 1, 'name': 'X'}]}]}
        self.assertEqual(groups.seasons_from_episode_group(data)[1]['episodes'][99]['absolute'], 99)


def _load_groups():
    # cli_battery's logger is all the module needs from its package
    fake_logger = mock.MagicMock()
    with mock.patch.dict(sys.modules, {'cli_battery.app.logger_config': mock.MagicMock(logger=fake_logger)}):
        source = open(os.path.join(ROOT, 'cli_battery/app/tmdb_episode_groups.py')).read()
        source = source.replace('from .logger_config import logger', 'from cli_battery.app.logger_config import logger')
        module = type(sys)('tmdb_episode_groups_under_test')
        exec(compile(source, 'tmdb_episode_groups.py', 'exec'), module.__dict__)
        return module


class SeasonsInShowOrderTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in sys.modules if n == 'cli_battery.app.episode_order']:
            del sys.modules[name]
        import cli_battery.app.episode_order as eo  # noqa: E402
        self.eo = eo

    def test_default_order_keeps_seasons(self):
        with mock.patch.object(self.eo, 'show_episode_order', return_value=None), \
                mock.patch.object(self.eo, 'fetch_seasons_for_order') as fetch:
            self.assertEqual(self.eo.seasons_in_show_order('tt1', {1: 'default'}), {1: 'default'})
            fetch.assert_not_called()

    def test_chosen_order_replaces_seasons(self):
        with mock.patch.object(self.eo, 'show_episode_order', return_value='tvdb:dvd'), \
                mock.patch.object(self.eo, 'fetch_seasons_for_order', return_value={1: 'dvd'}) as fetch:
            self.assertEqual(self.eo.seasons_in_show_order('tt1', {1: 'default'}), {1: 'dvd'})
            fetch.assert_called_once_with('tt1', 'tvdb:dvd')

    def test_fetch_failure_keeps_default(self):
        with mock.patch.object(self.eo, 'show_episode_order', return_value='tmdb_group:x'), \
                mock.patch.object(self.eo, 'fetch_seasons_for_order', side_effect=RuntimeError('down')):
            self.assertEqual(self.eo.seasons_in_show_order('tt1', {1: 'default'}), {1: 'default'})

    def test_default_aliases(self):
        for order in (None, '', 'default', 'tvdb:official', 'tvdb:default'):
            self.assertTrue(self.eo.is_default_order(order), order)
        self.assertFalse(self.eo.is_default_order('tvdb:dvd'))


class PreviewOrderChangeTests(unittest.TestCase):
    AIRED = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('B', tvdb=2)},
                      2: {1: _ep('Movie', tvdb=3)},
                      3: {1: _ep('C', tvdb=4)}})
    DVD = _seasons({1: {1: _ep('A', tvdb=1), 2: _ep('B', tvdb=2)},
                    2: {1: _ep('C', tvdb=4)}})

    def setUp(self):
        patcher = mock.patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [n for n in sys.modules if n in ('utilities.episode_order_change', 'cli_battery.app.episode_order')
                     or n == 'database' or n.startswith('database.')]:
            del sys.modules[name]
        import database  # noqa: F401
        import cli_battery.app.episode_order as eo
        import utilities.episode_order_change as oc
        self.eo, self.oc = eo, oc

    def _row(self, rid, s, e, version='1080p', state='Wanted', file=None):
        return {'id': rid, 'season_number': s, 'episode_number': e, 'version': version, 'state': state,
                'episode_title': '', 'filled_by_file': file, 'location_on_disk': None, 'ms_item_id': None}

    def _preview(self, rows, order='tvdb:dvd', current=None):
        orders = {'default': self.AIRED, 'tvdb:dvd': self.DVD}
        with mock.patch.object(self.eo, 'show_episode_order', return_value=current), \
                mock.patch.object(self.eo, 'fetch_seasons_for_order', side_effect=lambda i, o: orders.get(o)), \
                mock.patch.object(self.oc, '_show_rows', return_value=rows), \
                mock.patch.object(self.oc, '_add_stored_ids'), \
                mock.patch('cli_battery.app.tvdb_client.is_available', return_value=True):
            return self.oc.preview_order_change('tt1', order)

    def test_moves_removes_and_unmappable(self):
        rows = [self._row(1, 1, 1, state='Collected', file='a.mkv'),
                self._row(2, 3, 1, state='Collected', file='c.mkv'),
                self._row(3, 3, 1, version='2160p'),
                self._row(4, 2, 1),                                   # movie, only wanted
                self._row(5, 2, 1, version='2160p', state='Collected', file='m.mkv')]
        p = self._preview(rows)
        self.assertTrue(p['success'])
        self.assertEqual(p['unchanged'], 1)
        self.assertEqual(sorted(m['id'] for m in p['moves']), [2, 3])
        self.assertEqual(p['moves'][0]['new'], [2, 1])
        self.assertEqual([r['id'] for r in p['removes']], [4])
        self.assertEqual([u['id'] for u in p['unmappable']], [5])
        # Row 5 stays at S02E01 [2160p] while row 3 moves to S02E01 [2160p]
        self.assertEqual(p['conflicts'], [{'version': '2160p', 'season': 2, 'episode': 1, 'row_ids': [3, 5]}])
        self.assertEqual(p['plex_ordering'], 'dvd')

    def test_same_order_rejected(self):
        self.assertFalse(self._preview([], order='tvdb:dvd', current='tvdb:dvd')['success'])
        self.assertFalse(self._preview([], order='tvdb:official')['success'])

    def test_order_without_plex_equivalent(self):
        self.assertIsNone(self.oc._plex_ordering_for('tmdb_group:abc'))
        self.assertEqual(self.oc._plex_ordering_for('tvdb:absolute'), 'absolute')


if __name__ == '__main__':
    unittest.main()
