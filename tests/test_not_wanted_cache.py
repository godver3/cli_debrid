"""The not-wanted lists are cached in memory, keyed by file signature.

Membership checks run once per scrape result, so they must not re-read the
pickle each time, but must still see writes made by any path (this module,
debug routes, a deleted file).
"""
import os
import pickle
import threading
import unittest
from unittest import mock

from database import not_wanted_magnets as nw

HEX = "a" * 40
MAGNET = f"magnet:?xt=urn:btih:{HEX}&dn=Some.Show"


class NotWantedCacheTest(unittest.TestCase):
    def setUp(self):
        base = os.path.join(os.environ["USER_DB_CONTENT"], f"nw_{self._testMethodName}")
        os.makedirs(base, exist_ok=True)
        self.paths = {
            "NOT_WANTED_MAGNETS_FILE": os.path.join(base, "m.pkl"),
            "NOT_WANTED_URLS_FILE": os.path.join(base, "u.pkl"),
            "NOT_WANTED_NZB_GUIDS_FILE": os.path.join(base, "g.pkl"),
            "NOT_WANTED_NZB_SEGMENTS_FILE": os.path.join(base, "s.pkl"),
        }
        for p in self.paths.values():
            if os.path.exists(p):
                os.remove(p)
        patches = [mock.patch.object(nw, k, v) for k, v in self.paths.items()]
        patches.append(mock.patch.object(nw, "get_setting", return_value=False))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        nw._store_cache.clear()

    def test_magnet_match_uses_normalized_hash(self):
        # Stored as a full magnet, checked with a differently-formatted one
        nw.add_to_not_wanted(MAGNET)
        self.assertTrue(nw.is_magnet_not_wanted(f"magnet:?xt=urn:btih:{HEX.upper()}"))
        self.assertFalse(nw.is_magnet_not_wanted("magnet:?xt=urn:btih:" + "b" * 40))

    def test_check_does_not_reread_unchanged_file(self):
        nw.add_to_not_wanted(MAGNET)
        nw.is_magnet_not_wanted(MAGNET)
        with mock.patch.object(nw.pickle, "load", side_effect=AssertionError("re-read")):
            for _ in range(5):
                self.assertTrue(nw.is_magnet_not_wanted(MAGNET))

    def test_external_rewrite_is_picked_up(self):
        nw.add_to_not_wanted(MAGNET)
        self.assertTrue(nw.is_magnet_not_wanted(MAGNET))
        with open(self.paths["NOT_WANTED_MAGNETS_FILE"], "wb") as f:
            pickle.dump(set(), f)
        self.assertFalse(nw.is_magnet_not_wanted(MAGNET))

    def test_deleted_file_is_picked_up(self):
        nw.add_to_not_wanted_nzb_guid("ed914f26add1db0a7cc6a19c6358e5b0")
        self.assertTrue(nw.is_nzb_guid_not_wanted("ed914f26add1db0a7cc6a19c6358e5b0"))
        os.remove(self.paths["NOT_WANTED_NZB_GUIDS_FILE"])
        self.assertFalse(nw.is_nzb_guid_not_wanted("ed914f26add1db0a7cc6a19c6358e5b0"))

    def test_load_returns_private_mutable_copy(self):
        nw.add_to_not_wanted(MAGNET)
        s = nw.load_not_wanted_magnets()
        s.clear()
        self.assertEqual(nw.load_not_wanted_magnets(), {MAGNET})

    def test_url_and_segment_lists(self):
        nw.add_to_not_wanted_urls("http://x/y/file.torrent")
        self.assertTrue(nw.is_url_not_wanted("http://other/host/file.torrent"))
        xml = '<nzb><file><segments><segment>&lt;ABC@x&gt;</segment></segments></file></nzb>'
        nw.add_to_not_wanted_nzb_segment("<ABC@x>")
        self.assertTrue(nw.is_nzb_segment_not_wanted(xml))

    def test_concurrent_adds_lose_nothing(self):
        hashes = [f"{i:040x}" for i in range(40)]
        threads = [threading.Thread(target=nw.add_to_not_wanted, args=(h,)) for h in hashes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(self.paths["NOT_WANTED_MAGNETS_FILE"], "rb") as f:
            self.assertEqual(pickle.load(f), set(hashes))

    def test_purge_empties_list(self):
        nw.add_to_not_wanted(MAGNET)
        nw.purge_not_wanted_magnets_file()
        self.assertFalse(nw.is_magnet_not_wanted(MAGNET))


if __name__ == "__main__":
    unittest.main()
