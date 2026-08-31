#!/usr/bin/env python3
"""Confirms the shared-log upload runs both the raw log lines and our own
settings snapshot through utilities.log_redaction.scrub() before compression
-- belt-and-suspenders on top of RedactingFormatter, which only covers lines
written after it's active and never sees our snapshot text (built as a plain
string, not emitted through a logging.Logger).

scrub() is scrubbed per line (and once for the snapshot), not over the final
assembled log_content string in one call: scrub() runs several regex passes
per call, and the assembled content can be hundreds of MB across up to 1.5M
lines -- one call over that whole blob can take long enough to hold up the
whole app, since CPython regex matching doesn't release the GIL. Scrubbing
each piece individually keeps every regex call bounded to a single line's
length, matching the cost profile of the live per-record logging path.

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

    def test_process_log_upload_scrubs_both_pieces_per_line(self):
        start = self.source.index("def process_log_upload(task_id):")
        end = self.source.index("def upload_to_paste_cnet(", start)
        body = self.source[start:end]

        self.assertIn("from utilities.log_redaction import scrub", body)
        # Logs are scrubbed line-by-line, not as one assembled blob -- see
        # module docstring for why (unbounded regex cost / GIL hold time).
        self.assertIn("logs = [scrub(line) for line in logs]", body)
        self.assertIn("settings_snapshot = scrub(settings_snapshot)", body)

        # Must run before log_content is assembled from both pieces, not
        # after -- otherwise the joined blob never gets scrubbed at all.
        scrub_call_idx = body.index("logs = [scrub(line) for line in logs]")
        snapshot_build_idx = body.index("f\"{settings_snapshot}\\n\"")
        self.assertLess(scrub_call_idx, snapshot_build_idx)

    def test_scrub_failure_does_not_break_upload(self):
        start = self.source.index("def process_log_upload(task_id):")
        end = self.source.index("def upload_to_paste_cnet(", start)
        body = self.source[start:end]
        scrub_idx = body.index("logs = [scrub(line) for line in logs]")
        # The scrub calls must be wrapped so an import/runtime error there
        # can't take down the whole upload: nearest preceding 'try:' within
        # a tight window, and a matching 'except Exception' right after.
        preceding_window = body[max(0, scrub_idx - 200):scrub_idx]
        self.assertIn("try:", preceding_window)
        self.assertIn("except Exception", body[scrub_idx:scrub_idx + 400])


if __name__ == "__main__":
    unittest.main()
