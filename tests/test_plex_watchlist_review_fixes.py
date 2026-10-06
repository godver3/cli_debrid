#!/usr/bin/env python3
"""Fixes from the end-to-end Plex watchlist review (PR #522).

Group 1: transient token check no longer NameErrors, per-item isolation, bounded paging.
Group 2: Trakt-free TVDB->IMDb, short retry of cached TMDB failures, retry of unresolved items.
Group 3: one removal rule for fetch/RSS/post-processing, exact IMDb match, read-only label sync,
         deletion_manager type matching.
Group 4: debug dispatcher matches the scheduled run.
Group 5: onboarding content-source listing needs an admin.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
import content_checkers.plex_watchlist as pw
import content_checkers.plex_rss_watchlist as rss
from content_checkers import content_cache_management as ccm
from cli_battery.app import staleness, tvdb_client


# Some other test modules replace flask / utilities.settings in sys.modules and never restore
# them, so route-level tests (which need the real ones) run in a clean interpreter instead of
# depending on test order.
_ISOLATED_ENV = 'PLEX_REVIEW_FIXES_ISOLATED'
_isolated_results = {}


def _delegated_to_clean_interpreter(testcase):
    """In the parent run, execute this test's class once in a subprocess and assert it passed.

    Returns True when the caller should skip its own body (parent), False inside the subprocess.
    """
    if os.environ.get(_ISOLATED_ENV):
        return False
    cls = type(testcase)
    if cls.__name__ not in _isolated_results:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _isolated_results[cls.__name__] = subprocess.run(
            [sys.executable, '-m', 'pytest', f'{os.path.abspath(__file__)}::{cls.__name__}',
             '-q', '-p', 'no:cacheprovider', '-W', 'ignore'],
            cwd=root, env={**os.environ, _ISOLATED_ENV: '1'}, capture_output=True, text=True, timeout=600)
    proc = _isolated_results[cls.__name__]
    testcase.assertEqual(proc.returncode, 0, (proc.stdout or '')[-4000:] + (proc.stderr or '')[-1000:])
    return True


def _async_result(results):
    async def _fake(items, token):
        return results(items)
    return _fake


# --------------------------------------------------------------------------- Group 1

class TestTokenCheckTransient(unittest.TestCase):
    def test_transient_failure_keeps_last_known_status_without_nameerror(self):
        settings = lambda section, key, default=None: 'tok' if (section, key) == ('Plex', 'token') else default
        with mock.patch.object(pw, 'get_setting', side_effect=settings), \
             mock.patch.object(pw, '_check_plex_token', return_value=(False, None, None, 'timeout', True)), \
             mock.patch.object(pw, 'load_token_status', return_value={'main': {'valid': True}}), \
             mock.patch.object(pw, 'load_config', return_value={'Content Sources': {}}):
            status = pw.validate_plex_tokens()
        self.assertEqual(status['main'], {'valid': True})

    def test_transient_failure_with_no_history_is_unknown_not_invalid(self):
        settings = lambda section, key, default=None: 'tok' if (section, key) == ('Plex', 'token') else default
        with mock.patch.object(pw, 'get_setting', side_effect=settings), \
             mock.patch.object(pw, '_check_plex_token', return_value=(False, None, None, 'timeout', True)), \
             mock.patch.object(pw, 'load_token_status', return_value={}), \
             mock.patch.object(pw, 'load_config', return_value={'Content Sources': {}}):
            status = pw.validate_plex_tokens()
        self.assertIsNone(status['main']['valid'])


class _FakePlexItem:
    def __init__(self, title, media_type='movie'):
        self.title = title
        self.type = 'show' if media_type == 'tv' else 'movie'


class _FakeAccount:
    username = 'tester'
    title = 'Tester'

    def __init__(self, items):
        self._items = items
        self.removed = []

    def watchlist(self):
        return self._items

    def removeFromWatchlist(self, items):
        self.removed.extend(items if isinstance(items, (list, tuple)) else [items])  # plexapi takes one or many


def _fetched(entries):
    """entries: [(plex_item, imdb_id, tmdb_id, media_type)] -> run_async_fetches stand-in."""
    def build(items):
        return [{'imdb_id': i, 'tmdb_id': t, 'media_type': m, 'original_plex_item': p} for p, i, t, m in entries]
    return _async_result(build)


class TestMyPlexWatchlist(unittest.TestCase):
    def _run(self, entries, presence, setting_values=None, read_only=False, show_status='returning series'):
        account = _FakeAccount([e[0] for e in entries])
        values = {'plex_watchlist_removal': True, 'plex_watchlist_keep_series': False}
        values.update(setting_values or {})
        with mock.patch.object(pw, 'get_plex_client', return_value=(account, 'tok')), \
             mock.patch.object(pw, 'get_setting', side_effect=lambda s, k, d=False: values.get(k, d)), \
             mock.patch.object(pw, 'run_async_fetches', _fetched(entries)), \
             mock.patch.object(pw, 'get_media_item_presence_overall', side_effect=presence), \
             mock.patch.object(pw, 'get_show_status', return_value=show_status):
            result = pw.get_wanted_from_plex_watchlist({'Default': True}, read_only=read_only)
        return [i for batch, _ in result for i in batch], account.removed

    def test_one_failing_item_does_not_empty_the_source(self):
        bad, good = _FakePlexItem('Bad'), _FakePlexItem('Good')

        def presence(imdb_id):
            if imdb_id == 'tt0000001':
                raise RuntimeError('database is locked')
            return 'Missing'

        items, _ = self._run([(bad, 'tt0000001', None, 'movie'), (good, 'tt0000002', '22', 'movie')], presence)
        self.assertEqual([i['imdb_id'] for i in items], ['tt0000002'])
        self.assertEqual(items[0]['tmdb_id'], '22')  # passed through for the metadata step

    def test_partial_movie_is_kept_collected_movie_is_removed(self):
        partial, collected = _FakePlexItem('Partial'), _FakePlexItem('Collected')
        states = {'tt0000001': 'Partial', 'tt0000002': 'Collected'}
        items, removed = self._run(
            [(partial, 'tt0000001', None, 'movie'), (collected, 'tt0000002', None, 'movie')],
            lambda imdb_id: states[imdb_id])
        self.assertEqual([i['imdb_id'] for i in items], ['tt0000001'])
        self.assertEqual(removed, [collected])

    def test_read_only_never_removes_and_keeps_collected_titles(self):
        collected = _FakePlexItem('Collected')
        items, removed = self._run([(collected, 'tt0000002', None, 'movie')], lambda imdb_id: 'Collected', read_only=True)
        self.assertEqual(removed, [])
        self.assertEqual([i['imdb_id'] for i in items], ['tt0000002'])

    def test_managed_profile_without_username_uses_title_as_source_detail(self):
        item = _FakePlexItem('X')
        account = _FakeAccount([item])
        account.username = ''
        with mock.patch.object(pw, 'get_plex_client', return_value=(account, 'tok')), \
             mock.patch.object(pw, 'get_setting', return_value=False), \
             mock.patch.object(pw, 'run_async_fetches', _fetched([(item, 'tt0000001', None, 'movie')])), \
             mock.patch.object(pw, 'get_media_item_presence_overall', return_value='Missing'):
            result = pw.get_wanted_from_plex_watchlist({'Default': True})
        self.assertEqual(result[0][0][0]['content_source_detail'], 'Tester')


class TestFriendsPaging(unittest.TestCase):
    def test_cursor_that_never_advances_stops(self):
        calls = []

        def community(token, query, variables=None):
            calls.append(variables['after'])
            return {'user': {'watchlist': {'nodes': [{'id': f'n{len(calls)}'}],
                                           'pageInfo': {'hasNextPage': True, 'endCursor': 'same'}}}}

        with mock.patch.object(pw, '_plex_community_query', side_effect=community):
            nodes = pw._get_friend_watchlist_nodes('tok', 'u1')
        self.assertEqual(len(calls), 2)  # first page, then the repeated cursor ends it
        self.assertEqual(len(nodes), 2)

    def test_endless_advancing_cursor_is_capped(self):
        counter = {'n': 0}

        def community(token, query, variables=None):
            counter['n'] += 1
            return {'user': {'watchlist': {'nodes': [{'id': f'n{counter["n"]}'}],
                                           'pageInfo': {'hasNextPage': True, 'endCursor': f'c{counter["n"]}'}}}}

        with mock.patch.object(pw, '_plex_community_query', side_effect=community):
            nodes = pw._get_friend_watchlist_nodes('tok', 'u1')
        self.assertEqual(counter['n'], pw._FRIEND_WATCHLIST_MAX_PAGES)
        self.assertEqual(len(nodes), pw._FRIEND_WATCHLIST_MAX_PAGES)

    def test_friends_source_survives_plex_tv_outage(self):
        """The main token comes from settings, so a plex.tv outage is not 'no token configured'."""
        with mock.patch.object(pw, 'get_setting', side_effect=lambda s, k, d=None: 'tok' if k == 'token' else d), \
             mock.patch.object(pw, 'get_plex_client', side_effect=AssertionError('must not call plex.tv')), \
             mock.patch.object(pw, 'get_plex_friends', return_value=[]):
            result = pw.get_wanted_from_plex_friends_watchlist({'friends': ''}, {'Default': True})
        self.assertEqual(result, [([], {'Default': True})])


# --------------------------------------------------------------------------- Group 2

class TestTvdbToImdbWithoutTrakt(unittest.TestCase):
    def _response(self, payload):
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def test_cached_mapping_wins(self):
        with mock.patch.object(tvdb_client.DatabaseManager, 'get_imdb_from_tvdb', return_value='tt0000111'), \
             mock.patch.object(tvdb_client, '_make_request', side_effect=AssertionError('no network')):
            self.assertEqual(tvdb_client.convert_tvdb_to_imdb('482971'), 'tt0000111')

    def test_tvdb_remote_ids_supply_the_imdb_id_and_are_cached(self):
        payload = {'data': {'remoteIds': [{'sourceName': 'IMDB', 'id': 'tt0000222'}]}}
        with mock.patch.object(tvdb_client.DatabaseManager, 'get_imdb_from_tvdb', return_value=None), \
             mock.patch.object(tvdb_client, 'is_available', return_value=True), \
             mock.patch.object(tvdb_client, '_make_request', return_value=self._response(payload)), \
             mock.patch.object(tvdb_client, '_cache_tvdb_mapping') as cache:
            self.assertEqual(tvdb_client.convert_tvdb_to_imdb('462637'), 'tt0000222')
        cache.assert_called_once_with('462637', 'tt0000222', 'show')

    def test_falls_back_to_tmdb_when_tvdb_has_no_imdb_id(self):
        payload = {'data': {'remoteIds': [{'sourceName': 'TheMovieDB.com', 'id': '555'}]}}
        direct = mock.Mock()
        direct.tmdb_to_imdb.return_value = ('tt0000333', 'tmdb')
        with mock.patch.object(tvdb_client.DatabaseManager, 'get_imdb_from_tvdb', return_value=None), \
             mock.patch.object(tvdb_client, 'is_available', return_value=True), \
             mock.patch.object(tvdb_client, '_make_request', return_value=self._response(payload)), \
             mock.patch.object(tvdb_client, '_get_tmdb_api_key', return_value='key'), \
             mock.patch.object(tvdb_client, '_cache_tvdb_mapping') as cache, \
             mock.patch('cli_battery.app.direct_api.DirectAPI', direct):
            self.assertEqual(tvdb_client.convert_tvdb_to_imdb('385925'), 'tt0000333')
        direct.tmdb_to_imdb.assert_called_once_with('555', media_type='show')
        cache.assert_called_once_with('385925', 'tt0000333', 'show')

    def test_tmdb_find_used_when_no_tvdb_key(self):
        find = self._response({'tv_results': [{'id': 777}]})
        direct = mock.Mock()
        direct.tmdb_to_imdb.return_value = ('tt0000444', 'tmdb')
        with mock.patch.object(tvdb_client.DatabaseManager, 'get_imdb_from_tvdb', return_value=None), \
             mock.patch.object(tvdb_client, 'is_available', return_value=False), \
             mock.patch.object(tvdb_client, '_get_tmdb_api_key', return_value='key'), \
             mock.patch.object(tvdb_client.requests, 'get', return_value=find) as get, \
             mock.patch.object(tvdb_client, '_cache_tvdb_mapping'), \
             mock.patch('cli_battery.app.direct_api.DirectAPI', direct):
            self.assertEqual(tvdb_client.convert_tvdb_to_imdb('326887'), 'tt0000444')
        self.assertEqual(get.call_args.kwargs['params']['external_source'], 'tvdb_id')

    def test_no_keys_means_none_not_a_crash(self):
        with mock.patch.object(tvdb_client.DatabaseManager, 'get_imdb_from_tvdb', return_value=None), \
             mock.patch.object(tvdb_client, 'is_available', return_value=False), \
             mock.patch.object(tvdb_client, '_get_tmdb_api_key', return_value=''):
            self.assertIsNone(tvdb_client.convert_tvdb_to_imdb('255325'))

    def test_rss_tvdb_guid_uses_the_conversion_not_trakt(self):
        self.assertFalse(hasattr(rss, 'trakt_client'))
        with mock.patch('cli_battery.app.tvdb_client.convert_tvdb_to_imdb', return_value='tt0000555') as conv:
            self.assertEqual(rss.extract_imdb_id('tvdb://482971', 'Some Show'), 'tt0000555')
        conv.assert_called_once_with('482971')


class TestRssCategoryAndCap(unittest.TestCase):
    def test_missing_category_tries_movie_only(self):
        """No <category>: a tmdb guid is a movie (as before), never tried as a show first."""
        calls = []

        def tmdb_to_imdb(tmdb_id, media_type):
            calls.append(media_type)
            return ('tt0000666', 'x') if media_type == 'movie' else ('tt9999999', 'wrong show')

        api = mock.Mock()
        api.tmdb_to_imdb.side_effect = tmdb_to_imdb
        with mock.patch('cli_battery.app.direct_api.DirectAPI', return_value=api):
            imdb_id, media_type = rss.resolve_imdb_and_type(['tmdb://9'], 'Thing', None)
        self.assertEqual((imdb_id, media_type), ('tt0000666', 'movie'))
        self.assertEqual(calls, ['movie'])

    def test_tvdb_only_guid_without_category_is_a_show(self):
        with mock.patch.object(rss, 'extract_imdb_id', return_value='tt0000777'):
            self.assertEqual(rss.resolve_imdb_and_type(['tvdb://1'], 'Thing', None), ('tt0000777', 'tv'))

    def test_cap_warning_also_fires_for_fifty_item_feeds(self):
        entries = [{'title': f'M{n}', 'guids': [f'imdb://tt{n:07d}'], 'category': 'movie'} for n in range(50)]
        with mock.patch.object(rss, 'fetch_plex_rss_entries', return_value=entries), \
             mock.patch.object(rss, 'get_setting', return_value=False), \
             mock.patch.object(rss, 'get_media_item_presence_overall', return_value='Missing'), \
             mock.patch.object(rss.logging, 'warning') as warn:
            rss.get_wanted_from_plex_rss('https://rss.plex.tv/x', {'Default': True})
        self.assertTrue(any('caps watchlist RSS feeds' in c.args[0] for c in warn.call_args_list))

    def test_friend_feed_label_hides_the_secret_url(self):
        label = rss._friend_feed_label('https://rss.plex.tv/0123456789abcdef-secret-uuid')
        self.assertNotIn('0123456789', label)
        self.assertTrue(label.endswith('t-uuid'[-6:]))


class TestNegativeTmdbMappingRetry(unittest.TestCase):
    def test_cached_failure_is_retried_after_a_day_but_a_hit_lasts_weeks(self):
        two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
        self.assertTrue(staleness.is_tmdb_mapping_stale(two_days_ago, negative=True))
        self.assertFalse(staleness.is_tmdb_mapping_stale(two_days_ago, negative=False))
        self.assertFalse(staleness.is_tmdb_mapping_stale(datetime.now(timezone.utc) - timedelta(hours=1), negative=True))
        self.assertTrue(staleness.is_tmdb_mapping_stale(datetime.now(timezone.utc) - timedelta(days=22)))

    def test_old_single_argument_callers_keep_the_positive_threshold(self):
        self.assertFalse(staleness.is_tmdb_mapping_stale(datetime.now(timezone.utc) - timedelta(days=5)))


class TestUnresolvedItemsAreRetriedSooner(unittest.TestCase):
    def test_output_ids_match_by_imdb_or_tmdb(self):
        output = ccm.metadata_output_ids([{'imdb_id': 'tt1', 'tmdb_id': 5}, {'imdb_id': 'tt2'}])
        self.assertTrue(ccm.item_has_metadata_output({'imdb_id': 'tt1'}, output))
        self.assertTrue(ccm.item_has_metadata_output({'tmdb_id': '5'}, output))
        self.assertFalse(ccm.item_has_metadata_output({'imdb_id': 'tt9'}, output))
        self.assertFalse(ccm.item_has_metadata_output({'title': 'no ids'}, output))

    def test_unresolved_item_gets_the_short_expiry(self):
        cache = {}
        ccm.update_cache_for_item({'imdb_id': 'tt9', 'media_type': 'tv'}, 'src_1', cache,
                                  retry_after_hours=ccm.UNRESOLVED_RETRY_HOURS)
        (entry,) = cache.values()
        self.assertEqual(entry['expiry_duration_hours'], ccm.UNRESOLVED_RETRY_HOURS)

    def test_resolved_item_keeps_the_randomised_expiry(self):
        cache = {}
        ccm.update_cache_for_item({'imdb_id': 'tt1', 'media_type': 'movie'}, 'src_1', cache)
        (entry,) = cache.values()
        self.assertTrue(6 <= entry['expiry_duration_hours'] <= 18)

    def test_deleting_a_source_removes_its_item_cache(self):
        from queues import config_manager
        cache_file = ccm.get_cache_file_path('Other Plex Watchlist_9')
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        with open(cache_file, 'wb') as f:
            f.write(b'x')
        config = {'Content Sources': {'Other Plex Watchlist_9': {}}}
        with mock.patch.object(config_manager, 'load_config', return_value=config), \
             mock.patch.object(config_manager, 'save_config'):
            self.assertTrue(config_manager.delete_content_source('Other Plex Watchlist_9'))
        self.assertFalse(os.path.exists(cache_file))


class TestEpisodelessRefreshThrottle(unittest.TestCase):
    def test_second_refresh_within_cooldown_is_refused(self):
        from metadata import metadata
        metadata._episodeless_refresh_at.pop('tt-throttle', None)
        self.assertTrue(metadata._episodeless_refresh_allowed('tt-throttle'))
        self.assertFalse(metadata._episodeless_refresh_allowed('tt-throttle'))


# --------------------------------------------------------------------------- Group 3

class TestRemovalRule(unittest.TestCase):
    def _rule(self, media_type, state, status='returning series', keep_series=False):
        with mock.patch.object(pw, 'get_setting', side_effect=lambda s, k, d=False: keep_series if k == 'plex_watchlist_keep_series' else d), \
             mock.patch.object(pw, 'get_show_status', return_value=status):
            return pw.should_remove_from_watchlist('tt0000001', media_type, state)[0]

    def test_movies_only_when_fully_collected(self):
        self.assertTrue(self._rule('movie', 'Collected'))
        self.assertFalse(self._rule('movie', 'Partial'))
        self.assertFalse(self._rule('movie', 'Wanted'))

    def test_shows_only_when_ended(self):
        self.assertFalse(self._rule('tv', 'Collected', 'returning series'))
        self.assertFalse(self._rule('tv', 'Collected', ''))
        self.assertTrue(self._rule('tv', 'Collected', 'ended'))

    def test_keep_series_never_removes_a_show(self):
        self.assertFalse(self._rule('tv', 'Collected', 'ended', keep_series=True))

    def test_battery_status_is_mapped_without_trakt(self):
        direct = mock.Mock()
        direct.get_show_status.return_value = 'canceled'
        with mock.patch.object(pw, 'DirectAPI', direct):
            self.assertEqual(pw.get_show_status('tt0000001'), 'ended')
        direct.get_show_status.return_value = 'returning series'
        with mock.patch.object(pw, 'DirectAPI', direct):
            self.assertEqual(pw.get_show_status('tt0000001'), 'returning series')
        direct.get_show_status.return_value = None
        with mock.patch.object(pw, 'DirectAPI', direct):
            self.assertEqual(pw.get_show_status('tt0000001'), '')
        direct.get_show_status.side_effect = RuntimeError('db')
        with mock.patch.object(pw, 'DirectAPI', direct):
            self.assertEqual(pw.get_show_status('tt0000001'), '')
        # The full show record (all seasons/episodes, possible provider refresh) is never loaded.
        direct.get_show_metadata.assert_not_called()


class TestExactImdbMatch(unittest.TestCase):
    def test_longer_id_does_not_match_its_prefix(self):
        self.assertTrue(pw._guid_matches_imdb(SimpleNamespace(id='imdb://tt1234567'), 'tt1234567'))
        self.assertFalse(pw._guid_matches_imdb(SimpleNamespace(id='imdb://tt12345678'), 'tt1234567'))
        self.assertFalse(pw._guid_matches_imdb('tmdb://tt1234567', 'tt1234567'))

    def test_removal_picks_the_exact_watchlist_entry(self):
        longer = SimpleNamespace(title='Longer', guids=[SimpleNamespace(id='imdb://tt12345678')])
        exact = SimpleNamespace(title='Exact', guids=[SimpleNamespace(id='imdb://tt1234567')])
        account = _FakeAccount([longer, exact])
        with mock.patch.object(pw, 'get_plex_client', return_value=(account, 'tok')):
            result = pw.remove_from_plex_watchlist_by_item({'imdb_id': 'tt1234567', 'title': 'Exact', 'type': 'movie'})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(account.removed, [exact])


class TestPostProcessingRemoval(unittest.TestCase):
    def _handle(self, item, source_type='My Plex Watchlist', **rule):
        from utilities import post_processing
        config = {'Content Sources': {'My Plex Watchlist_1': {'type': source_type, 'token': 'tok'}}}
        with mock.patch.object(post_processing, 'get_setting', return_value=True), \
             mock.patch('queues.config_manager.load_config', return_value=config), \
             mock.patch.object(pw, 'should_remove_from_watchlist', return_value=(rule.get('remove', False), 'why')), \
             mock.patch.object(pw, 'remove_from_plex_watchlist_by_item', return_value={'success': True}) as mine, \
             mock.patch.object(pw, 'remove_from_other_plex_watchlist_by_item', return_value={'success': True}) as other:
            post_processing._handle_plex_watchlist_removal(item)
        return mine, other

    ITEM = {'imdb_id': 'tt0000001', 'title': 'T', 'type': 'episode', 'content_source': 'My Plex Watchlist_1'}

    def test_first_collected_episode_of_a_running_show_does_not_remove_it(self):
        mine, other = self._handle(self.ITEM, remove=False)
        mine.assert_not_called()
        other.assert_not_called()

    def test_ended_show_is_removed_from_my_watchlist(self):
        mine, _ = self._handle(self.ITEM, remove=True)
        mine.assert_called_once()

    def test_other_watchlist_uses_its_own_token_and_the_same_rule(self):
        mine, other = self._handle(self.ITEM, source_type='Other Plex Watchlist', remove=True)
        mine.assert_not_called()
        other.assert_called_once()
        self.assertEqual(other.call_args.args[1], 'tok')

    def test_movie_is_judged_as_a_movie(self):
        from utilities import post_processing
        movie = dict(self.ITEM, type='movie')
        config = {'Content Sources': {'My Plex Watchlist_1': {'type': 'My Plex Watchlist'}}}
        with mock.patch.object(post_processing, 'get_setting', return_value=True), \
             mock.patch('queues.config_manager.load_config', return_value=config), \
             mock.patch.object(pw, 'should_remove_from_watchlist', return_value=(False, 'partial')) as rule:
            post_processing._handle_plex_watchlist_removal(movie)
        self.assertEqual(rule.call_args.args[:2], ('tt0000001', 'movie'))


class TestRssSharesTheRemovalRule(unittest.TestCase):
    def _run(self, state, media_type, read_only=False):
        entry = {'title': 'T', 'guids': ['imdb://tt0000001'], 'category': 'show' if media_type == 'tv' else 'movie'}
        values = {'plex_watchlist_removal': True, 'plex_watchlist_keep_series': False}
        with mock.patch.object(rss, 'fetch_plex_rss_entries', return_value=[entry]), \
             mock.patch.object(rss, 'get_setting', side_effect=lambda s, k, d=False: values.get(k, d)), \
             mock.patch.object(rss, 'get_media_item_presence_overall', return_value=state), \
             mock.patch.object(pw, 'get_setting', side_effect=lambda s, k, d=False: values.get(k, d)), \
             mock.patch.object(pw, 'get_show_status', return_value='returning series'):
            result = rss.get_wanted_from_plex_rss('https://rss.plex.tv/x', {'Default': True}, read_only=read_only)
        return [i for batch, _ in result for i in batch]

    def test_partial_movie_is_no_longer_suppressed(self):
        self.assertEqual(len(self._run('Partial', 'movie')), 1)

    def test_collected_movie_is_suppressed_unless_read_only(self):
        self.assertEqual(self._run('Collected', 'movie'), [])
        self.assertEqual(len(self._run('Collected', 'movie', read_only=True)), 1)

    def test_running_show_is_kept_and_monitored_for_new_episodes(self):
        (item,) = self._run('Collected', 'tv')
        self.assertTrue(item['monitor_missing_episodes_only'])


class TestDeletionManagerPlexMatching(unittest.TestCase):
    def test_full_type_strings_and_first_word_forms_match(self):
        from utilities.deletion_manager import _item_from_plex_watchlist
        self.assertTrue(_item_from_plex_watchlist({'My Plex Watchlist'}))
        self.assertTrue(_item_from_plex_watchlist({'Other Plex Watchlist'}))
        self.assertTrue(_item_from_plex_watchlist({'Other'}))
        self.assertFalse(_item_from_plex_watchlist({'Overseerr', 'Trakt'}))


class TestLabelSyncIsReadOnly(unittest.TestCase):
    def test_label_sync_asks_the_fetchers_not_to_remove(self):
        from utilities import plex_label_manager
        with mock.patch.object(pw, 'get_wanted_from_plex_watchlist', return_value=[([], {})]) as mine:
            plex_label_manager._fetch_live_imdb_ids_for_source('My Plex Watchlist_1', {'type': 'My Plex Watchlist'}, {})
        self.assertTrue(mine.call_args.kwargs.get('read_only'))
        with mock.patch.object(rss, 'get_wanted_from_plex_rss', return_value=[([], {})]) as feed:
            plex_label_manager._fetch_live_imdb_ids_for_source(
                'My Plex RSS Watchlist_1', {'type': 'My Plex RSS Watchlist', 'url': 'https://rss.plex.tv/x'}, {})
        self.assertTrue(feed.call_args.kwargs.get('read_only'))


# --------------------------------------------------------------------------- Group 4

class TestDebugDispatcherMatchesScheduledRun(unittest.TestCase):
    def _run(self, source, fetched_items):
        import routes.debug_routes as dbg
        seen = {}

        def fake_metadata(items):
            seen['items'] = items
            return {'movies': [dict(i, media_type='movie', release_date='2024-01-01') for i in items],
                    'episodes': [], 'anime': [{'imdb_id': 'tt-anime', 'media_type': 'movie', 'release_date': '2024-01-01'}]}

        added = []
        settings = {'Content Sources': {'My Plex Watchlist_1': source}}
        with mock.patch.object(dbg, 'get_all_settings', return_value=settings), \
             mock.patch.object(dbg, 'get_setting', return_value=False), \
             mock.patch.object(dbg, 'load_source_cache', return_value={}), \
             mock.patch.object(dbg, 'save_source_cache'), \
             mock.patch('content_checkers.plex_watchlist.get_wanted_from_plex_watchlist', return_value=fetched_items), \
             mock.patch('metadata.metadata.process_metadata', side_effect=fake_metadata), \
             mock.patch('database.add_wanted_items', side_effect=lambda items, versions, **kw: added.append((items, versions)) or len(items)):
            result = dbg.get_and_add_wanted_content('My Plex Watchlist_1')
        return result, seen, added

    def test_content_source_is_set_before_metadata_runs_and_anime_is_kept(self):
        if _delegated_to_clean_interpreter(self):
            return
        source = {'versions': ['1080p'], 'media_type': 'All', 'enabled': True}
        raw = [([{'imdb_id': 'tt0000001', 'media_type': 'movie'}], {'1080p': True})]
        result, seen, added = self._run(source, raw)
        self.assertEqual(seen['items'][0]['content_source'], 'My Plex Watchlist_1')
        self.assertEqual(seen['items'][0]['versions'], {'1080p': True})
        self.assertEqual(sorted(i['imdb_id'] for i in added[0][0]), ['tt-anime', 'tt0000001'])

    def test_source_without_enabled_versions_is_not_processed(self):
        if _delegated_to_clean_interpreter(self):
            return
        source = {'versions': {'1080p': False}, 'media_type': 'All', 'enabled': True}
        result, seen, added = self._run(source, [([{'imdb_id': 'tt0000001', 'media_type': 'movie'}], {})])
        self.assertIn('error', result)
        self.assertEqual(added, [])
        self.assertNotIn('items', seen)

    def test_unknown_release_date_is_dropped_when_a_cutoff_is_set(self):
        if _delegated_to_clean_interpreter(self):
            return
        source = {'versions': ['1080p'], 'media_type': 'All', 'cutoff_date': '2000-01-01', 'enabled': True}
        import routes.debug_routes as dbg

        def fake_metadata(items):
            return {'movies': [{'imdb_id': 'tt1', 'media_type': 'movie', 'release_date': 'unknown'},
                               {'imdb_id': 'tt2', 'media_type': 'movie', 'release_date': '2024-01-01'}],
                    'episodes': []}

        added = []
        with mock.patch.object(dbg, 'get_all_settings', return_value={'Content Sources': {'My Plex Watchlist_1': source}}), \
             mock.patch.object(dbg, 'get_setting', return_value=False), \
             mock.patch.object(dbg, 'load_source_cache', return_value={}), \
             mock.patch.object(dbg, 'save_source_cache'), \
             mock.patch('content_checkers.plex_watchlist.get_wanted_from_plex_watchlist',
                        return_value=[([{'imdb_id': 'tt1'}, {'imdb_id': 'tt2'}], {'1080p': True})]), \
             mock.patch('metadata.metadata.process_metadata', side_effect=fake_metadata), \
             mock.patch('database.add_wanted_items', side_effect=lambda items, versions, **kw: added.append(items) or len(items)):
            dbg.get_and_add_wanted_content('My Plex Watchlist_1')
        self.assertEqual([i['imdb_id'] for i in added[0]], ['tt2'])


class TestContentSourceDetail(unittest.TestCase):
    def test_rss_sources_keep_their_feed_label(self):
        from content_checkers.content_source_detail import append_content_source_detail
        for source_type in ('My Plex RSS Watchlist', 'My Friends Plex RSS Watchlist'):
            item = {'content_source': f'{source_type}_1', 'content_source_detail': 'Friend RSS ...abc123'}
            self.assertEqual(append_content_source_detail(item, source_type=source_type)['content_source_detail'],
                             'Friend RSS ...abc123')


# --------------------------------------------------------------------------- Group 5

class TestOnboardingContentSourcesNeedAdmin(unittest.TestCase):
    def test_non_admin_is_redirected_and_tokens_are_not_returned(self):
        if _delegated_to_clean_interpreter(self):
            return
        from flask import Flask
        from routes import models
        from routes import onboarding_routes

        app = Flask(__name__)
        app.secret_key = 'test'

        @app.route('/unauthorized', endpoint='auth.unauthorized')
        def _unauthorized():
            return 'no'

        anonymous = SimpleNamespace(is_authenticated=False, role=None)
        config = {'Content Sources': {'Other Plex Watchlist_1': {'token': 'SECRET-TOKEN'}}}
        with app.test_request_context('/onboarding/content_sources/get'), \
             mock.patch.object(models, 'is_user_system_enabled', return_value=True), \
             mock.patch.object(models, 'current_user', anonymous), \
             mock.patch.object(onboarding_routes, 'load_config', return_value=config):
            response = onboarding_routes.get_onboarding_content_sources()
        self.assertEqual(getattr(response, 'status_code', None), 302)
        self.assertNotIn(b'SECRET-TOKEN', getattr(response, 'data', b''))

    def test_still_works_before_the_user_system_exists(self):
        if _delegated_to_clean_interpreter(self):
            return
        from flask import Flask
        from routes import models
        from routes import onboarding_routes

        app = Flask(__name__)
        config = {'Content Sources': {}}
        with app.test_request_context('/onboarding/content_sources/get'), \
             mock.patch.object(models, 'is_user_system_enabled', return_value=False), \
             mock.patch.object(onboarding_routes, 'load_config', return_value=config):
            response = onboarding_routes.get_onboarding_content_sources()
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()


class TestTvdbExtendedSeasonFallback(unittest.TestCase):
    def test_fallback_extraction_no_longer_raises_name_error(self):
        """The extended-response fallback used an undefined imdb_id and crashed on any episode."""
        from cli_battery.app import tvdb_client
        raw = {
            'seasons': [{'number': 1, 'type': {'id': 1}}],
            'episodes': [{'seasonNumber': 1, 'number': 1, 'name': 'Pilot', 'aired': '2020-01-01'}],
        }
        seasons = tvdb_client._extract_seasons_from_extended(raw, 'tt0000001')
        self.assertEqual(seasons[1]['episodes'][1]['title'], 'Pilot')
        # The imdb_id argument is optional (it is only used for log messages).
        self.assertIn(1, tvdb_client._extract_seasons_from_extended(raw))


class TestImportOrder(unittest.TestCase):
    def test_plex_watchlist_can_be_imported_first(self):
        """main.py imports content_checkers.plex_watchlist before routes; that must not be a circular import
        (plex_watchlist -> database -> routes -> connections_routes -> plex_watchlist)."""
        import subprocess, sys, tempfile
        code = (
            "import os, sys\n"
            "try:\n"
            "    from content_checkers.plex_watchlist import validate_plex_tokens\n"
            "    print('IMPORT_OK')\n"
            "except Exception as e:\n"
            "    print('IMPORT_FAILED', type(e).__name__, e)\n"
            "sys.stdout.flush(); os._exit(0)\n"
        )
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=repo, USER_CONFIG=os.path.join(tmp, 'config'),
                       USER_DB_CONTENT=os.path.join(tmp, 'db'), USER_LOGS=os.path.join(tmp, 'logs'))
            for d in ('config', 'db', 'logs'):
                os.makedirs(os.path.join(tmp, d), exist_ok=True)
            proc = subprocess.run([sys.executable, '-c', code], cwd=repo, env=env,
                                  capture_output=True, text=True, timeout=240)
        self.assertIn('IMPORT_OK', proc.stdout, proc.stdout[-500:] + proc.stderr[-500:])
