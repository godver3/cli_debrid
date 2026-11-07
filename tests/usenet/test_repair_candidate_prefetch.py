#!/usr/bin/env python3
"""
Replacement NZBs must only be downloaded for the candidate about to be
submitted, never for every scrape result.

_scrape_for_replacement used to fetch every result's NZB before the playback
exclusion check ran. A stuck playback repair (awaiting_candidate, retried every
30 min for up to 7 days) therefore re-downloaded the same already-failed NZB
from every indexer each retry, and indexers flagged the grab volume.
"""

import unittest
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


from tests._isolation import isolated_modules


def _permissive_module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr: (lambda *a, **k: None)  # any imported name → dummy callable
    return m


@isolated_modules
def _load():
    if 'requests' not in sys.modules:
        sys.modules['requests'] = _permissive_module('requests')
    for n in ['database', 'database.core', 'database.nzb_repair_activity',
              'database.nzb_playback_repair',
              'database.not_wanted_magnets']:
        sys.modules[n] = _permissive_module(n)
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: (a[2] if len(a) > 2 else None)
    sys.modules['utilities.settings'] = uss
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'usenet', 'repair_engine.py')
    spec = importlib.util.spec_from_file_location('repair_engine_prefetch_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


re_mod = _load()


class _Resp:
    status_code = 200

    def __init__(self, url):
        self.text = f'<nzb><segment>{url}</segment></nzb>'


def _candidate(title):
    return {'title': title, 'protocol': 'nzb', 'nzb_url': f'https://indexer/get/{title}'}


def _keys(result):
    keys = {f't:{result.get("original_title") or result.get("title")}'}
    if result.get('_prefetched_nzb'):
        keys.add(f's:{result["_prefetched_nzb"]}')
    return keys


class _Base(unittest.TestCase):
    def setUp(self):
        self.fetched = []
        self.failed = []
        self.submitted = []
        self.excluded = set()
        self.blacklisted_segments = set()

        def fake_get(url, **kwargs):
            self.fetched.append(url)
            return _Resp(url)

        self._patch(re_mod.requests, 'get', fake_get)
        self._patch(re_mod, 'is_nzb_segment_not_wanted', lambda xml: xml in self.blacklisted_segments)
        self._patch(re_mod, 'candidate_keys', _keys)
        self._patch(re_mod, 'candidate_is_excluded', lambda item_id, r: bool(_keys(r) & self.excluded))
        self._patch(re_mod, 'record_failed_candidate',
                    lambda item_id, r, job_id='': self.failed.append(r['title']))
        self._patch(re_mod, 'playback_candidate_search_blocked', lambda *a: (False, ''))
        self._patch(re_mod, 'set_playback_candidate', lambda *a: True)
        self._patch(re_mod, '_update_db_for_repair', lambda *a, **k: True)
        # The real junk check imports debrid, which cycles through routes in tests.
        self._patch(re_mod, '_is_junk_replacement_result', lambda r: False)

        def fake_submit(result, title, item=None):
            self.submitted.append(result['title'])
            return f'job-{result["title"]}', result['title'], 'accepted'

        self._patch(re_mod, '_submit_and_confirm_replacement', fake_submit)

    def _patch(self, obj, name, value):
        old = getattr(obj, name, None)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, old)


class TestScrapeForReplacementDoesNotDownload(_Base):
    def test_scrape_returns_candidates_without_fetching_any_nzb(self):
        scraper_mod = types.ModuleType('scraper.scraper')
        scraper_mod.scrape = lambda **kwargs: ([_candidate('A'), _candidate('B')], [])
        old = sys.modules.get('scraper.scraper')
        sys.modules['scraper.scraper'] = scraper_mod
        self.addCleanup(lambda: sys.modules.__setitem__('scraper.scraper', old)
                        if old else sys.modules.pop('scraper.scraper', None))

        results = re_mod._scrape_for_replacement({'title': 'Movie', 'type': 'movie'}, 'Broken')

        self.assertEqual([r['title'] for r in results], ['A', 'B'])
        self.assertEqual(self.fetched, [])


class TestPlaybackCandidateRetry(_Base):
    def _run(self, *candidates):
        self._patch(re_mod, '_scrape_for_replacement', lambda item, title: list(candidates))
        return re_mod.find_and_submit_playback_candidate(
            {'id': 1, 'old_info_hash': 'old'}, {'id': 7, 'title': 'Movie'})

    def test_title_excluded_candidate_is_never_downloaded(self):
        self.excluded = {'t:Failed'}
        outcome = self._run(_candidate('Failed'))
        self.assertEqual(outcome, 'no_replacement')
        self.assertEqual(self.fetched, [])

    def test_only_the_submitted_candidate_is_downloaded(self):
        self.excluded = {'t:Failed'}
        outcome = self._run(_candidate('Failed'), _candidate('Good'), _candidate('Spare'))
        self.assertEqual(outcome, 'submitted')
        self.assertEqual(self.fetched, ['https://indexer/get/Good'])
        self.assertEqual(self.submitted, ['Good'])

    def test_segment_excluded_candidate_is_recorded_so_next_retry_skips_download(self):
        self.excluded = {'s:<nzb><segment>https://indexer/get/Repost</segment></nzb>'}
        outcome = self._run(_candidate('Repost'), _candidate('Good'))
        self.assertEqual(outcome, 'submitted')
        self.assertEqual(self.failed, ['Repost'])
        self.assertEqual(self.submitted, ['Good'])

    def test_blacklisted_segment_is_recorded_and_skipped(self):
        self.blacklisted_segments = {'<nzb><segment>https://indexer/get/Dead</segment></nzb>'}
        outcome = self._run(_candidate('Dead'), _candidate('Good'))
        self.assertEqual(outcome, 'submitted')
        self.assertEqual(self.failed, ['Dead'])
        self.assertEqual(self.submitted, ['Good'])


class TestPrefetchCandidateNzb(_Base):
    def test_already_prefetched_candidate_is_not_downloaded_again(self):
        candidate = dict(_candidate('A'), _prefetched_nzb='<nzb />')
        self.assertTrue(re_mod._prefetch_candidate_nzb(candidate))
        self.assertEqual(self.fetched, [])

    def test_failed_download_still_allows_submission_by_url(self):
        def boom(url, **kwargs):
            raise OSError('indexer down')
        self._patch(re_mod.requests, 'get', boom)
        candidate = _candidate('A')
        self.assertTrue(re_mod._prefetch_candidate_nzb(candidate))
        self.assertNotIn('_prefetched_nzb', candidate)


if __name__ == '__main__':
    unittest.main(verbosity=2)
