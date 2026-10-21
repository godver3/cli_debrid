#!/usr/bin/env python3
"""Regression test: get_media_meta()'s TMDB requests must carry a timeout.

utilities/web_scraper.py's get_media_meta() is called synchronously from
database/collected_items.py's _cache_tmdb_artwork(), which the Plex full
scan's DB-reconciliation step (add_collected_items()) calls directly. The
underlying api.get() (routes/api_tracker.py's APITracker.get()) is a bare
requests.Session.get(url, **kwargs) passthrough with no default timeout, so
a stalled TMDB connection with no timeout kwarg blocks the whole scan
indefinitely -- observed live as task_plex_full_scan remaining "Running" for
over 24 hours.

utilities/web_scraper.py pulls in heavy optional deps (fuzzywuzzy, the
scraper package) not available in this test environment, so this test
inspects the source directly rather than importing the module -- consistent
with the other source-inspection regression tests in this suite (see
test_log_upload_scrub_integration.py).
"""

import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestGetMediaMetaTimeout(unittest.TestCase):
    def setUp(self):
        path = os.path.join(PROJECT_ROOT, "utilities", "web_scraper.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        start = source.index("def get_media_meta(")
        # Slice to the next top-level def so we only inspect this function's body.
        end = source.index("\ndef ", start + 1)
        self.body = source[start:end]

    def test_details_request_has_timeout(self):
        self.assertIn("api.get(details_url, timeout=", self.body)

    def test_images_request_has_timeout(self):
        self.assertIn("api.get(images_url, timeout=", self.body)


if __name__ == "__main__":
    unittest.main()
