#!/usr/bin/env python3
"""
Unit tests for repair_engine._verify_file_readable's conservative aggregation.

This guard decides whether the repair engine may delete + re-scrape an item the
provider flagged as broken: a False return permits deletion. The safety-critical
property is that transient/inconclusive read results on lazy debrid/FUSE mounts
must NEVER be reported as 'dead' (which would delete a good file) — only a
consistent CLEAN failure counts as unreadable.

The per-attempt probe (_probe_readable_once) is monkeypatched so we test the
retry/aggregation decision, not ffprobe itself.
"""

import unittest
import sys
import os
import types
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _permissive_module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr: (lambda *a, **k: None)  # any imported name → dummy callable
    return m


def _load():
    # Stub third-party + cli-debrid imports repair_engine pulls at module scope.
    if 'requests' not in sys.modules:
        sys.modules['requests'] = _permissive_module('requests')
    for n in ['database', 'database.core', 'database.nzb_repair_activity',
              'database.not_wanted_magnets']:
        sys.modules[n] = _permissive_module(n)
    if 'utilities' not in sys.modules:
        sys.modules['utilities'] = types.ModuleType('utilities')
    uss = types.ModuleType('utilities.settings')
    uss.get_setting = lambda *a, **k: (a[2] if len(a) > 2 else None)
    sys.modules['utilities.settings'] = uss
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'usenet', 'repair_engine.py')
    spec = importlib.util.spec_from_file_location('repair_engine_under_test', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


re_mod = _load()


class TestVerifyFileReadable(unittest.TestCase):
    def setUp(self):
        # real file so os.path.exists() is True; path must not start with /debrid/
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.write(b'x')
        self.tmp.close()
        self.addCleanup(lambda: os.unlink(self.tmp.name))
        # no-op sleeps to keep tests fast
        import time
        self._orig_sleep = time.sleep
        time.sleep = lambda *a, **k: None
        self.addCleanup(lambda: setattr(time, 'sleep', self._orig_sleep))
        self._orig_probe = re_mod._probe_readable_once
        self.addCleanup(lambda: setattr(re_mod, '_probe_readable_once', self._orig_probe))
        # keep tests hermetic: don't actually shell out for duration
        self._orig_dur = re_mod._media_duration_seconds
        re_mod._media_duration_seconds = lambda *a, **k: None
        self.addCleanup(lambda: setattr(re_mod, '_media_duration_seconds', self._orig_dur))

    def _stub_probe(self, sequence):
        seq = list(sequence)
        default = sequence[-1] if sequence else None
        self.calls = 0
        self.offsets = []
        def fake(file_path, offset_seconds=None, timeout=10):
            self.calls += 1
            self.offsets.append(offset_seconds)
            return seq.pop(0) if seq else default
        re_mod._probe_readable_once = fake

    def test_missing_path_is_unreadable(self):
        self.assertFalse(re_mod._verify_file_readable('/does/not/exist/file.mkv'))

    def test_empty_path_is_unreadable(self):
        self.assertFalse(re_mod._verify_file_readable(''))

    def test_success_first_attempt(self):
        self._stub_probe([True])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name))
        self.assertEqual(self.calls, 1)  # short-circuits on success

    def test_all_clean_failures_is_unreadable(self):
        self._stub_probe([False, False, False])
        self.assertFalse(re_mod._verify_file_readable(self.tmp.name, attempts=3))
        self.assertEqual(self.calls, 3)

    def test_transient_then_success(self):
        self._stub_probe([None, True])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_any_inconclusive_means_keep(self):
        # a clean fail mixed with a transient -> must NOT delete (conservative True)
        self._stub_probe([False, None, False])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_all_inconclusive_means_keep(self):
        self._stub_probe([None, None, None])
        self.assertTrue(re_mod._verify_file_readable(self.tmp.name, attempts=3))

    def test_offset_is_random_fraction_of_duration(self):
        # With a known duration, the probe offset must be a deep 20–80% point,
        # and constant across the retries within one call.
        re_mod._media_duration_seconds = lambda *a, **k: 1000.0
        self._stub_probe([False, False])  # force 2 attempts (no success)
        re_mod._verify_file_readable(self.tmp.name, attempts=2)
        self.assertTrue(self.offsets, 'probe was not called')
        for off in self.offsets:
            self.assertIsNotNone(off)
            self.assertGreaterEqual(off, 200.0)   # >= 20% of 1000
            self.assertLessEqual(off, 800.0)       # <= 80% of 1000
        self.assertEqual(len(set(self.offsets)), 1, 'offset must be constant across retries')

    def test_unknown_duration_falls_back_to_start(self):
        re_mod._media_duration_seconds = lambda *a, **k: None
        self._stub_probe([False])
        re_mod._verify_file_readable(self.tmp.name, attempts=1)
        self.assertEqual(self.offsets, [None])  # start-read (no offset)


if __name__ == '__main__':
    unittest.main(verbosity=2)
