#!/usr/bin/env python3
"""Indexer download-limit cooldown (usenet/nzb_fetch_cooldown.py).

Reported 2026-09-26: NZBPlanet answered every getnzb with HTTP 503 (download
limit). Each attempt fetched the URL three times (pre-check, cli_mount's URL
fetch, direct-upload fallback), nothing was recorded, and the next scrape a few
minutes later picked the same release again, so the item retried indefinitely
and kept the indexer pinned at its limit.

Only an indexer *limit* answer may trigger the cooldown; missing articles,
ffprobe failures and other errors must keep their existing handling.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

from usenet import nzb_fetch_cooldown as cd

LIMIT_XML = '<?xml version="1.0"?><error code="501" description="Download limit reached"/>'
NZB_XML = '<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'


class TestCooldownModule(unittest.TestCase):
    def setUp(self):
        cd.reset_fetch_cooldowns()

    def test_limit_responses(self):
        self.assertEqual(cd.classify_refusal(429, {}, ''), cd.COOLDOWN_SECONDS)
        self.assertEqual(cd.classify_refusal(200, {}, LIMIT_XML), cd.COOLDOWN_SECONDS)
        self.assertEqual(cd.classify_refusal(
            200, {}, '<error code="500" description="Request limit reached"/>'), cd.COOLDOWN_SECONDS)
        # Code alone is enough (spec codes 500/501, and 429 used by some indexers) ...
        self.assertTrue(cd.classify_refusal(200, {}, '<error code="429" description="Slow down"/>'))
        # ... and so is limit wording under another code.
        self.assertTrue(cd.classify_refusal(
            200, {}, '<error code="100" description="Daily API quota exceeded"/>'))
        self.assertTrue(cd.classify_refusal(
            200, {}, '<error description="Maximum grabs reached for today" code="102"/>'))

    def test_plain_and_json_bodies(self):
        self.assertTrue(cd.classify_refusal(200, {}, 'You have reached your download limit for today.'))
        self.assertTrue(cd.classify_refusal(403, {}, '{"message": "Grab limit reached"}'))
        self.assertTrue(cd.classify_refusal(402, {}, '<html>Download quota exceeded</html>'))

    def test_bare_503_is_short(self):
        self.assertEqual(cd.classify_refusal(503, {}, 'Service Unavailable'), cd.UNAVAILABLE_COOLDOWN_SECONDS)
        self.assertEqual(cd.classify_refusal(503, {}, 'Download limit reached'), cd.COOLDOWN_SECONDS)

    def test_retry_after_is_honoured_and_clamped(self):
        self.assertEqual(cd.classify_refusal(429, {'Retry-After': '600'}, ''), 600)
        self.assertEqual(cd.classify_refusal(503, {'Retry-After': '5'}, ''), 60)
        self.assertEqual(cd.classify_refusal(429, {'Retry-After': '86400'}, ''), 3600)
        self.assertEqual(cd.classify_refusal(429, {'Retry-After': 'garbage'}, ''), cd.COOLDOWN_SECONDS)

    def test_non_limit_errors_are_ignored(self):
        self.assertIsNone(cd.classify_refusal(404, {}, 'not found, rate limit page'))
        self.assertIsNone(cd.classify_refusal(500, {}, 'boom'))
        self.assertIsNone(cd.classify_refusal(200, {}, '<error code="300" description="No such item"/>'))
        self.assertIsNone(cd.classify_refusal(200, {}, '<error code="100" description="Incorrect user credentials"/>'))
        self.assertIsNone(cd.classify_refusal(200, {}, '<html>Generated page, moderate traffic</html>'))

    def test_longer_cooldown_is_not_shortened(self):
        url = 'https://api.nzbplanet.net/getnzb/abc.nzb'
        with mock.patch.object(cd.time, 'monotonic', return_value=1000.0):
            cd.record_indexer_limit(url, cd.COOLDOWN_SECONDS)
            cd.record_indexer_limit(url, cd.UNAVAILABLE_COOLDOWN_SECONDS)
        with mock.patch.object(cd.time, 'monotonic', return_value=1000.0 + cd.UNAVAILABLE_COOLDOWN_SECONDS + 1):
            self.assertIsNotNone(cd.cooldown_reason(url))

    def test_cooldown_is_per_indexer_host(self):
        cd.record_indexer_limit('https://api.nzbplanet.net/getnzb/abc.nzb&i=1&r=key')
        self.assertIsNotNone(cd.cooldown_reason('https://api.nzbplanet.net/getnzb/other.nzb&i=1'))
        self.assertIsNone(cd.cooldown_reason('https://api.nzbgeek.info/api?t=get&id=x'))

    def test_prowlarr_indexers_are_separate(self):
        cd.record_indexer_limit('http://prowlarr:9696/3/download?apikey=k&link=a')
        self.assertIsNotNone(cd.cooldown_reason('http://prowlarr:9696/3/download?apikey=k&link=b'))
        self.assertIsNone(cd.cooldown_reason('http://prowlarr:9696/7/download?apikey=k&link=c'))

    def test_prowlarr_behind_url_base(self):
        cd.record_indexer_limit('https://host/prowlarr/3/download?apikey=k&link=a')
        self.assertIsNotNone(cd.cooldown_reason('https://host/prowlarr/3/download?link=b'))
        self.assertIsNone(cd.cooldown_reason('https://host/prowlarr/7/download?link=c'))

    def test_cooldown_expires(self):
        url = 'https://api.nzbplanet.net/getnzb/abc.nzb'
        with mock.patch.object(cd.time, 'monotonic', return_value=1000.0):
            cd.record_indexer_limit(url)
        with mock.patch.object(cd.time, 'monotonic', return_value=1000.0 + cd.COOLDOWN_SECONDS - 1):
            self.assertIsNotNone(cd.cooldown_reason(url))
        with mock.patch.object(cd.time, 'monotonic', return_value=1000.0 + cd.COOLDOWN_SECONDS + 1):
            self.assertIsNone(cd.cooldown_reason(url))


try:
    import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
    from queues.torrent_processor import NZB_DEFERRED_INDEXER_COOLDOWN, TorrentProcessor
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover - env without deps
    TorrentProcessor = None
    _IMPORT_ERR = exc


class _Resp:
    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    last_missing_segments = False

    def __init__(self):
        self.add_nzb = mock.Mock(return_value=None)
        self.add_nzb_content = mock.Mock(return_value='job-1')
        self.get_job_status = mock.Mock(return_value={'state': 'downloading'})

    def is_enabled(self):
        return True


@unittest.skipIf(TorrentProcessor is None, f'torrent_processor not importable: {_IMPORT_ERR}')
class TestProcessNzbResult(unittest.TestCase):
    URL = 'https://api.nzbplanet.net/getnzb/f8f2.nzb&i=1&r=key'

    def setUp(self):
        cd.reset_fetch_cooldowns()
        self.client = _FakeClient()
        self.proc = TorrentProcessor.__new__(TorrentProcessor)
        self.item = {'id': 5132, 'title': 'South Park', 'type': 'episode',
                     'imdb_id': None, 'season_number': 17, 'episode_number': 2,
                     'version': '4k and under'}
        self.result = {'title': 'South.Park.S17E02.1080p.BluRay.Remux-NTb', 'nzb_url': self.URL,
                       'parsed_info': {'seasons': [17], 'episodes': [2]}}
        patches = [
            mock.patch('usenet.get_usenet_client', return_value=self.client),
            mock.patch('usenet.reset_usenet_client'),
            mock.patch('queues.torrent_processor.time.sleep'),
            mock.patch('database.not_wanted_magnets.is_nzb_guid_not_wanted', return_value=False),
            mock.patch('database.not_wanted_magnets.is_nzb_segment_not_wanted', return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, indexer_response):
        api = mock.Mock()
        # The cli_mount listing dedup uses the same api object; make it a non-200 no-op.
        api.get.side_effect = lambda url, **kw: (
            indexer_response if url == self.URL else _Resp(404, ''))
        with mock.patch('routes.api_tracker.api', api):
            out = self.proc._process_nzb_result(dict(self.result), dict(self.item))
        indexer_hits = [c for c in api.get.call_args_list if c.args and c.args[0] == self.URL]
        return out, len(indexer_hits)

    def test_limit_response_skips_submission_and_retries(self):
        out, hits = self._run(_Resp(503, 'Service Unavailable'))
        self.assertIs(out, NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(hits, 1, 'the limited indexer must be fetched once, not three times')
        self.client.add_nzb.assert_not_called()
        self.client.add_nzb_content.assert_not_called()

        # The next attempt (e.g. the rescrape minutes later) doesn't touch the indexer at all.
        out, hits = self._run(_Resp(200, NZB_XML))
        self.assertIs(out, NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(hits, 0)
        self.client.add_nzb_content.assert_not_called()

    def test_newznab_limit_error_with_http_200(self):
        out, hits = self._run(_Resp(200, LIMIT_XML))
        self.assertIs(out, NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(hits, 1)
        self.client.add_nzb.assert_not_called()

    def test_other_errors_keep_existing_fallback(self):
        out, _ = self._run(_Resp(404, 'Not Found'))
        self.assertIsNone(out)
        # Old behaviour: cli_mount still gets the URL submission attempt.
        self.client.add_nzb.assert_called_once()
        self.assertIsNone(cd.cooldown_reason(self.URL))

    def test_healthy_download_submits(self):
        out, hits = self._run(_Resp(200, NZB_XML))
        self.assertIsNotNone(out)
        self.assertEqual(hits, 1)
        self.client.add_nzb_content.assert_called_once()


@unittest.skipIf(TorrentProcessor is None, f'torrent_processor not importable: {_IMPORT_ERR}')
class TestProcessResultsKeepsCooledResults(unittest.TestCase):
    """A cooled-down indexer's results are kept; only the attempted result is dropped."""

    def setUp(self):
        self.proc = TorrentProcessor.__new__(TorrentProcessor)
        self.proc._check_sibling_debrid_pack = lambda *a, **k: None
        self.results = [
            {'title': 'A', 'nzb_url': 'https://cooled/a', 'protocol': 'nzb'},
            {'title': 'B', 'nzb_url': 'https://other/b', 'protocol': 'nzb'},
            {'title': 'C', 'nzb_url': 'https://cooled/c', 'protocol': 'nzb'},
        ]
        self.item = {'id': 1, 'title': 'X', 'scrape_results': list(self.results)}
        p = mock.patch('database.database_writing.update_media_item')
        self.update = p.start()
        self.addCleanup(p.stop)

    def _run(self, outcome):
        self.proc._process_nzb_result = lambda r, item, adding_queue_items=None: outcome(r)
        return self.proc._process_results_inner(list(self.item['scrape_results']), False, self.item)

    def test_cooled_results_kept_and_attempted_one_dropped(self):
        out = self._run(lambda r: NZB_DEFERRED_INDEXER_COOLDOWN if 'cooled' in r['nzb_url'] else None)
        self.assertEqual(out, (None, None, None))
        self.assertEqual([r['title'] for r in self.item['scrape_results']], ['A', 'C'])

    def test_stored_json_without_queue_added_fields_still_matches(self):
        import json
        self.item['scrape_results'] = json.dumps(self.results)
        results = [dict(r, original_scraped_torrent_title=r['title']) for r in self.results]
        self.proc._process_nzb_result = lambda r, item, adding_queue_items=None: (
            NZB_DEFERRED_INDEXER_COOLDOWN if 'cooled' in r['nzb_url'] else None)
        self.proc._process_results_inner(results, False, self.item)
        self.assertEqual([r['title'] for r in self.item['scrape_results']], ['A', 'C'])
        # Hybrid mode's second pass over the same list must not drop another result.
        self.proc._process_results_inner(results, True, self.item)
        self.assertEqual([r['title'] for r in self.item['scrape_results']], ['A', 'C'])

    def test_all_cooled_keeps_everything(self):
        out = self._run(lambda r: NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(out, (None, None, None))
        self.assertEqual(len(self.item['scrape_results']), 3)
        self.update.assert_not_called()


if __name__ == '__main__':
    unittest.main()
