"""Parsed-config cache and atomic writes in utilities.settings / config_manager.

Other test modules leave stub ``utilities.settings`` modules in sys.modules, so
the real module is loaded from its file under a private name and patched in
only for the duration of each test.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_real_settings():
    spec = importlib.util.spec_from_file_location(
        "_real_utilities_settings", os.path.join(ROOT, "utilities", "settings.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfgcache_")
        self.env = patch.dict(os.environ, {"USER_CONFIG": self.tmp})
        self.env.start()
        self.settings = _load_real_settings()
        self.modules = patch.dict(sys.modules, {"utilities.settings": self.settings})
        self.modules.start()
        self.path = os.path.join(self.tmp, "config.json")
        self._write_raw({
            "Plex": {"url": "plex:32400", "enabled": "true"},
            "Scraping": {"versions": {"Default": {"min_size": 1}}},
            "Content Sources": {"Trakt_1": {"type": "Trakt", "enabled": True}},
        })

    def tearDown(self):
        self.modules.stop()
        self.env.stop()

    def _write_raw(self, config):
        with open(self.path, "w") as f:
            json.dump(config, f)

    def test_existing_get_setting_semantics_are_kept(self):
        s = self.settings
        self.assertEqual(s.get_setting("Plex", "url"), "http://plex:32400")
        self.assertIs(s.get_setting("Plex", "enabled"), True)
        self.assertEqual(s.get_setting("Plex", "missing", "dflt"), "dflt")
        self.assertEqual(s.get_setting("Missing Section", "x", 5), 5)
        self.assertEqual(s.get_setting("Content Sources")["Trakt_1"]["type"], "Trakt")

    def test_repeated_reads_hit_the_cache(self):
        s = self.settings
        real_read = s._read_config_from_disk
        with patch.object(s, "_read_config_from_disk", side_effect=real_read) as reader:
            for _ in range(50):
                s.get_setting("Plex", "url")
                s.load_config()
        self.assertEqual(reader.call_count, 1)

    def test_external_edit_is_picked_up(self):
        s = self.settings
        self.assertEqual(s.get_setting("Plex", "url"), "http://plex:32400")
        self._write_raw({"Plex": {"url": "http://other-host:32400"}})
        self.assertEqual(s.get_setting("Plex", "url"), "http://other-host:32400")

    def test_returned_containers_cannot_corrupt_the_cache(self):
        s = self.settings
        s.get_setting("Scraping", "versions")["Default"]["min_size"] = 999
        s.get_setting("Content Sources")["Trakt_1"]["enabled"] = False
        s.get_setting("Plex")["url"] = "mutated"
        s.load_config()["Plex"]["url"] = "mutated"
        self.assertEqual(s.get_setting("Scraping", "versions")["Default"]["min_size"], 1)
        self.assertIs(s.get_setting("Content Sources")["Trakt_1"]["enabled"], True)
        self.assertEqual(s.get_setting("Plex", "url"), "http://plex:32400")

    def test_set_setting_is_visible_immediately(self):
        s = self.settings
        s.get_setting("Plex", "url")
        s.set_setting("Plex", "url", "http://new:1")
        self.assertEqual(s.get_setting("Plex", "url"), "http://new:1")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["Plex"]["url"], "http://new:1")

    def test_atomic_write_leaves_no_temp_files_and_keeps_mode(self):
        s = self.settings
        os.chmod(self.path, 0o640)
        s.save_config({"Plex": {"url": "http://a"}})
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), oct(0o640))
        leftovers = [n for n in os.listdir(self.tmp) if ".tmp." in n]
        self.assertEqual(leftovers, [])

    def test_failed_rename_falls_back_to_in_place_write(self):
        s = self.settings
        with patch.object(s.os, "replace", side_effect=OSError("EBUSY")):
            s.save_config({"Plex": {"url": "http://bind-mounted"}})
        self.assertEqual(s.get_setting("Plex", "url"), "http://bind-mounted")

    def test_config_manager_save_shares_lock_cache_and_writer(self):
        import queues.config_manager as cm
        with patch.object(cm, "CONFIG_FILE", self.path):
            config = cm.load_config()
            self.assertIn("Scraping", config)
            config["Plex"]["url"] = "http://from-config-manager"
            cm.save_config(config)
        self.assertEqual(self.settings.get_setting("Plex", "url"), "http://from-config-manager")
        self.assertTrue(os.path.exists(self.settings.get_lock_file_path()))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "config.lock")))

    def test_concurrent_writers_never_produce_a_corrupt_file(self):
        s = self.settings
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    s.set_setting("Plex", f"key_{n}", i)
            except Exception as e:  # pragma: no cover - surfaced below
                errors.append(e)

        def reader():
            for _ in range(200):
                with open(self.path) as f:
                    try:
                        json.load(f)
                    except json.JSONDecodeError as e:
                        errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
        threads.append(threading.Thread(target=reader))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.path) as f:
            json.load(f)


if __name__ == "__main__":
    unittest.main()
