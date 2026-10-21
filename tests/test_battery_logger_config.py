#!/usr/bin/env python3
"""Regression test for cli_battery/app/logger_config.py's RedactingColoredFormatter.

`format()` calls `scrub(...)` but the module previously imported only
`RedactingFormatter` from utilities.log_redaction, so any Battery log record
reaching this formatter raised `NameError: name 'scrub' is not defined`,
masking the original warning/error (godver3/cli_debrid#495).

`import cli_battery.app.logger_config` drags in cli_battery/app/__init__.py,
which imports sqlalchemy (not installed in this test environment, same
constraint as the other tests in this suite that avoid `import database`/
`import routes.library_routes`) -- so the module is loaded directly from its
file path instead, bypassing the package __init__.
"""

import importlib.util
import logging
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGGER_CONFIG_PATH = os.path.join(PROJECT_ROOT, 'cli_battery', 'app', 'logger_config.py')


def _load_logger_config():
    sys.path.insert(0, PROJECT_ROOT)
    # Module-level `logger = setup_logger()` runs on import and os.makedirs()s
    # USER_LOGS (default /user/logs) -- redirect to a temp dir so this doesn't
    # need write access outside the test environment.
    os.environ.setdefault('USER_LOGS', tempfile.mkdtemp(prefix='battery_logger_test_'))
    spec = importlib.util.spec_from_file_location('_battery_logger_config_under_test', LOGGER_CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logger_config = _load_logger_config()


class TestRedactingColoredFormatter(unittest.TestCase):
    def setUp(self):
        # RedactingColoredFormatter is a local class inside setup_logger(),
        # not a module-level name -- the module-level `logger = setup_logger()`
        # already ran on import, so pull the live formatter off its console
        # handler (added before the file handler; see logger_config.py).
        self.formatter = logger_config.logger.handlers[0].formatter

    def _make_record(self, message):
        return logging.LogRecord(
            name='cli_battery', level=logging.WARNING, pathname=__file__,
            lineno=1, msg=message, args=(), exc_info=None,
        )

    def test_scrub_is_importable_and_used(self):
        # Guards against the NameError regressing: format() must not raise.
        record = self._make_record('warning with api_key=abcdef1234567890abcdef')
        result = self.formatter.format(record)
        self.assertNotIn('abcdef1234567890abcdef', result)

    def test_plain_message_passes_through(self):
        record = self._make_record('routine status update, nothing sensitive')
        result = self.formatter.format(record)
        self.assertIn('routine status update, nothing sensitive', result)


if __name__ == '__main__':
    unittest.main()
