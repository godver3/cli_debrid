import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import patch

from utilities import library_recommendations as lr
from utilities import viewer_history as vh


def _guid(value):
    return NS(id=value)


class FakePlex:
    def __init__(self, *args, **kwargs):
        movie = NS(ratingKey=10, title='Heat', guids=[_guid('tmdb://949'), _guid('imdb://tt0113277')])
        show = NS(ratingKey=20, title='Severance', guids=[_guid('tmdb://95396')])
        # Plex sometimes has no external ids at all; cli_debrid's own DB fills in
        bare = NS(ratingKey=40, title='Veep', year=2012, guids=[])
        self.library = NS(sections=lambda: [
            NS(type='movie', all=lambda: [movie]),
            NS(type='show', all=lambda: [show, bare]),
            NS(type='artist', all=lambda: []),
        ])

    def systemAccounts(self):
        return [NS(id=0, name=''), NS(id=1, name='ryan'), NS(id=5, name='sam')]

    def history(self, mindate=None):
        t = datetime(2026, 10, 1, 20, 0)
        return [
            NS(type='movie', ratingKey=10, accountID=1, viewedAt=t),
            # real history entries carry grandparentKey, not grandparentRatingKey
            NS(type='episode', ratingKey=21, grandparentRatingKey=None,
               grandparentKey='/library/metadata/20', accountID=5, viewedAt=t),
            NS(type='episode', ratingKey=21, grandparentRatingKey=None,
               grandparentKey='/library/metadata/20', accountID=5, viewedAt=datetime(2026, 9, 1)),
            NS(type='episode', ratingKey=22, grandparentRatingKey=20, accountID=5, viewedAt=datetime(2026, 10, 2)),
            NS(type='episode', ratingKey=99, grandparentRatingKey=98, accountID=5, viewedAt=t),  # deleted show
            NS(type='track', ratingKey=30, accountID=1, viewedAt=t),
            NS(type='episode', ratingKey=41, grandparentKey='/library/metadata/40', accountID=1, viewedAt=t),
        ]


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeEmbySession:
    def __init__(self):
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        path = url.split(':8096', 1)[1]
        if path == '/System/Info/Public':
            return FakeResponse({'ProductName': 'Jellyfin Server'})
        if path == '/Users':
            return FakeResponse([
                {'Id': 'u1', 'Name': 'kid', 'Policy': {'IsAdministrator': False}},
                {'Id': 'u2', 'Name': 'admin', 'Policy': {'IsAdministrator': True}},
            ])
        if path == '/Users/u2/Items' and params.get('Ids'):
            return FakeResponse({'Items': [{'Id': 's1', 'Name': 'Bluey', 'ProviderIds': {'Tmdb': '82728'}}]})
        if path == '/Users/u2/Items':
            return FakeResponse({'Items': [
                {'Type': 'Movie', 'Name': 'Up', 'ProviderIds': {'Tmdb': '14160'},
                 'UserData': {'LastPlayedDate': '2026-10-01T10:00:00.0000000Z'}},
                {'Type': 'Episode', 'SeriesId': 's1', 'SeriesName': 'Bluey', 'ProviderIds': {'Tmdb': 'ep'},
                 'UserData': {'LastPlayedDate': '2026-10-03T10:00:00Z'}},
            ]})
        return FakeResponse({'Items': []})


class ViewerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.settings = {}
        p = patch.object(vh, 'get_setting', side_effect=lambda s, k, d='': self.settings.get((s, k), d))
        p.start()
        self.addCleanup(p.stop)

    def test_plex_sync_stores_every_account_by_show(self):
        self.settings = {('Plex', 'url'): 'http://plex', ('Plex', 'token'): 't'}
        fake_server = types.ModuleType('plexapi.server')
        fake_server.PlexServer = FakePlex
        with patch.dict(sys.modules, {'plexapi.server': fake_server}), \
                patch.object(vh, '_library_ids_by_title', return_value={('tv', 'veep', '2012'): ('2947', 'tt1759761')}):
            self.assertEqual(vh.sync_plex(), (2, 3))

        viewers = vh.get_viewers()
        self.assertEqual([(v['id'], v['is_main']) for v in viewers], [('plex:1', True), ('plex:5', False)])
        self.assertEqual(vh.main_viewer_id(), 'plex:1')
        sam = vh.get_plays('plex:5')
        self.assertEqual(len(sam), 1)
        self.assertEqual(sam[0][:4], ('tv', '95396', None, 'Severance'))
        self.assertEqual(sam[0][5:], (3, 2))  # three plays of two distinct episodes
        self.assertEqual(sam[0][4], '2026-10-02 00:00:00')
        self.assertEqual(len(vh.get_plays('all')), 3)
        self.assertIn(('tv', '2947'), {(r[0], r[1]) for r in vh.get_plays('plex:1')})

    def test_emby_sync_uses_series_ids_and_first_admin_is_main(self):
        self.settings = {('Debug', 'emby_jellyfin_url'): 'http://jf:8096/', ('Debug', 'emby_jellyfin_token'): 'k'}
        with patch.object(vh.requests, 'Session', FakeEmbySession):
            self.assertEqual(vh.sync_emby(), (2, 2))

        self.assertEqual(vh.main_viewer_id(), 'jellyfin:u2')
        # the kid has no plays, so isn't offered
        self.assertEqual([v['id'] for v in vh.get_viewers()], ['jellyfin:u2'])
        plays = {r[1]: r for r in vh.get_plays('jellyfin:u2')}
        self.assertEqual(plays['82728'][:4], ('tv', '82728', None, 'Bluey'))
        self.assertEqual(plays['14160'][0], 'movie')

    def test_unconfigured_sources_are_skipped_and_keep_old_rows(self):
        vh._store('plex', [(1, 'ryan', True)], {('1', 'movie', '949', None): {'title': 'Heat', 'last': None, 'plays': 1}})
        vh.sync_all()
        self.assertEqual(len(vh.get_plays('plex:1')), 1)


class PerViewerRecommendationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        for name, value in (('_db_dir', self.tmp.name), ('_CACHE_FILE', os.path.join(self.tmp.name, 'c.json')),
                            ('_file_cache', None), ('_result_cache', {})):
            p = patch.object(lr, name, value)
            p.start()
            self.addCleanup(p.stop)
        recs = {
            'movie/949/': [{'id': 1, 'poster_path': '/p', 'vote_count': 50}, {'id': 500, 'poster_path': '/p', 'vote_count': 50}],
            'tv/95396/': [{'id': 2, 'poster_path': '/p', 'vote_count': 50}],
            'movie/27205/': [{'id': 3, 'poster_path': '/p', 'vote_count': 50}],
        }
        fake = lambda url, timeout=10: {'results': next((v for k, v in recs.items() if k in url), [])}
        for target, kwargs in (('get_tmdb_json', {'side_effect': fake}),
                               ('_library_rows', {'return_value': [
                                   ('movie', '500', 1, 'Pulp Fiction', None),
                                   ('movie', '27205', 1, 'Inception', datetime.now().isoformat())]})):
            p = patch.object(lr, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch('database.manual_blacklist.get_manual_blacklist', return_value={})
        p.start()
        self.addCleanup(p.stop)
        now = datetime.now()
        vh._store('plex', [(1, 'ryan', True), (5, 'sam', False)], {
            ('1', 'movie', '949', None): {'title': 'Heat', 'last': now, 'plays': 1},
            ('5', 'tv', '95396', None): {'title': 'Severance', 'last': now, 'plays': 3},
            ('5', 'movie', '949', None): {'title': 'Heat', 'last': now, 'plays': 1},
        })

    def run_for(self, viewer):
        with patch.object(lr, 'get_setting', return_value='key'):
            return sorted((r['media_type'], r['id']) for r in lr.get_library_recommendations(viewer))

    def test_main_user_gets_own_history_plus_collection_without_library_titles(self):
        self.assertEqual(self.run_for(None), [('movie', 1), ('movie', 3)])
        # picking the main user's own id is the same list
        self.assertEqual(self.run_for('plex:1'), [('movie', 1), ('movie', 3)])

    def test_other_user_gets_their_history_only_without_library_titles(self):
        self.assertEqual(self.run_for('plex:5'), [('movie', 1), ('tv', 2)])

    def test_everyone_merges_histories(self):
        self.assertEqual(self.run_for('all'), [('movie', 1), ('movie', 3), ('tv', 2)])


if __name__ == '__main__':
    unittest.main()
