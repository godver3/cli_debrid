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

    def test_raised_http_error_still_starts_cooldown(self):
        # The real api.get calls raise_for_status(), so a 429 arrives as an
        # exception carrying the response, not as a returned 429 response.
        import requests
        resp = _Resp(429, 'Too Many Requests')
        err = requests.exceptions.HTTPError('429 Client Error', response=resp)
        api = mock.Mock()
        api.get.side_effect = lambda url, **kw: (_ for _ in ()).throw(err) if url == self.URL else _Resp(404, '')
        with mock.patch('routes.api_tracker.api', api):
            out = self.proc._process_nzb_result(dict(self.result), dict(self.item))
        hits = [c for c in api.get.call_args_list if c.args and c.args[0] == self.URL]
        self.assertIs(out, NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(len(hits), 1, 'no URL submit / direct-upload retry after a raised 429')
        self.client.add_nzb.assert_not_called()
        self.assertIsNotNone(cd.cooldown_reason(self.URL))

    def test_daily_grab_limit_defers_without_touching_the_indexer(self):
        with mock.patch('usenet.indexer_quota.allow_url', return_value=False):
            out, hits = self._run(_Resp(200, NZB_XML))
        self.assertIs(out, NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(hits, 0)
        self.client.add_nzb.assert_not_called()
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


@unittest.skipIf(TorrentProcessor is None, f'torrent_processor not importable: {_IMPORT_ERR}')
class TestCooldownParksItemInAdding(unittest.TestCase):
    """A cooling-down NZB keeps the item's results but must not freeze the show."""

    URL = 'https://ninjacentral.co.za/getnzb/abc.nzb'

    def setUp(self):
        import queues.adding_queue as aq
        self.aq = aq
        aq._cooldown_wake.clear()
        aq._cooldown_notes.clear()
        cd.reset_fetch_cooldowns()
        self.addCleanup(aq._cooldown_wake.clear)
        self.addCleanup(aq._cooldown_notes.clear)
        self.addCleanup(cd.reset_fetch_cooldowns)
        self.nzb = {'title': 'Survivor.S21E04.1080p', 'nzb_url': self.URL, 'protocol': 'nzb'}
        self.item = {'id': 7, 'title': 'Survivor', 'imdb_id': 'tt0239195', 'type': 'episode',
                     'state': 'Adding', 'filled_by_torrent_id': None,
                     'scrape_results': [dict(self.nzb)]}
        self.proc = TorrentProcessor.__new__(TorrentProcessor)
        self.proc._check_sibling_debrid_pack = lambda *a, **k: None
        p = mock.patch('database.database_writing.update_media_item')
        self.update = p.start()
        self.addCleanup(p.stop)

    def _process(self, nzb_outcome):
        self.proc._process_nzb_result = lambda r, item, adding_queue_items=None: nzb_outcome
        return self.proc._process_results_inner([dict(self.nzb)], False, self.item)

    def test_cooling_indexer_records_wait_until_cooldown_ends(self):
        cd.record_indexer_limit(self.URL, 1800)
        out = self._process(NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(out, (None, None, None))
        wait = self.item[self.aq.NZB_COOLDOWN_WAIT_KEY]
        self.assertTrue(1700 < wait <= 1800, wait)
        # Nothing dropped, blacklisted or marked not-wanted.
        self.assertEqual(len(self.item['scrape_results']), 1)
        self.update.assert_not_called()

    def test_parked_item_does_not_block_sibling_scraping(self):
        self.assertTrue(self.aq.adding_item_blocks_scraping(self.item))
        self.aq.park_for_indexer_cooldown(self.item['id'], 1800)
        self.assertTrue(self.aq.item_parked_for_cooldown(7))
        self.assertFalse(self.aq.adding_item_blocks_scraping(self.item))
        # Another show's Adding item is unaffected.
        self.assertTrue(self.aq.adding_item_blocks_scraping({'id': 8, 'filled_by_torrent_id': ''}))

    def test_item_retried_once_cooldown_ends(self):
        with mock.patch.object(self.aq.time, 'monotonic', return_value=1000.0):
            self.aq.park_for_indexer_cooldown(7, 1800)
            self.assertTrue(self.aq.item_parked_for_cooldown(7))
        with mock.patch.object(self.aq.time, 'monotonic', return_value=1000.0 + 1801):
            self.assertFalse(self.aq.item_parked_for_cooldown(7))
            self.assertTrue(self.aq.adding_item_blocks_scraping(self.item))
        # The NZB result is still there to be submitted.
        self.assertEqual(self.item['scrape_results'][0]['nzb_url'], self.URL)

    def test_quota_hold_without_end_time_rechecks_soon(self):
        self._process(NZB_DEFERRED_INDEXER_COOLDOWN)
        from queues.torrent_processor import NZB_QUOTA_RECHECK_SECONDS
        self.assertEqual(self.item[self.aq.NZB_COOLDOWN_WAIT_KEY], NZB_QUOTA_RECHECK_SECONDS)

    def test_normal_nzb_failure_pops_result_and_does_not_park(self):
        out = self._process(None)
        self.assertEqual(out, (None, None, None))
        self.assertNotIn(self.aq.NZB_COOLDOWN_WAIT_KEY, self.item)
        self.assertEqual(self.item['scrape_results'], [])

    def test_rejected_result_after_a_cooled_one_clears_the_wait(self):
        cd.record_indexer_limit(self.URL, 1800)
        other = {'title': 'B', 'nzb_url': 'https://other/b', 'protocol': 'nzb'}
        self.item['scrape_results'] = [dict(self.nzb), other]
        self.proc._process_nzb_result = lambda r, item, adding_queue_items=None: (
            NZB_DEFERRED_INDEXER_COOLDOWN if 'ninja' in r['nzb_url'] else None)
        self.proc._process_results_inner([dict(self.nzb), dict(other)], False, self.item)
        self.assertNotIn(self.aq.NZB_COOLDOWN_WAIT_KEY, self.item)
        self.assertEqual([r['title'] for r in self.item['scrape_results']], [self.nzb['title']])

    def test_note_names_the_indexer_and_why(self):
        cd.record_indexer_limit(self.URL, 1800, cd.refusal_why(429, ''))
        with mock.patch('usenet.indexer_quota.instance_for_url', return_value='NinjaCentral_1'):
            self._process(NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(self.item[self.aq.NZB_COOLDOWN_NOTES_KEY], {'NinjaCentral': 'out of downloads'})

    def test_note_falls_back_to_host_and_quota_reason(self):
        with mock.patch('usenet.indexer_quota.instance_for_url', return_value=None):
            self._process(NZB_DEFERRED_INDEXER_COOLDOWN)
        self.assertEqual(self.item[self.aq.NZB_COOLDOWN_NOTES_KEY],
                         {'ninjacentral.co.za': 'daily grab limit used up'})

    def test_queue_page_note_while_parked(self):
        with mock.patch.object(self.aq.time, 'monotonic', return_value=1000.0):
            self.aq.park_for_indexer_cooldown(7, 1800, {'NinjaCentral': 'out of downloads'})
            self.assertEqual(self.aq.cooldown_note(7), 'NinjaCentral out of downloads, retrying in 30 min')
            self.assertIsNone(self.aq.cooldown_note(8))
        with mock.patch.object(self.aq.time, 'monotonic', return_value=1000.0 + 1801):
            self.assertIsNone(self.aq.cooldown_note(7))
        self.aq.unpark_item(7)
        self.assertNotIn(7, self.aq._cooldown_notes)

    def test_refusal_why(self):
        self.assertEqual(cd.refusal_why(429, ''), 'out of downloads')
        self.assertEqual(cd.refusal_why(200, '<error code="501" description="Download limit reached"/>'),
                         'out of downloads')
        self.assertEqual(cd.refusal_why(503, '<html>maintenance</html>'), 'unavailable (HTTP 503)')


@unittest.skipIf(TorrentProcessor is None, f'torrent_processor not importable: {_IMPORT_ERR}')
class TestHybridModeWithCooledNzb(unittest.TestCase):
    """A cooled-down NZB deferral must not block Hybrid mode's uncached torrent pass."""

    URL = 'https://ninjacentral.co.za/getnzb/abc.nzb'
    MAGNET = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567'

    def setUp(self):
        import queues.adding_queue as aq
        self.aq = aq
        aq._cooldown_wake.clear()
        aq._cooldown_notes.clear()
        cd.reset_fetch_cooldowns()
        self.addCleanup(aq._cooldown_wake.clear)
        self.addCleanup(aq._cooldown_notes.clear)
        self.addCleanup(cd.reset_fetch_cooldowns)

    def _make_queue(self, item):
        q = self.aq.AddingQueue.__new__(self.aq.AddingQueue)
        q.items = [item]
        q._adding_ticks = {}
        q._nzb_submitted_ids = set()
        q._nzb_downloading_job_ids = set()
        q._held_summary = None
        q._held_summary_at = 0.0
        q.torrent_processor = mock.Mock()
        q.media_matcher = mock.Mock()
        q.debrid_provider = mock.Mock()
        q.remove_item = mock.Mock()
        q._handle_failed_item = mock.Mock()
        return q

    def _run_process(self, item, side_effect):
        q = self._make_queue(item)
        calls = []

        def fake_process_results(results, accept_uncached=False, item=None, adding_queue_items=None):
            calls.append(accept_uncached)
            return side_effect(results, accept_uncached, item)

        q.torrent_processor.process_results.side_effect = fake_process_results
        qm = mock.Mock()
        qm.queues = {}
        qm.upgrade_process_locks = set()

        def gs(section, key, default=None):
            if section == 'Scraping' and key == 'hybrid_mode':
                return True
            if section == 'Queue' and key == 'item_process_delay_seconds':
                return 0.0
            return default

        # Return Pending Uncached after processing when the uncached torrent was added,
        # so process() cleanly exits after verifying the torrent was accepted.
        added_holder = {'added': False}

        def get_item_by_id(_id):
            if added_holder['added']:
                return {'id': _id, 'state': 'Pending Uncached'}
            return {'id': _id, 'state': 'Adding'}

        def wrapped_side_effect(results, accept_uncached, it):
            res = side_effect(results, accept_uncached, it)
            if res[0] is not None and res[1] is not None:
                added_holder['added'] = True
            return res

        q.torrent_processor.process_results.side_effect = lambda results, accept_uncached=False, item=None, adding_queue_items=None: (
            calls.append(accept_uncached) or wrapped_side_effect(results, accept_uncached, item)
        )

        with mock.patch.object(self.aq, 'get_setting', side_effect=gs), \
             mock.patch.object(self.aq, 'accepts_uncached_now', return_value=False), \
             mock.patch('database.get_media_item_by_id', side_effect=get_item_by_id), \
             mock.patch('debrid.get_debrid_providers', return_value=[]), \
             mock.patch('utilities.acquisition_health.usable_results', side_effect=lambda rs, *a, **k: rs):
            q.process(qm)
        return q, calls, added_holder['added']

    def test_cooled_nzb_and_uncached_torrent_adds_torrent_in_hybrid_mode(self):
        """[cooled NZB, uncached torrent] in Hybrid mode adds the torrent instead of parking."""
        cd.record_indexer_limit(self.URL, 1800)
        nzb = {'title': 'Show.S01E01.NZB', 'nzb_url': self.URL, 'protocol': 'nzb'}
        torrent = {'title': 'Show.S01E01.TOR', 'magnet': self.MAGNET}
        item = {'id': 42, 'title': 'Show', 'type': 'movie', 'state': 'Adding',
                'filled_by_torrent_id': None, 'scrape_results': [dict(nzb), dict(torrent)]}

        # Use the real _process_results_inner for NZB handling; simulate an uncached torrent
        # that is skipped when accept_uncached=False and accepted when accept_uncached=True.
        proc = TorrentProcessor.__new__(TorrentProcessor)
        proc._check_sibling_debrid_pack = lambda *a, **k: None
        proc._process_nzb_result = lambda r, it, adding_queue_items=None: NZB_DEFERRED_INDEXER_COOLDOWN

        def fake_process_torrent(link):
            return link, None

        proc.process_torrent = fake_process_torrent
        proc._resolved_hash_not_wanted = lambda *a, **k: False

        def process_results_real_nzb(results, accept_uncached, it):
            for r in results:
                if r.get('protocol') == 'nzb' or r.get('nzb_url'):
                    # Exercise the real cooldown-deferral branch in _process_results_inner.
                    proc._process_results_inner([r], accept_uncached, it)
                elif accept_uncached:
                    return ({'id': 'tor-1', 'filename': 'Show.mkv', 'files': []}, self.MAGNET, r)
            return None, None, None

        q, calls, added = self._run_process(item, process_results_real_nzb)
        self.assertEqual(calls, [False, True], 'Hybrid second pass must run after cooldown-only deferral')
        self.assertTrue(added, 'uncached torrent must be added on the hybrid second pass')
        self.assertFalse(self.aq.item_parked_for_cooldown(42), 'item must not be parked when torrent was added')
        q._handle_failed_item.assert_not_called()

    def test_real_nzb_attempt_still_skips_hybrid_second_pass(self):
        """A real NZB submission attempt (not a cooldown deferral) still caps at one attempt per tick."""
        nzb = {'title': 'Show.S01E01.NZB', 'nzb_url': self.URL, 'protocol': 'nzb'}
        torrent = {'title': 'Show.S01E01.TOR', 'magnet': self.MAGNET}
        item = {'id': 43, 'title': 'Show', 'type': 'movie', 'state': 'Adding',
                'filled_by_torrent_id': None, 'scrape_results': [dict(nzb), dict(torrent)]}

        proc = TorrentProcessor.__new__(TorrentProcessor)
        proc._check_sibling_debrid_pack = lambda *a, **k: None
        proc._process_nzb_result = lambda r, it, adding_queue_items=None: None  # real attempt failed

        with mock.patch('database.database_writing.update_media_item'):
            _, calls, added = self._run_process(
                item, lambda rs, au, it: proc._process_results_inner(rs, au, it)
            )
        self.assertEqual(calls, [False], 'Hybrid second pass must be skipped after a real NZB attempt')
        self.assertFalse(added)

    def test_only_cooled_nzbs_still_parks_item(self):
        """When only cooled NZBs exist (no usable torrent), the item is still parked."""
        cd.record_indexer_limit(self.URL, 1800)
        nzb = {'title': 'Show.S01E01.NZB', 'nzb_url': self.URL, 'protocol': 'nzb'}
        item = {'id': 44, 'title': 'Show', 'type': 'movie', 'state': 'Adding',
                'filled_by_torrent_id': None, 'scrape_results': [dict(nzb)]}

        proc = TorrentProcessor.__new__(TorrentProcessor)
        proc._check_sibling_debrid_pack = lambda *a, **k: None
        proc._process_nzb_result = lambda r, it, adding_queue_items=None: NZB_DEFERRED_INDEXER_COOLDOWN

        q, calls, added = self._run_process(
            item, lambda rs, au, it: proc._process_results_inner(rs, au, it)
        )
        self.assertEqual(calls, [False])
        self.assertFalse(added)
        self.assertTrue(self.aq.item_parked_for_cooldown(44))
        q._handle_failed_item.assert_not_called()


if __name__ == '__main__':
    unittest.main()
