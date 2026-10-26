"""Deleting an item with "remove from content source" also removes it from the user's Trakt watchlist.

The delete path called content_checkers.trakt.remove_from_trakt_watchlist, which
never existed, behind a setting (Trakt.trakt_get_watchlist) that didn't exist
either, so a deleted watchlist item stayed on the watchlist. Its source check also
compared against 'Trakt' while source types are full names ('Trakt Watchlist').
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _settings(sources):
    def fake(section, key=None, default=None):
        if section == 'Content Sources' and key is None:
            return sources
        return default
    return fake


class TraktWatchlistRemovalTest(unittest.TestCase):
    def setUp(self):
        import database  # noqa: F401  (app import order; trakt first is circular)
        import content_checkers.trakt as trakt
        import utilities.deletion_manager as dm_mod
        self.trakt, self.dm_mod = trakt, dm_mod
        self.addCleanup(mock.patch.stopall)
        mock.patch('database.content_source_retry.add_to_retry_queue').start()

    def _remove(self, sources, item):
        mock.patch.object(self.dm_mod, 'get_setting', side_effect=_settings(sources)).start()
        return self.dm_mod.DeletionManager().remove_from_content_source(item)

    def test_sync_remover_posts_to_watchlist_endpoint(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'deleted': {'movies': 1}, 'not_found': {}}
        with mock.patch.object(self.trakt, 'make_trakt_request', return_value=response) as req:
            result = self.trakt.remove_from_trakt_watchlist([{'tmdb_id': 5, 'type': 'movie', 'title': 'M'}])
        req.assert_called_once_with('post', '/sync/watchlist/remove', data={'movies': [{'ids': {'tmdb': 5}}]})
        self.assertEqual((result['success'], result['removed']), (True, 1))

    def test_delete_removes_watchlist_item(self):
        sources = {'Trakt Watchlist_1': {'type': 'Trakt Watchlist', 'enabled': True}}
        item = {'tmdb_id': 5, 'imdb_id': 'tt5', 'type': 'movie', 'title': 'M', 'content_source': 'Trakt Watchlist_1'}
        with mock.patch.object(self.trakt, 'remove_from_trakt_watchlist',
                               return_value={'success': True, 'removed': 1, 'message': 'ok'}) as remove:
            result = self._remove(sources, item)
        remove.assert_called_once()
        self.assertIn('Trakt_Watchlist', result['sources_succeeded'])

    def test_item_from_another_source_is_left_alone(self):
        sources = {'Trakt Watchlist_1': {'type': 'Trakt Watchlist', 'enabled': True},
                   'Overseerr_1': {'type': 'Overseerr', 'enabled': False}}
        item = {'tmdb_id': 5, 'imdb_id': 'tt5', 'type': 'movie', 'title': 'M', 'content_source': 'Overseerr_1'}
        with mock.patch.object(self.trakt, 'remove_from_trakt_watchlist') as remove:
            self._remove(sources, item)
        remove.assert_not_called()

    def test_friends_watchlist_alone_does_not_touch_users_watchlist(self):
        sources = {'Friends Trakt Watchlist_1': {'type': 'Friends Trakt Watchlist', 'enabled': True}}
        item = {'tmdb_id': 5, 'type': 'movie', 'title': 'M', 'content_source': 'Friends Trakt Watchlist_1'}
        with mock.patch.object(self.trakt, 'remove_from_trakt_watchlist') as remove:
            self._remove(sources, item)
        remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
