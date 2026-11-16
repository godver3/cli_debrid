"""A file that reads as 0 bytes is a broken mount view, never proof of a dead release."""
import os
import tempfile
import unittest
from unittest import mock

import database  # noqa: F401  (app import order)
from usenet import repair_engine as re_


class EmptyReadGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _file(self, data: bytes) -> str:
        path = os.path.join(self.tmp.name, 'Show.S01E01.mkv')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_empty_read_is_inconclusive_and_passes_the_gate(self):
        # Stale mount: the file lists with its real size but reads back 0 bytes.
        path = self._file(b'\x1a\x45\xdf\xa3' + b'\0' * 64)
        with mock.patch.object(re_, '_reads_empty', return_value=True), \
             mock.patch.object(re_, '_probe_readable_once', return_value=False) as probe:
            self.assertIsNone(re_.probe_file_playable(path))
            self.assertTrue(re_._verify_file_readable(path))
        probe.assert_not_called()

    def test_zero_byte_file_is_dead_not_inconclusive(self):
        path = self._file(b'')
        with mock.patch.object(re_, '_probe_readable_once', return_value=True) as probe:
            self.assertIs(re_.probe_file_playable(path), False)
        probe.assert_not_called()

    def test_real_content_still_goes_through_the_probe(self):
        path = self._file(b'\x1a\x45\xdf\xa3' + b'\0' * 8192)
        with mock.patch.object(re_, '_media_lacks_video', return_value=False), \
             mock.patch.object(re_, '_media_duration_seconds', return_value=None), \
             mock.patch.object(re_, '_probe_readable_once', return_value=False), \
             mock.patch('time.sleep'):
            self.assertIs(re_.probe_file_playable(path), False)

    def test_reads_empty(self):
        self.assertTrue(re_._reads_empty(self._file(b'')))
        self.assertFalse(re_._reads_empty(self._file(b'abc')))


if __name__ == '__main__':
    unittest.main()
