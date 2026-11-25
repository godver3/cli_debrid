"""Manual assign of an NZB season pack.

Picking Seasons (or the whole show) and giving an NZB used to create one
library item with no season or episode: the selection was ignored, so only
the one episode the job resolved to got assigned (The Traitors (IN) S02,
2026-11-25: only S02E03). Each episode of the picked seasons now gets its own
item on the job, for an NZB URL and an uploaded .nzb alike.

The page posts media_type=show, and get_metadata only includes seasons for
'tv', so the fix above still found no episodes on the real page ("No episodes
found for the selected seasons"). The metadata stub below behaves the same way.
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests._patching import patch_in_package  # noqa: E402
from tests.matching.test_manual_assign_page import _fresh_app_modules  # noqa: E402

SEASONS = {1: {'episodes': {1: {'title': 'One'}, 2: {'title': 'Two'}}},
           2: {'episodes': {1: {'title': 'A'}, 2: {'title': 'B'}, 3: {'title': 'C'}}}}


def _metadata(**kw):
    """Like get_metadata: seasons only when asked for 'tv'."""
    meta = {'imdb_id': 'tt1', 'title': 'The Traitors (IN)'}
    if kw.get('item_media_type') == 'tv':
        meta['seasons'] = SEASONS
    return meta


NZB = '<?xml version="1.0"?><nzb><file subject="x"><segments><segment>a@b</segment></segments></file></nzb>'


class ManualAssignNzbRouteTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask, jsonify
        import routes.magnet_routes as mr
        import routes.models as models
        import routes.scraper_routes as sr
        app = Flask(__name__)
        app.register_blueprint(mr.magnet_bp, url_prefix='/magnet')
        self.client = app.test_client()
        self.app = app
        self.add = mock.Mock(side_effect=lambda **kw: jsonify({'success': True, 'job_id': 'J'}))
        for patcher in [mock.patch.object(models, 'is_user_system_enabled', return_value=False),
                        patch_in_package(mr, '_saved_episode_order', return_value='default'),
                        mock.patch.object(sr, '_add_nzb_to_usenet', self.add),
                        mock.patch('metadata.metadata.get_metadata', side_effect=_metadata)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, **form):
        # The page posts 'show' (result.mediaType), not 'tv'.
        base = {'tmdb_id': '271489', 'media_type': 'show', 'title': 'The Traitors (IN)', 'year': '2025',
                'version': '1080p'}
        base.update(form)
        return self.client.post('/magnet/prepare_manual_assignment', data=base,
                                content_type='multipart/form-data')

    def test_uploaded_nzb_for_a_season_creates_every_episode(self):
        response = self._post(selection_type='seasons', selected_seasons='2', season='', episode='',
                              torrent_file=(io.BytesIO(NZB.encode()), 'The.Traitors.S02.nzb'))
        self.assertTrue(response.get_json()['success'])
        kw = self.add.call_args.kwargs
        self.assertEqual(kw['episode_list'], [(2, 1), (2, 2), (2, 3)])
        self.assertEqual(kw['nzb_content'], NZB)
        self.assertIsNone(kw['nzb_url'])
        self.assertEqual(kw['original_scraped_torrent_title'], 'The.Traitors.S02.nzb')
        self.assertIsNone(kw['episode'])

    def test_nzb_url_for_the_whole_show(self):
        self._post(selection_type='all', magnet_link='https://indexer/getnzb/1')
        kw = self.add.call_args.kwargs
        self.assertEqual(kw['episode_list'], [(1, 1), (1, 2), (2, 1), (2, 2), (2, 3)])
        self.assertEqual(kw['nzb_url'], 'https://indexer/getnzb/1')

    def test_single_episode_unchanged(self):
        self._post(selection_type='episode', season='2', episode='3', magnet_link='https://indexer/getnzb/1')
        kw = self.add.call_args.kwargs
        self.assertIsNone(kw['episode_list'])
        self.assertEqual((kw['season'], kw['episode']), (2, 3))


class AddNzbEpisodeListTests(unittest.TestCase):
    """_add_nzb_to_usenet makes one Adding item per listed episode, on one job."""

    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)

    def test_one_item_per_listed_episode(self):
        from flask import Flask
        import routes.scraper_routes.usenet_add as ua
        import database.database_writing as dw
        import database.not_wanted_magnets as nwm
        client = mock.Mock(is_enabled=mock.Mock(return_value=True),
                           add_nzb_content=mock.Mock(return_value='J1'), last_missing_segments=None)
        added = []
        with mock.patch('usenet.get_usenet_client', return_value=client), \
                mock.patch('usenet.reset_usenet_client'), \
                mock.patch('metadata.metadata.get_metadata', side_effect=_metadata), \
                mock.patch.object(nwm, 'is_nzb_segment_not_wanted', return_value=False), \
                mock.patch.object(dw, 'add_media_item', side_effect=lambda item: added.append(item) or len(added)), \
                mock.patch.object(dw, 'update_media_item'), \
                mock.patch.object(dw, 'update_media_item_state') as state, \
                mock.patch.object(ua, '_record_manual_grab'), \
                mock.patch.object(ua, '_build_nzb_title', return_value='The Traitors S02'), \
                mock.patch('queues.queue_manager.QueueManager'), \
                Flask(__name__).app_context():
            ua._add_nzb_to_usenet(None, 'The Traitors (IN)', 2025, 'show', None, None, '1080p', '271489',
                                  nzb_content=NZB, episode_list=[(2, 1), (2, 2), (2, 3)])
        client.add_nzb_content.assert_called_once()
        self.assertEqual([(i['season_number'], i['episode_number'], i['episode_title']) for i in added],
                         [(2, 1, 'A'), (2, 2, 'B'), (2, 3, 'C')])
        self.assertEqual(state.call_count, 3)


if __name__ == '__main__':
    unittest.main()
