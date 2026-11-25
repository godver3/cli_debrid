"""scrape_with_fallback matches releases by the aired S/E for shows set to a
non-default episode order (Dragon Ball Kai S5E1 = aired S2E1): scraper.scrape
searches that way, so every release comes back named in aired numbers."""
import unittest
from unittest import mock

import database  # noqa: F401 - app import order
from queues.scraping_queue import ScrapingQueue


def _result(title, season, episode):
    return {'original_title': title, 'title': title, 'magnet': f'magnet:?xt=urn:btih:{"a" * 40}',
            'parsed_info': {'season_episode_info': {'seasons': [season], 'episodes': [episode]}}}


class _Mapping:
    def aired_of(self, season, episode):
        return {(5, 1): (2, 1)}.get((season, episode))


ITEM = {'id': 29203, 'imdb_id': 'tt1409055', 'tmdb_id': 61709, 'title': 'Dragon Ball Kai', 'year': 2009,
        'type': 'episode', 'version': '1080p', 'season_number': 5, 'episode_number': 1, 'genres': ['anime']}


class FallbackEpisodeOrderTest(unittest.TestCase):
    def run_fallback(self, results, mapping):
        qm = mock.Mock()
        qm.generate_identifier.return_value = 'episode_Dragon Ball Kai_tt1409055_S05E01_1080p'
        sq = ScrapingQueue.__new__(ScrapingQueue)
        with mock.patch('queues.scraping_queue.scrape', return_value=(results, [])), \
             mock.patch('database.get_media_item_by_id', return_value={}), \
             mock.patch.object(ScrapingQueue, '_note_season_episodes_seen', create=True), \
             mock.patch('scraper.functions.episode_order_mapping.order_mapping', return_value=mapping), \
             mock.patch('database.database_reading.get_all_season_episode_counts', return_value={}):
            out = sq.scrape_with_fallback(dict(ITEM), False, qm, skip_filter=True)
        return out[0] if isinstance(out, tuple) else out

    def test_aired_numbered_release_matches(self):
        got = self.run_fallback([_result('Dragon.Ball.Kai.S02E01.1080p.BluRay', 2, 1)], _Mapping())
        self.assertEqual([r['original_title'] for r in got], ['Dragon.Ball.Kai.S02E01.1080p.BluRay'])

    def test_order_numbered_release_is_not_taken(self):
        # "S05E01" in aired numbering is another episode.
        got = self.run_fallback([_result('Dragon.Ball.Kai.S05E01.1080p.BluRay', 5, 1)], _Mapping())
        self.assertEqual(got, [])

    def test_default_order_unchanged(self):
        got = self.run_fallback([_result('Show.S05E01.1080p', 5, 1)], None)
        self.assertEqual([r['original_title'] for r in got], ['Show.S05E01.1080p'])


if __name__ == '__main__':
    unittest.main()
