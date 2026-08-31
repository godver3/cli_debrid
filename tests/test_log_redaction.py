#!/usr/bin/env python3
"""Tests for utilities/log_redaction.py (from PR #488, merged into this branch).

Covers the shapes the original PR described as fixing, plus the username
gap found while integrating it: a Plex friend's username (e.g. an "Other
Plex Watchlist" source) is real, identifying personal information about
someone other than the account holder, and routinely appears in plain
prose log lines ("Starting watchlist retrieval for other Plex user: X")
rather than as a key/value pair the pattern-based pass would catch -- only
the value-based pass, seeded from a 'username' key in config.json, finds
and replaces that literal string wherever it appears.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import utilities.log_redaction as log_redaction
from utilities.log_redaction import scrub, RedactingFormatter, refresh_secrets


class TestPatternBasedRedaction(unittest.TestCase):
    """Key-name-driven redaction; doesn't require config.json to have the value."""

    def setUp(self):
        # Ensure no leftover value-based pattern from another test's config
        # bleeds into these (pattern-only) assertions.
        refresh_secrets()
        self._patch = patch.object(log_redaction, '_get_value_pattern', return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        refresh_secrets()

    def test_quoted_dict_repr(self):
        line = "Loaded config: {'api_key': 'abcdef123456'}"
        self.assertIn('***REDACTED***', scrub(line))
        self.assertNotIn('abcdef123456', scrub(line))

    def test_bare_query_string_kv(self):
        line = "GET https://api.example.com/x?api_key=abcdef123456&other=1"
        result = scrub(line)
        self.assertNotIn('abcdef123456', result)
        self.assertIn('other=1', result)

    def test_bearer_header(self):
        line = "Authorization: Bearer abcdef1234567890"
        result = scrub(line)
        self.assertNotIn('abcdef1234567890', result)

    def test_ordinary_lines_untouched(self):
        line = "Task 'task_heartbeat' finished execution, removed from currently_executing_tasks."
        self.assertEqual(scrub(line), line)

    def test_short_values_not_over_redacted(self):
        # Below MIN_SECRET_LEN -- shouldn't blanket-replace common short words.
        line = "auth = ok"
        self.assertEqual(scrub(line), line)

    def test_fails_open_on_bad_input(self):
        # Should never raise, even on None/empty.
        self.assertEqual(scrub(''), '')
        self.assertIsNone(scrub(None))

    def test_exact_sensitive_key_covered_when_value_not_yet_in_config(self):
        # _SENSITIVE_EXACT entries ('username', 'pass', 'auth', etc.) used to
        # only be checked by the value-based pass (which requires the value
        # to already be sitting in config.json). A not-yet-saved value --
        # e.g. an in-flight connection-test payload -- was invisible to
        # both passes, since _KEY_RE/_MARKER_RE were built only from
        # _SENSITIVE_FRAGMENTS. Fixed by folding _SENSITIVE_EXACT into both.
        line = "Debug payload: {'username': 'newuser123', 'pass': 'hunter12345'}"
        result = scrub(line)
        self.assertNotIn('newuser123', result)
        self.assertNotIn('hunter12345', result)

    def test_bare_exact_key_kv_covered(self):
        line = 'auth=supersecretvalue123'
        result = scrub(line)
        self.assertNotIn('supersecretvalue123', result)


class TestValueBasedRedactionFromConfig(unittest.TestCase):
    """The reliable half: real secret values pulled from config.json, matched
    wherever they appear regardless of surrounding syntax."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = patch.dict(os.environ, {'USER_CONFIG': self.tmpdir.name})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.tmpdir.cleanup()
        refresh_secrets()

    def _write_config(self, data):
        path = os.path.join(self.tmpdir.name, 'config.json')
        with open(path, 'w') as f:
            json.dump(data, f)
        refresh_secrets()

    def test_token_embedded_in_url_with_no_kv_syntax(self):
        self._write_config({'Plex': {'token': 'realplextoken123'}})
        line = "GET https://plex.tv/api/resources?X-Plex-Token=realplextoken123"
        result = scrub(line)
        self.assertNotIn('realplextoken123', result)

    def test_secret_inside_traceback_text(self):
        self._write_config({'Real-Debrid': {'api_key': 'rd-secret-key-9000'}})
        traceback_text = (
            "Traceback (most recent call last):\n"
            "  File \"client.py\", line 10, in add_torrent\n"
            "    requests.post(url + 'rd-secret-key-9000')\n"
            "requests.exceptions.HTTPError: 401\n"
        )
        result = scrub(traceback_text)
        self.assertNotIn('rd-secret-key-9000', result)

    def test_username_value_redacted_in_plain_prose(self):
        # The gap found integrating this PR: 'username' wasn't a sensitive
        # key, so a friend's real Plex username survived in narrated log
        # lines untouched. Fixed by adding 'username' to _SENSITIVE_EXACT.
        self._write_config({
            'Content Sources': {
                'Other Plex Watchlist_1': {'username': 'Bela_I', 'token': 'tok123456'}
            }
        })
        line = "Starting watchlist retrieval for other Plex user: Bela_I"
        result = scrub(line)
        self.assertNotIn('Bela_I', result)
        self.assertIn('***REDACTED***', result)

    def test_unrelated_short_strings_not_swept_up(self):
        self._write_config({'Plex': {'token': 'realplextoken123'}})
        line = "Processing item S01E01 for show 'Ed'"
        self.assertEqual(scrub(line), line)


class TestRedactingFormatter(unittest.TestCase):
    def test_format_applies_scrub(self):
        import logging as _logging
        formatter = RedactingFormatter('%(message)s')
        record = _logging.LogRecord(
            name='test', level=_logging.INFO, pathname=__file__, lineno=1,
            msg="api_key=abcdef123456", args=(), exc_info=None,
        )
        with patch.object(log_redaction, '_get_value_pattern', return_value=None):
            formatted = formatter.format(record)
        self.assertNotIn('abcdef123456', formatted)


if __name__ == "__main__":
    unittest.main()
