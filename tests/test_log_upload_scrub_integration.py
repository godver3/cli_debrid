#!/usr/bin/env python3
"""Confirms the shared-log upload runs the final assembled content (raw log
lines + our own settings snapshot) through utilities.log_redaction.scrub()
as a second, independent pass before compression -- belt-and-suspenders on
top of RedactingFormatter, which only covers lines written after it's
active and never sees our snapshot text (built as a plain string, not
emitted through a logging.Logger).

routes/log_viewer_routes.py imports flask (unavailable in this test
environment), so this test inspects the source directly rather than
importing the module -- consistent with the other flask-route regression
tests in this suite (see test_manual_run_bypasses_cache.py).
"""

import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(PROJECT_ROOT, path), encoding="utf-8") as f:
        return f.read()


class TestLogUploadRunsScrub(unittest.TestCase):
    def setUp(self):
        self.source = _read("routes/log_viewer_routes.py")

    def test_process_log_upload_scrubs_final_content(self):
        start = self.source.index("def process_log_upload(task_id):")
        end = self.source.index("def upload_to_paste_cnet(", start)
        body = self.source[start:end]

        self.assertIn("from utilities.log_redaction import scrub", body)
        self.assertIn("log_content = scrub(log_content)", body)

        # Must run on the fully assembled content (snapshot + logs), not
        # just the raw logs -- scrub() call has to come after log_content
        # is built from both pieces.
        snapshot_build_idx = body.index("f\"{settings_snapshot}\\n\"")
        scrub_call_idx = body.index("log_content = scrub(log_content)")
        self.assertLess(snapshot_build_idx, scrub_call_idx)

    def test_scrub_failure_does_not_break_upload(self):
        start = self.source.index("def process_log_upload(task_id):")
        end = self.source.index("def upload_to_paste_cnet(", start)
        body = self.source[start:end]
        scrub_idx = body.index("log_content = scrub(log_content)")
        # The scrub call must be wrapped so an import/runtime error there
        # can't take down the whole upload: nearest preceding 'try:' within
        # a tight window, and a matching 'except Exception' right after.
        preceding_window = body[max(0, scrub_idx - 200):scrub_idx]
        self.assertIn("try:", preceding_window)
        self.assertIn("except Exception", body[scrub_idx:scrub_idx + 300])


if __name__ == "__main__":
    unittest.main()
