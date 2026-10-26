"""ReNZB reinsert, Step 2 (re-scrape) when there's no usable stored NZB URL.

The fallback imported usenet.repair_engine._submit_replacement, which had been
renamed to _submit_and_confirm_replacement (returning (job_id, title,
disposition)). The ImportError was swallowed by the step's try/except, so every
reinsert without a working stored URL failed with "re-scrape found no results".
"""
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_FRESH_PREFIXES = ('flask', 'werkzeug', 'routes', 'database', 'content_checkers', 'cli_battery',
                   'utilities', 'scraper', 'queues', 'metadata', 'debrid', 'PTT', 'usenet')


def _fresh_app_modules():
    # Other tests leave stand-ins for flask and project modules in sys.modules
    patcher = mock.patch.dict(sys.modules)
    patcher.start()
    for name in [n for n in sys.modules if n.split('.')[0] in _FRESH_PREFIXES]:
        del sys.modules[name]
    import database  # noqa: F401  (app import order; routes first is circular)
    return patcher


def _item_db():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        "CREATE TABLE media_items (id INTEGER PRIMARY KEY, title TEXT, year INTEGER, type TEXT, "
        "imdb_id TEXT, tmdb_id TEXT, season_number INTEGER, episode_number INTEGER, version TEXT, "
        "genres TEXT, filled_by_torrent_id TEXT, scrape_results TEXT)")
    conn.execute("INSERT INTO media_items VALUES (7, 'Show', 2020, 'episode', 'tt1', '1', 1, 2, "
                 "'1080p', '[]', 'nzb:old', '[]')")
    return conn


class ReNzbRescrapeFallbackTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_fresh_app_modules().stop)
        from flask import Flask
        import routes.debrid_manager_routes as dm
        import routes.models as models
        import database.core
        import usenet
        import usenet.repair_engine as re_mod
        self.dm, self.re_mod = dm, re_mod
        app = Flask(__name__)
        app.register_blueprint(dm.debrid_manager_bp, url_prefix='/debrid_manager')
        self.client = app.test_client()

        conn = _item_db()
        for patcher in [
            mock.patch.object(models, 'is_user_system_enabled', return_value=False),
            mock.patch.object(database.core, 'get_db_connection', return_value=conn),
            mock.patch.object(usenet, 'get_usenet_client', return_value=mock.Mock()),
            mock.patch.object(usenet, 'reset_usenet_client'),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self):
        return self.client.post('/debrid_manager/api/nzb/reinsert',
                                json={'item_id': 7, 'nzb_url': '', 'nzb_title': 'Show.S01E02.Broken'})

    def test_skips_junk_and_rejected_candidates_and_submits_the_next(self):
        junk = {'title': 'junk'}
        rejected = {'title': 'Show.S01E02.Rejected', 'nzb_url': 'http://x/1'}
        good = {'title': 'Show.S01E02.Good', 'nzb_url': 'http://x/2'}
        submit = mock.Mock(side_effect=[(None, '', 'rejected'), ('job-2', 'Show.S01E02.Good.Named', 'confirmed')])
        with mock.patch.object(self.re_mod, '_scrape_for_replacement', return_value=[junk, rejected, good]), \
             mock.patch.object(self.re_mod, '_is_junk_replacement_result', side_effect=lambda r: r is junk), \
             mock.patch.object(self.re_mod, '_submit_and_confirm_replacement', submit), \
             mock.patch.object(self.re_mod, '_update_db_for_repair') as update_db:
            resp = self._post()

        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(body['job_id'], 'job-2')
        self.assertEqual(body['method'], 'rescrape')
        self.assertEqual(body['new_release_title'], 'Show.S01E02.Good.Named')
        self.assertEqual([c.args[0]['title'] for c in submit.call_args_list],
                         ['Show.S01E02.Rejected', 'Show.S01E02.Good'])
        item, job_id, best, _results = update_db.call_args.args
        self.assertEqual((item['id'], job_id, best['title']), (7, 'job-2', 'Show.S01E02.Good.Named'))
        # The indexer's title stays the exclusion key for later repairs
        self.assertEqual(best['original_title'], 'Show.S01E02.Good')

    def test_stops_after_max_submits(self):
        candidates = [{'title': f'Show.S01E02.R{i}', 'nzb_url': f'http://x/{i}'} for i in range(6)]
        submit = mock.Mock(return_value=('job-x', '', 'failed'))
        with mock.patch.object(self.re_mod, '_scrape_for_replacement', return_value=candidates), \
             mock.patch.object(self.re_mod, '_is_junk_replacement_result', return_value=False), \
             mock.patch.object(self.re_mod, '_submit_and_confirm_replacement', submit), \
             mock.patch.object(self.re_mod, '_update_db_for_repair') as update_db:
            resp = self._post()

        self.assertFalse(resp.get_json()['success'])
        self.assertEqual(submit.call_count, self.dm._RENZB_MAX_RESCRAPE_SUBMITS)
        update_db.assert_not_called()

    def test_all_candidates_rejected_reports_failure(self):
        with mock.patch.object(self.re_mod, '_scrape_for_replacement', return_value=[{'title': 'a'}]), \
             mock.patch.object(self.re_mod, '_is_junk_replacement_result', return_value=False), \
             mock.patch.object(self.re_mod, '_submit_and_confirm_replacement',
                               return_value=(None, '', 'failed')), \
             mock.patch.object(self.re_mod, '_update_db_for_repair') as update_db:
            resp = self._post()

        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.get_json()['success'])
        update_db.assert_not_called()


if __name__ == '__main__':
    unittest.main()
