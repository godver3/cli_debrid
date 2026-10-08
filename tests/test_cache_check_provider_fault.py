#!/usr/bin/env python3
"""Regression test: a provider-side failure during a debrid cache check must not
blacklist the hash install-wide.

Reported symptom: Real-Debrid answers some addMagnet calls with HTTP 451
(Unavailable For Legal Reasons). RD's cache check is add -> inspect -> remove, so
the 451 surfaced inside is_cached(), whose catch-all handler called
add_to_not_wanted(hash). The release was then dropped from every future scrape,
even though the AllDebrid fallback that was being checked in parallel could have
served it. Six releases were lost this way in one 11-hour run.

A 451, 429, 5xx, timeout or auth failure describes the provider, not the torrent,
so is_cached() now returns None (skip for now) without blacklisting. Errors that
describe the torrent (no video files, invalid magnet) still blacklist.
"""

import asyncio
import unittest
from unittest import mock

import requests

import database  # noqa: F401 - app import order; debrid alone hits a routes<->debrid cycle

from debrid.base import (
    ProviderUnavailableError, RateLimitError, TooManyDownloadsError,
    TorrentAdditionError, is_provider_fault,
)

HASH = 'a' * 40
MAGNET = f'magnet:?xt=urn:btih:{HASH}'


class IsProviderFaultTest(unittest.TestCase):
    def test_provider_faults(self):
        for exc in (
            ProviderUnavailableError("Request failed: 451 Client Error: Unavailable For Legal Reasons"),
            ProviderUnavailableError("Request failed: 502 Server Error: Bad Gateway"),
            ProviderUnavailableError("Request timed out"),
            RateLimitError("Rate limit exceeded"),
            TooManyDownloadsError("too many"),
            requests.exceptions.ConnectionError("boom"),
            Exception("AllDebrid service temporarily unavailable (HTTP 503)"),
            Exception("AllDebrid API error MAGNET_TOO_MANY_ACTIVE: too many active magnets"),
            type('RealDebridAuthError', (Exception,), {})("Invalid API key"),
        ):
            self.assertTrue(is_provider_fault(exc), repr(exc))

    def test_torrent_faults(self):
        for exc in (
            TorrentAdditionError("No video files found in torrent"),
            TorrentAdditionError("Timed out waiting for torrent files"),
            ProviderUnavailableError("Request failed: 400 Client Error: Bad Request"),
            Exception("Invalid magnet link or torrent unavailable"),
            Exception("AllDebrid API error MAGNET_INVALID_URI: bad uri"),
        ):
            self.assertFalse(is_provider_fault(exc), repr(exc))


class RealDebridIsCachedTest(unittest.TestCase):
    def _provider(self):
        from debrid.real_debrid.client import RealDebridProvider
        p = RealDebridProvider.__new__(RealDebridProvider)
        p._cached_torrent_ids, p._cached_torrent_titles, p._all_torrent_ids = {}, {}, {}
        p.phalanx_enabled, p.phalanx_cache = False, None
        return p

    def _run(self, exc):
        p = self._provider()
        with mock.patch.object(type(p), 'add_torrent', side_effect=exc), \
             mock.patch('debrid.real_debrid.client.add_to_not_wanted') as not_wanted:
            result = asyncio.run(p.is_cached(MAGNET))
        return result, not_wanted

    def test_451_does_not_blacklist(self):
        result, not_wanted = self._run(ProviderUnavailableError(
            "Request failed: 451 Client Error: Unavailable For Legal Reasons"))
        self.assertIsNone(result)
        not_wanted.assert_not_called()

    def test_bad_torrent_still_blacklists(self):
        result, not_wanted = self._run(TorrentAdditionError("No video files found in torrent"))
        self.assertIsNone(result)
        not_wanted.assert_called_once_with(HASH)

    def test_failed_info_fetch_does_not_blacklist(self):
        p = self._provider()
        with mock.patch.object(type(p), 'add_torrent', return_value='TID'), \
             mock.patch.object(type(p), 'get_torrent_info', return_value=None), \
             mock.patch.object(type(p), 'remove_torrent'), \
             mock.patch('debrid.real_debrid.client.add_to_not_wanted') as not_wanted:
            result = asyncio.run(p.is_cached(MAGNET))
        self.assertIsNone(result)
        not_wanted.assert_not_called()


if __name__ == '__main__':
    unittest.main()
