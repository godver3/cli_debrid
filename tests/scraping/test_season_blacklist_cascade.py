#!/usr/bin/env python3
"""Old-season blacklist cascade must spare siblings the scrape proved exist.

Real Housewives of Amsterdam S04 (aired 2025): S04E03 found nothing anywhere, so
handle_no_results blacklisted it *and every other S04 episode* before any of them
had been searched. Yet S04E03's own season-pack search had just returned the
NinjaCentral releases for E05/E06/E07/E09/E10/E12, rejected only as "Single
episode result when searching for multi". Those siblings must stay in Scraping
and get their own search.
"""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from queues.blacklisted_queue import BlacklistedQueue
from queues.scraping_queue import ScrapingQueue, _season_episodes_seen


def _result(title, reason, seasons, episodes):
    return {
        'title': title,
        'filter_reason': reason,
        'parsed_info': {
            'seasons': seasons,
            'episodes': episodes,
            'season_episode_info': {'seasons': seasons, 'episodes': episodes},
        },
    }


SINGLE_IN_MULTI = "Single episode result when searching for multi"

SEASON_SEARCH = [
    _result('The.Real.Housewives.Of.Amsterdam.S04E12.DUTCH.1080p.WEB.h264-TRIPEL', SINGLE_IN_MULTI, [4], [12]),
    _result('The.Real.Housewives.Of.Amsterdam.S04E09.DUTCH.1080p.WEB.h264-TRIPEL', SINGLE_IN_MULTI, [4], [9]),
    _result('The.Real.Housewives.Of.Amsterdam.S04E05.DUTCH.1080p.WEB.h264-TRIPEL', SINGLE_IN_MULTI, [4], [5]),
    # Wrong show: says nothing about Amsterdam's episodes.
    _result('The Real Housewives of Miami S04E02 1080p HEVC x265-MeGusta',
            "Different title: 'the.real.housewives.of.miami' swaps a word of "
            "'the.real.housewives.of.amsterdam' (similarity 0.85)", [4], [2]),
    # Right show, wrong quality: not proof a wanted release exists.
    _result('The.Real.Housewives.of.Amsterdam.S04E04.2160p.WEB', "Resolution mismatch (max: 1080p, wanted: <=)", [4], [4]),
    # Multi-season pack: can't attribute episode numbers to S04.
    _result('The.Real.Housewives.of.Amsterdam.S01-S04', "Multi-season pack when searching for single episode", [1, 2, 3, 4], []),
]


class SeasonEpisodesSeenTest(unittest.TestCase):
    def test_only_shape_rejects_and_passes_count(self):
        self.assertEqual(_season_episodes_seen(4, SEASON_SEARCH), {5, 9, 12})

    def test_passed_results_count(self):
        passed = [_result('Show.S04E07.1080p', 'Passed all filters', [4], [7]),
                  {'title': 'no reason', 'parsed_info': {'seasons': [4], 'episodes': [8]}}]
        self.assertEqual(_season_episodes_seen(4, passed), {7, 8})

    def test_other_season_ignored(self):
        self.assertEqual(_season_episodes_seen(3, SEASON_SEARCH), set())

    def test_no_season(self):
        self.assertEqual(_season_episodes_seen(None, SEASON_SEARCH), set())


def _ep(item_id, episode):
    return {'id': item_id, 'type': 'episode', 'imdb_id': 'tt21424426', 'season_number': 4,
            'episode_number': episode, 'version': '1080p or less', 'release_date': '2025-12-01',
            'title': 'The Real Housewives of Amsterdam'}


class CascadeExemptionTest(unittest.TestCase):
    def _cascade(self, exempt):
        bq = BlacklistedQueue()
        failed = _ep(3, 3)
        siblings = [_ep(i, i) for i in (1, 2, 4, 5, 9, 12)]
        qm = mock.Mock()
        qm.get_item_queue.return_value = 'Scraping'
        qm.generate_identifier.side_effect = lambda it: f"S04E{it['episode_number']:02d}"
        with mock.patch.object(BlacklistedQueue, 'find_related_season_items', return_value=siblings), \
             mock.patch.object(BlacklistedQueue, 'is_item_old', return_value=True), \
             mock.patch.object(BlacklistedQueue, 'blacklist_item') as bl:
            bq.blacklist_old_season_items(failed, qm, exempt_episodes=exempt)
        return sorted(c.args[0]['episode_number'] for c in bl.call_args_list)

    def test_seen_siblings_not_blacklisted(self):
        self.assertEqual(self._cascade({5, 9, 12}), [1, 2, 3, 4])

    def test_no_evidence_keeps_old_behaviour(self):
        self.assertEqual(self._cascade(None), [1, 2, 3, 4, 5, 9, 12])


class HandleNoResultsWiringTest(unittest.TestCase):
    def test_scrape_evidence_reaches_cascade_and_is_consumed(self):
        q = ScrapingQueue()
        item = dict(_ep(3, 3), state='Scraping')
        q.add_item(dict(item))
        q._note_season_episodes_seen(item, SEASON_SEARCH)
        qm = mock.MagicMock()
        qm.generate_identifier.return_value = 'S04E03'
        with mock.patch.object(ScrapingQueue, 'is_item_old', return_value=True), \
             mock.patch.object(ScrapingQueue, 'reset_not_wanted_check'):
            q.handle_no_results(item, qm)
        cascade = qm.queues.__getitem__.return_value.blacklist_old_season_items
        cascade.assert_called_once()
        self.assertEqual(cascade.call_args.kwargs['exempt_episodes'], {5, 9, 12})
        self.assertNotIn(3, q._season_episodes_seen)

    def test_movies_record_nothing(self):
        q = ScrapingQueue()
        q._note_season_episodes_seen({'id': 1, 'type': 'movie', 'season_number': None}, SEASON_SEARCH)
        self.assertEqual(q._season_episodes_seen, {})


if __name__ == '__main__':
    unittest.main()
