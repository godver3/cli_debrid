#!/usr/bin/env python3
"""Plex watchlist sources drop blacklisted/ghostlisted titles before metadata processing."""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

from content_checkers import blocked_items
from content_checkers.source_run_report import SourceRunReport

PLEX = 'My Plex Watchlist'
VERSIONS = {'1080p': True}


class PlexBlacklistGateTest(unittest.TestCase):
    def setUp(self):
        self.db_path = os.path.join(tempfile.mkdtemp(), 'media_items.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE media_items (imdb_id TEXT, tmdb_id TEXT, type TEXT, state TEXT, ghostlisted INTEGER)")
        conn.commit()
        conn.close()
        self.manual = {}
        patches = [
            mock.patch('database.core.get_db_connection', side_effect=lambda: sqlite3.connect(self.db_path)),
            mock.patch('database.manual_blacklist.is_blacklisted', side_effect=self._manual_blacklisted),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _manual_blacklisted(self, imdb_id, season=None):
        entry = self.manual.get(imdb_id)
        if entry is None:
            return False
        return not entry.get('seasons')

    def _rows(self, *rows):
        conn = sqlite3.connect(self.db_path)
        conn.executemany("INSERT INTO media_items VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def _run(self, items, source_type=PLEX, unblacklist=False, granular=False):
        report = SourceRunReport(f'{source_type}_1', source_type)
        result = blocked_items.drop_blocked_items(
            [(items, VERSIONS)], f'{source_type}_1', source_type, unblacklist, granular, report)
        return [i['imdb_id'] for i in result[0][0]], report

    def test_blacklisted_and_ghostlisted_movies_are_dropped(self):
        self._rows(('tt1', '1', 'movie', 'Blacklisted', 0), ('tt2', '2', 'movie', 'Collected', 1),
                   ('tt3', '3', 'movie', 'Wanted', 0))
        kept, report = self._run([{'imdb_id': 'tt1', 'media_type': 'movie'},
                                  {'imdb_id': 'tt2', 'media_type': 'movie'},
                                  {'imdb_id': 'tt3', 'media_type': 'movie'},
                                  {'imdb_id': 'tt4', 'media_type': 'movie'}])
        self.assertEqual(kept, ['tt3', 'tt4'])
        self.assertEqual(report.counts['blocked'], 2)

    def test_movie_blacklisted_under_tmdb_id_only(self):
        self._rows((None, '55', 'movie', 'Blacklisted', 0))
        kept, _ = self._run([{'imdb_id': 'tt5', 'tmdb_id': 55, 'media_type': 'movie'}])
        self.assertEqual(kept, [])

    def test_show_is_dropped_only_when_every_episode_is_blocked(self):
        self._rows(('tt10', '10', 'episode', 'Blacklisted', 0), ('tt10', '10', 'episode', 'Wanted', 0),
                   ('tt11', '11', 'episode', 'Blacklisted', 0), ('tt11', '11', 'episode', 'Collected', 1))
        kept, _ = self._run([{'imdb_id': 'tt10', 'media_type': 'tv'}, {'imdb_id': 'tt11', 'media_type': 'tv'}])
        self.assertEqual(kept, ['tt10'])

    def test_unblacklist_and_granular_let_blacklisted_through_but_not_ghostlisted(self):
        self._rows(('tt1', '1', 'movie', 'Blacklisted', 0), ('tt2', '2', 'movie', 'Collected', 1))
        items = [{'imdb_id': 'tt1', 'media_type': 'movie'}, {'imdb_id': 'tt2', 'media_type': 'movie'}]
        self.assertEqual(self._run(items, unblacklist=True)[0], ['tt1'])
        self.assertEqual(self._run(items, granular=True)[0], ['tt1'])

    def test_manual_blacklist_whole_title_only(self):
        self.manual = {'tt20': {'media_type': 'movie'}, 'tt21': {'media_type': 'episode', 'seasons': [2]}}
        kept, _ = self._run([{'imdb_id': 'tt20', 'media_type': 'movie'}, {'imdb_id': 'tt21', 'media_type': 'tv'}],
                            unblacklist=True)
        self.assertEqual(kept, ['tt21'])

    def test_movie_rows_do_not_block_a_show_with_the_same_id(self):
        self._rows(('tt30', '30', 'movie', 'Blacklisted', 0))
        kept, _ = self._run([{'imdb_id': 'tt30', 'media_type': 'tv'}])
        self.assertEqual(kept, ['tt30'])

    def test_non_plex_sources_are_untouched(self):
        self._rows(('tt1', '1', 'movie', 'Blacklisted', 0))
        kept, _ = self._run([{'imdb_id': 'tt1', 'media_type': 'movie'}], source_type='MDBList')
        self.assertEqual(kept, ['tt1'])

    def test_db_failure_keeps_every_item(self):
        with mock.patch('database.core.get_db_connection', side_effect=sqlite3.OperationalError('locked')):
            kept, _ = self._run([{'imdb_id': 'tt1', 'media_type': 'movie'}])
        self.assertEqual(kept, ['tt1'])

    def test_blocked_titles_show_in_the_run_summary(self):
        self._rows(('tt1', '1', 'movie', 'Blacklisted', 0))
        _, report = self._run([{'imdb_id': 'tt1', 'title': 'Some Movie', 'media_type': 'movie'}])
        with self.assertLogs(level='INFO') as logs:
            report.log()
        joined = '\n'.join(logs.output)
        self.assertIn('blocked=1', joined)
        self.assertIn("Blacklisted (skipped before metadata processing): 'Some Movie'", joined)


if __name__ == '__main__':
    unittest.main()
