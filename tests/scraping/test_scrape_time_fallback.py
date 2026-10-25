#!/usr/bin/env python3
"""Per-version fallback_at_scrape_time.

fallback_version used to be applied only by blacklist_item(), after the whole
sleep/Final_Check cycle: with wake_limit 8, 60 min sleeps and a 12h final-check delay,
a strict "1080p only" version that can never match old content took ~20h and nine
identical scrapes before the permissive fallback was tried.

With fallback_at_scrape_time on, an empty scrape probes the fallback version once. If
the probe finds results the Adding queue could take (with cached-only settings: at least
one cached torrent, or any NZB) the item goes through the existing blacklist-time
fallback (move_to_blacklisted) now; if not, it sleeps exactly as before.
"""

import unittest
from unittest import mock

import database  # noqa: F401 - app import order

from queues.scraping_queue import ScrapingQueue

ITEM = {'id': 7, 'type': 'movie', 'title': 'Aladdin', 'year': 1992, 'imdb_id': 'tt0103639',
        'tmdb_id': 812, 'version': '1080p Ultimate', 'state': 'Scraping'}


def _versions(**strict):
    return {
        '1080p Ultimate': dict({'fallback_version': '1080p', 'fallback_at_scrape_time': True}, **strict),
        '1080p': {'fallback_version': 'None'},
    }


class ScrapeTimeFallbackTest(unittest.TestCase):
    def _run(self, versions, probe_results=(), wake_count=0, fallback_exists=False, item=ITEM, addable=True):
        q = ScrapingQueue()
        q.add_item(dict(item))
        qm = mock.Mock()
        qm.generate_identifier.return_value = 'Aladdin (1992)'

        def fake_get_setting(section, key, default=None):
            if (section, key) == ('Scraping', 'versions'):
                return versions
            if (section, key) == ('Queue', 'wake_limit'):
                return 8
            return default

        with mock.patch('queues.scraping_queue.get_setting', side_effect=fake_get_setting), \
             mock.patch('database.get_wake_count', return_value=wake_count), \
             mock.patch('database.database_reading.check_existing_media_item', return_value=fallback_exists), \
             mock.patch.object(ScrapingQueue, 'is_item_old', return_value=False), \
             mock.patch.object(ScrapingQueue, 'reset_not_wanted_check'), \
             mock.patch.object(ScrapingQueue, '_probe_has_addable_result', return_value=addable), \
             mock.patch.object(ScrapingQueue, 'scrape_with_fallback',
                               return_value=(list(probe_results), [])) as probe:
            q.handle_no_results(dict(item), qm)
        return qm, probe

    def test_probe_hit_switches_to_fallback_now(self):
        qm, probe = self._run(_versions(), probe_results=[{'title': 'Aladdin.1992.720p'}])
        self.assertEqual(probe.call_args.args[0]['version'], '1080p')
        self.assertEqual(probe.call_args.args[0]['id'], 7)
        qm.move_to_blacklisted.assert_called_once()
        qm.move_to_sleeping.assert_not_called()

    def test_probe_miss_keeps_retry_cycle(self):
        qm, probe = self._run(_versions(), probe_results=[])
        probe.assert_called_once()
        qm.move_to_blacklisted.assert_not_called()
        qm.move_to_sleeping.assert_called_once()

    def test_probe_hit_with_nothing_addable_keeps_retry_cycle(self):
        qm, probe = self._run(_versions(), probe_results=[{'title': 'x'}], addable=False)
        probe.assert_called_once()
        qm.move_to_blacklisted.assert_not_called()
        qm.move_to_sleeping.assert_called_once()

    def test_flag_off_is_unchanged(self):
        qm, probe = self._run(_versions(fallback_at_scrape_time=False))
        probe.assert_not_called()
        qm.move_to_sleeping.assert_called_once()

    def test_waits_for_fallback_after_attempts(self):
        # wake_count 0 -> this is failed scrape 1 of the 2 required
        qm, probe = self._run(_versions(fallback_after_attempts=2), probe_results=[{'title': 'x'}])
        probe.assert_not_called()
        qm.move_to_sleeping.assert_called_once()
        # wake_count 1 -> failed scrape 2: probe now
        qm, probe = self._run(_versions(fallback_after_attempts=2), probe_results=[{'title': 'x'}], wake_count=1)
        probe.assert_called_once()
        qm.move_to_blacklisted.assert_called_once()

    def test_existing_fallback_item_skips_probe(self):
        qm, probe = self._run(_versions(), probe_results=[{'title': 'x'}], fallback_exists=True)
        probe.assert_not_called()
        qm.move_to_sleeping.assert_called_once()

    def test_no_or_invalid_fallback_version(self):
        for fb in ('None', 'Missing Version', '1080p Ultimate'):
            qm, probe = self._run(_versions(fallback_version=fb), probe_results=[{'title': 'x'}])
            probe.assert_not_called()
            qm.move_to_sleeping.assert_called_once()

    def test_early_release_skips_probe(self):
        qm, probe = self._run(_versions(), probe_results=[{'title': 'x'}],
                              item=dict(ITEM, early_release=True))
        probe.assert_not_called()
        qm.move_to_sleeping.assert_called_once()


class ProbeAddableResultTest(unittest.TestCase):
    """_probe_has_addable_result: cached-only settings need a cached torrent."""
    MAGNET = 'magnet:?xt=urn:btih:' + 'a' * 40

    def _check(self, results, cached=(), accepts_uncached=False, hybrid=False):
        def fake_get_setting(section, key, default=None):
            return {('Scraping', 'hybrid_mode'): hybrid}.get((section, key), default)

        provider = mock.Mock(PROVIDER_NAME='RD')
        processor = mock.Mock(_providers=[provider])
        processor.process_torrent.side_effect = lambda link: (link, None)
        statuses = iter(cached)
        processor.check_cache_status.side_effect = lambda *a, **k: (next(statuses, False), 'direct_check')
        with mock.patch('queues.scraping_queue.get_setting', side_effect=fake_get_setting), \
             mock.patch('queues.adding_queue.accepts_uncached_now', return_value=accepts_uncached), \
             mock.patch('debrid.get_debrid_provider', return_value=provider), \
             mock.patch('queues.torrent_processor.TorrentProcessor', return_value=processor):
            ok = ScrapingQueue()._probe_has_addable_result(results, dict(ITEM), 'Aladdin (1992)')
        return ok, processor

    def test_uncached_allowed_skips_cache_check(self):
        # Full / inside accept_uncached_within_hours, or Hybrid's uncached second pass
        for accepts_uncached, hybrid in ((True, False), (False, True)):
            ok, processor = self._check([{'magnet': self.MAGNET}], accepts_uncached=accepts_uncached, hybrid=hybrid)
            self.assertTrue(ok)
            processor.check_cache_status.assert_not_called()

    def test_cached_only_needs_a_cached_torrent(self):
        ok, processor = self._check([{'magnet': self.MAGNET}] * 2, cached=(False, False))
        self.assertFalse(ok)
        self.assertEqual(processor.check_cache_status.call_count, 2)
        ok, _ = self._check([{'magnet': self.MAGNET}] * 2, cached=(False, True))
        self.assertTrue(ok)

    def test_probe_removes_what_it_checked(self):
        _, processor = self._check([{'magnet': self.MAGNET}], cached=(True,))
        self.assertTrue(processor.check_cache_status.call_args.kwargs['remove_cached'])

    def test_checks_at_most_five(self):
        ok, processor = self._check([{'magnet': self.MAGNET}] * 8)
        self.assertFalse(ok)
        self.assertEqual(processor.check_cache_status.call_count, 5)

    def test_nzb_result_needs_no_cache_check(self):
        ok, processor = self._check([{'magnet': self.MAGNET}, {'nzb_url': 'http://x/nzb'}])
        self.assertTrue(ok)
        processor.check_cache_status.assert_not_called()


class AcceptsUncachedNowTest(unittest.TestCase):
    """The Adding queue's first-pass uncached rule, shared with the probe."""

    def _accepts(self, release_date, handling='None', within_hours=24):
        from queues import adding_queue

        def fake_get_setting(section, key, default=None):
            return {('Scraping', 'accept_uncached_within_hours'): within_hours,
                    ('Scraping', 'uncached_content_handling'): handling,
                    ('Queue', 'episode_airtime_offset'): '0'}.get((section, key), default)

        item = {'type': 'episode', 'release_date': release_date, 'airtime': None}
        with mock.patch('queues.adding_queue.get_setting', side_effect=fake_get_setting):
            return adding_queue.accepts_uncached_now(item, 'Show S01E01')

    def test_recent_release_inside_window(self):
        from datetime import date
        self.assertTrue(self._accepts(date.today().isoformat()))

    def test_old_release_outside_window(self):
        self.assertFalse(self._accepts('2001-01-01'))
        self.assertFalse(self._accepts('Unknown'))

    def test_full_always_accepts(self):
        self.assertTrue(self._accepts('2001-01-01', handling='Full'))


if __name__ == '__main__':
    unittest.main()
