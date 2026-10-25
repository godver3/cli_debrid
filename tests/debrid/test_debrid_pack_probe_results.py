"""Adding's candidate list for a season sent down the debrid season pack path.

Shrinking (NZB season packs disabled): Scraping put the season on the debrid
pack path because a 1080p debrid pack ranked first. Real-Debrid refused (451)
all nine 1080p packs, and Adding worked down to the tenth, a 720p Italian dub,
while 1080p single episodes were available but never scraped. Adding now only
tries packs at the top pack's resolution; once those are refused the existing
single-scraper fallback re-scrapes the season episode by episode.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import database  # noqa: F401  (app import order)
from queues.adding_queue import limit_debrid_pack_probe_results


def _pack(title, resolution, probe=True):
    r = {'title': title, 'parsed_info': {'resolution': resolution}}
    if probe:
        r['_debrid_pack_probe'] = True
    return r


SHRINKING = [_pack(f'Shrinking S01 1080p pack {n}', '1080p') for n in range(1, 10)] + [
    _pack('Shrinking S01e01-10 [720p Ita Eng Spa SubS] byMe7alh [MIRCrew]', '720p'),
    _pack('Shrinking S01 480p', '480p'),
]


def _setting(fallback_enabled=True):
    return mock.patch('queues.adding_queue.get_setting',
                      side_effect=lambda s, k, d=None: fallback_enabled if k == 'fallback_to_single_enabled' else d)


class DebridPackProbeResultsTests(unittest.TestCase):
    def test_tries_every_pack_at_the_top_resolution_not_lower(self):
        with _setting():
            kept = limit_debrid_pack_probe_results(SHRINKING, {'id': 1})
        self.assertEqual(len(kept), 9)
        self.assertTrue(all('1080p' in r['title'] for r in kept))
        self.assertEqual(kept[0]['title'], SHRINKING[0]['title'])  # rank order kept

    def test_untagged_results_unchanged(self):
        results = [_pack('a', '1080p', probe=False), _pack('b', '720p', probe=False)]
        with _setting():
            self.assertEqual(limit_debrid_pack_probe_results(results, {'id': 1}), results)

    def test_already_fell_back_unchanged(self):
        with _setting():
            self.assertEqual(limit_debrid_pack_probe_results(SHRINKING, {'fall_back_to_single_scraper': True}),
                             SHRINKING)

    def test_single_fallback_disabled_unchanged(self):
        # Without the single scraper to fall back on, every pack is still worth a try.
        with _setting(fallback_enabled=False):
            self.assertEqual(limit_debrid_pack_probe_results(SHRINKING, {'id': 1}), SHRINKING)

    def test_unknown_top_resolution_tries_top_only(self):
        results = [_pack('a', 'Unknown'), _pack('b', 'Unknown'), _pack('c', '720p')]
        with _setting():
            self.assertEqual(limit_debrid_pack_probe_results(results, {'id': 1}), results[:1])

    def test_empty(self):
        with _setting():
            self.assertEqual(limit_debrid_pack_probe_results([], {'id': 1}), [])


if __name__ == '__main__':
    unittest.main()
