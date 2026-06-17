#!/usr/bin/env python3
"""
Unit tests for reverse_parser source-scoping (filter_in/filter_out `source`).

The reverse-parser must honor a filter term's `source` (both|nzb|debrid) when
assigning a version, exactly like filter_results — so a term scoped to the other
protocol doesn't affect the label. Default (`is_nzb=False`, source 'both') must
be byte-identical to the previous behaviour.
"""

import unittest
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    sys.modules['utilities.settings'] = types.ModuleType('utilities.settings')
    sys.modules['utilities.settings'].get_setting = lambda *a, **k: (a[2] if len(a) > 2 else None)
    # smart_search stub: case-insensitive substring (enough to exercise the gating)
    sf = types.ModuleType('scraper'); sff = types.ModuleType('scraper.functions')
    sfo = types.ModuleType('scraper.functions.other_functions')
    sfo.smart_search = lambda term, text: str(term).lower() in str(text).lower()
    sys.modules['scraper'] = sf
    sys.modules['scraper.functions'] = sff
    sys.modules['scraper.functions.other_functions'] = sfo
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'utilities', 'reverse_parser.py')
    spec = importlib.util.spec_from_file_location('reverse_parser_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rp = _load()
NEG_INF = -float('inf')
PTT = {'title': 'Movie', 'resolution': '1080p', 'source': 'WEB', 'codec': 'x264', 'group': 'GRP'}


def score(filename, cfg, is_nzb):
    return rp._calculate_match_score(filename, dict(PTT), 'v1', cfg, is_nzb=is_nzb)


class TestFilterOutSourceScope(unittest.TestCase):
    def test_debrid_scoped_filter_out_ignored_for_nzb(self):
        cfg = {'filter_out': [{'pattern': 'CAM', 'source': 'debrid'}]}
        # nzb item: a debrid-scoped filter_out must NOT disqualify
        self.assertNotEqual(score('Movie.2020.CAM.x264', cfg, is_nzb=True), NEG_INF)
        # debrid item: it applies -> DQ
        self.assertEqual(score('Movie.2020.CAM.x264', cfg, is_nzb=False), NEG_INF)

    def test_both_scoped_filter_out_applies_to_all(self):
        cfg = {'filter_out': [{'pattern': 'CAM'}]}  # no source -> 'both'
        self.assertEqual(score('Movie.2020.CAM.x264', cfg, is_nzb=True), NEG_INF)
        self.assertEqual(score('Movie.2020.CAM.x264', cfg, is_nzb=False), NEG_INF)

    def test_legacy_string_filter_out_unchanged(self):
        cfg = {'filter_out': ['CAM']}  # legacy plain string -> 'both'
        self.assertEqual(score('Movie.2020.CAM.x264', cfg, is_nzb=True), NEG_INF)


class TestFilterInSourceScope(unittest.TestCase):
    def test_other_protocol_only_filter_in_does_not_dq(self):
        # all filter_in scoped to debrid; evaluated for an nzb item -> no applicable
        # mandatory filter_in -> must NOT DQ
        cfg = {'filter_in': [{'pattern': 'REMUX', 'source': 'debrid'}]}
        self.assertNotEqual(score('Movie.2020.WEB.x264', cfg, is_nzb=True), NEG_INF)

    def test_applicable_filter_in_missing_dqs(self):
        cfg = {'filter_in': [{'pattern': 'REMUX', 'source': 'both'}]}
        # REMUX not in filename, term applies -> DQ
        self.assertEqual(score('Movie.2020.WEB.x264', cfg, is_nzb=True), NEG_INF)
        # present -> not DQ
        self.assertNotEqual(score('Movie.2020.REMUX.x264', cfg, is_nzb=True), NEG_INF)


if __name__ == '__main__':
    unittest.main(verbosity=2)
