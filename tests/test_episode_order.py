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


class GroupTvdbIdLookupTests(unittest.TestCase):
    def test_only_requested_missing_positions_are_looked_up(self):
        groups = _load_groups()
        seasons = {1: {'episodes': {1: _ep('A', src=(1, 1)), 2: _ep('B', src=(1, 2)), 3: _ep('C', tvdb='9', src=(1, 3))}}}
        calls = []

        def fake_get(path):
            calls.append(path)
            return {'tvdb_id': 501} if path.endswith('/episode/1/external_ids') else None
        groups._get = fake_get
        added = groups.add_tvdb_ids(40424, seasons, [(1, 1), (1, 2), (1, 3), (9, 9)])
        self.assertEqual(added, 1)
        self.assertEqual(seasons[1]['episodes'][1]['tvdb_id'], '501')
        self.assertNotIn('tvdb_id', {k: v for k, v in seasons[1]['episodes'][2].items() if v})
        # (1, 3) already had an ID and (9, 9) doesn't exist: two requests only
        self.assertEqual(sorted(calls), ['/tv/40424/season/1/episode/1/external_ids',
                                         '/tv/40424/season/1/episode/2/external_ids'])


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
        self.assertEqual(p['plex_ordering'], ['tvdbDvd', 'dvd'])
        # DVD S01E02 "B" has no library entry before or after the change
        self.assertEqual(p['missing_episodes'], 1)
        self.assertEqual([(m['season'], m['episode'], m['title']) for m in p['missing']], [(1, 2, 'B')])

    def test_missing_skips_specials_and_occupied(self):
        target = {0: {'episodes': {1: _ep('Special')}},
                  1: {'episodes': {1: _ep('A', aired='2009-04-05T00:00:00Z'), 2: _ep('B')}},
                  2: {'episodes': {1: _ep('C')}}}
        missing = self.oc._missing_after_change(target, {(1, 1)})
        self.assertEqual([(m['season'], m['episode']) for m in missing], [(1, 2), (2, 1)])
        self.assertEqual(self.oc._missing_after_change(target, {(1, 1), (1, 2), (2, 1)}), [])

    def _apply(self, add_missing, missing_episodes=3):
        preview = {'success': True, 'conflicts': [], 'new_order': 'tvdb:dvd', 'current_order': 'default',
                   'moves': [], 'removes': [], 'unmappable': [], 'missing_episodes': missing_episodes,
                   'missing_positions': [[1, 101], [1, 102], [1, 165]][:missing_episodes],
                   'plex_ordering': None, 'plex_note': None}
        with mock.patch.object(self.oc, 'preview_order_change', return_value=preview), \
                mock.patch.object(self.oc, '_apply_rows'), \
                mock.patch.object(self.oc, '_reset_battery_seasons'), \
                mock.patch('database.show_overrides.set_show_override'), \
                mock.patch('cli_battery.app.direct_api.DirectAPI.force_refresh_metadata', return_value=({}, None)), \
                mock.patch.object(self.oc, 'queue_missing_episodes', return_value=3) as queue:
            return self.oc.apply_order_change('tt1409055', 'tvdb:dvd', sync_plex=False, add_missing=add_missing), queue

    def test_apply_adds_missing_only_when_asked(self):
        result, queue = self._apply(add_missing=True)
        queue.assert_called_once_with('tt1409055', 'Episode-Order', [[1, 101], [1, 102], [1, 165]])
        self.assertEqual((result['missing_added'], result['missing_requested']), (3, True))
        result, queue = self._apply(add_missing=False)
        queue.assert_not_called()
        self.assertFalse(result['missing_requested'])
        result, queue = self._apply(add_missing=True, missing_episodes=0)
        queue.assert_not_called()



    def test_same_order_rejected(self):
        self.assertFalse(self._preview([], order='tvdb:dvd', current='tvdb:dvd')['success'])
        self.assertFalse(self._preview([], order='tvdb:official')['success'])

    def test_order_without_plex_equivalent(self):
        self.assertIsNone(self.oc._plex_ordering_for('tmdb_group:abc'))
        self.assertEqual(self.oc._plex_ordering_for('tvdb:absolute'), ['tvdbAbsolute', 'absolute'])

    def test_plex_value_follows_the_agent(self):
        def show(enum):
            pref = mock.Mock(id='showOrdering', enumValues=enum)
            return mock.Mock(preferences=mock.Mock(return_value=[pref]))
        current = show({'': 'Library default', 'tvdbAiring': 'a', 'tvdbDvd': 'b', 'tvdbAbsolute': 'c', 'tmdbAiring': 'd'})
        legacy = show({'': 'Library default', 'aired': 'a', 'dvd': 'b', 'absolute': 'c'})
        self.assertEqual(self.oc.plex_ordering_value(current, ['tvdbDvd', 'dvd']), 'tvdbDvd')
        self.assertEqual(self.oc.plex_ordering_value(legacy, ['tvdbDvd', 'dvd']), 'dvd')
        self.assertIsNone(self.oc.plex_ordering_value(legacy, ['tmdbAiring']))

    def _sync(self, refresh_error=None):
        pref = mock.Mock(id='showOrdering', enumValues={'': 'Library default', 'tvdbAbsolute': 'c'})
        show = mock.Mock(type='show', title='Dragon Ball Kai', preferences=mock.Mock(return_value=[pref]))
        show.refresh.side_effect = refresh_error
        plex = mock.Mock(fetchItem=mock.Mock(return_value=show))
        with mock.patch.object(self.oc, '_plex_server', return_value=plex):
            note = self.oc.sync_plex_ordering('tt1409055', ['tvdbAbsolute', 'absolute'], ['112237'])
        return show, note

    def test_plex_ordering_set_and_refreshed(self):
        show, note = self._sync()
        show.editAdvanced.assert_called_once_with(showOrdering='tvdbAbsolute')
        self.assertIn('and the show refreshed', note)

    def test_slow_plex_refresh_is_not_a_failure(self):
        # Plex held the refresh open past the 30s read timeout (Dragon Ball Kai)
        import requests
        show, note = self._sync(requests.exceptions.ReadTimeout('Read timed out. (read timeout=30)'))
        show.editAdvanced.assert_called_once_with(showOrdering='tvdbAbsolute')
        self.assertIn("ordering set to 'tvdbAbsolute'", note)
        self.assertIn('still refreshing', note)
        self.assertNotIn('Could not', note)

    def test_failed_ordering_change_still_reported(self):
        pref = mock.Mock(id='showOrdering', enumValues={'tvdbAbsolute': 'c'})
        show = mock.Mock(type='show', title='Dragon Ball Kai', preferences=mock.Mock(return_value=[pref]))
        show.editAdvanced.side_effect = RuntimeError('401 Unauthorized')
        with mock.patch.object(self.oc, '_plex_server', return_value=mock.Mock(fetchItem=mock.Mock(return_value=show))):
            note = self.oc.sync_plex_ordering('tt1409055', ['tvdbAbsolute'], ['112237'])
        self.assertTrue(note.startswith('Could not set the ordering in Plex'))
        show.refresh.assert_not_called()


class QueueMissingEpisodesTests(unittest.TestCase):
    """utilities/missing_episodes.py: what "add missing to Wanted" queues."""

    def setUp(self):
        import sqlite3
        self.me = _load('missing_episodes_under_test', 'utilities/missing_episodes.py')
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE media_items (imdb_id, tmdb_id, title, type, version)")
        # Dragon Ball Kai: 159 entries in 1080p, 3 in "720 and below", 2 mid-upgrade
        rows = [('1080p',)] * 157 + [('1080p*',)] * 2 + [('720 and below',)] * 3
        conn.executemany("INSERT INTO media_items VALUES ('tt1409055', '61709', 'Dragon Ball Z Kai', 'episode', ?)", rows)
        self.db = mock.Mock(execute=conn.execute)
        patcher = mock.patch.object(self.me, 'get_db_connection', return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_main_versions_only(self):
        self.assertEqual(self.me.show_versions('tt1409055'), ('61709', 'Dragon Ball Z Kai', {'1080p': True}))

    def test_only_the_listed_positions_are_added(self):
        episodes = [{'season_number': 1, 'episode_number': 101}, {'season_number': 1, 'episode_number': 5},
                    {'season_number': 1, 'episode_number': 165}]
        with mock.patch('metadata.metadata.process_metadata', return_value={'episodes': episodes}) as process, \
                mock.patch('database.wanted_items.add_wanted_items', return_value=2) as add:
            self.assertEqual(self.me.queue_missing_episodes('tt1409055', 'Episode-Order', [(1, 101), (1, 165)]), 2)
        item = process.call_args[0][0][0]
        self.assertEqual((item['imdb_id'], item['tmdb_id'], item['versions']), ('tt1409055', '61709', {'1080p': True}))
        queued, versions = add.call_args[0]
        self.assertEqual([(e['season_number'], e['episode_number']) for e in queued], [(1, 101), (1, 165)])
        self.assertEqual(versions, {'1080p': True})
        self.assertEqual(queued[0]['content_source_detail'], 'Episode-Order')

    def test_refresh_adds_everything_missing(self):
        episodes = [{'season_number': 1, 'episode_number': 5}]
        with mock.patch('metadata.metadata.process_metadata', return_value={'episodes': episodes}), \
                mock.patch('database.wanted_items.add_wanted_items', return_value=1) as add:
            self.assertEqual(self.me.queue_missing_episodes('tt1409055', 'Metadata-Refresh'), 1)
        self.assertEqual(len(add.call_args[0][0]), 1)

    def test_show_without_entries_adds_nothing(self):
        self.db.execute = mock.Mock(return_value=mock.Mock(fetchall=mock.Mock(return_value=[])))
        with mock.patch('metadata.metadata.process_metadata') as process:
            self.assertEqual(self.me.queue_missing_episodes('tt0000000', 'Metadata-Refresh'), 0)
        process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
