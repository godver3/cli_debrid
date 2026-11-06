"""A Scrob list entry limited to one season must only request that season.

Scrob lists can hold a single season of a show (list item season_number); Plex
watchlists can't. process_scrob_items used to drop the season number, so the whole
series was grabbed.
"""
import unittest
from unittest.mock import MagicMock, patch

import database  # noqa: F401  (import order: other modules hit a circular import when first)
from content_checkers import scrob

SNL_TMDB = 1667
SNL_IMDB = 'tt0072562'


def _show(tmdb_id=SNL_TMDB, season=None, media_type='series'):
    return {'id': 1, 'list_id': 1, 'media': {
        'tmdb_id': tmdb_id, 'type': media_type, 'title': 'Saturday Night Live',
        'season_number': season, 'episode_number': None,
    }}


def _process(items, unblacklist=False):
    imdb_for = {SNL_TMDB: SNL_IMDB, 2: 'tt0000002', 3: 'tt0000003'}
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None
    with patch.object(scrob, '_tmdb_to_imdb', side_effect=lambda t, m: imdb_for.get(t)), \
            patch('database.core.get_db_connection', return_value=conn):
        return scrob.process_scrob_items(items, unblacklist=unblacklist)


class TestScrobSeasonListItems(unittest.TestCase):
    def test_season_entry_requests_only_that_season(self):
        out = _process([_show(season=52)])
        self.assertEqual(out, [{'imdb_id': SNL_IMDB, 'media_type': 'tv', 'requested_seasons': [52]}])

    def test_whole_show_entry_has_no_season_limit(self):
        out = _process([_show()])
        self.assertEqual(out, [{'imdb_id': SNL_IMDB, 'media_type': 'tv'}])

    def test_several_seasons_of_one_show_merge(self):
        out = _process([_show(season=52), _show(season=50), _show(season=52)])
        self.assertEqual(out, [{'imdb_id': SNL_IMDB, 'media_type': 'tv', 'requested_seasons': [50, 52]}])

    def test_whole_show_wins_over_season_entries_in_either_order(self):
        for items in ([_show(season=52), _show()], [_show(), _show(season=52)]):
            out = _process(items)
            self.assertEqual(out, [{'imdb_id': SNL_IMDB, 'media_type': 'tv'}])

    def test_season_zero_is_kept(self):
        out = _process([_show(season=0)])
        self.assertEqual(out[0]['requested_seasons'], [0])

    def test_other_shows_are_independent(self):
        out = _process([_show(season=52), _show(tmdb_id=2)])
        self.assertEqual(out, [
            {'imdb_id': SNL_IMDB, 'media_type': 'tv', 'requested_seasons': [52]},
            {'imdb_id': 'tt0000002', 'media_type': 'tv'},
        ])

    def test_episode_entry_keeps_rolling_up_to_the_show(self):
        episode = {'id': 2, 'media': {'tmdb_id': 999, 'type': 'episode', 'show_tmdb_id': SNL_TMDB,
                                      'season_number': 52, 'episode_number': 1}}
        out = _process([episode])
        self.assertEqual(out, [{'imdb_id': SNL_IMDB, 'media_type': 'tv'}])

    def test_movie_and_junk_season_values_are_ignored(self):
        movie = {'media': {'tmdb_id': 2, 'type': 'movie', 'season_number': 3}}
        out = _process([movie, _show(tmdb_id=3, season='x'), _show(tmdb_id=SNL_TMDB, season=True)])
        self.assertEqual(out, [
            {'imdb_id': 'tt0000002', 'media_type': 'movie'},
            {'imdb_id': 'tt0000003', 'media_type': 'tv'},
            {'imdb_id': SNL_IMDB, 'media_type': 'tv'},
        ])

    def test_numeric_string_season_is_accepted(self):
        out = _process([_show(season='52')])
        self.assertEqual(out[0]['requested_seasons'], [52])


class TestRequestedSeasonsReachMetadata(unittest.TestCase):
    def test_item_cache_key_includes_the_season(self):
        """Changing the season on a list entry must not be hidden by the source cache."""
        from content_checkers.content_cache_management import create_cache_key
        base = {'imdb_id': SNL_IMDB, 'media_type': 'tv'}
        a = create_cache_key({**base, 'requested_seasons': [52]}, 'Scrob Lists_1')
        b = create_cache_key({**base, 'requested_seasons': [51]}, 'Scrob Lists_1')
        c = create_cache_key(base, 'Scrob Lists_1')
        self.assertEqual(len({a, b, c}), 3)


if __name__ == '__main__':
    unittest.main()
