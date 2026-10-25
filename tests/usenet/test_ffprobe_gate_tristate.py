#!/usr/bin/env python3
"""ffprobe playability probe: inconclusive stays readable, no-video files fail.

Files Plex showed with no audio/video (RAR volumes stitched out of order)
passed because a packet was read. An inconclusive probe (every read timed
out) is still treated as readable by the gates, so a busy mount never
rejects or holds back a good file.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

for _var in ('USER_CONFIG', 'USER_DB_CONTENT', 'USER_LOGS'):
    os.environ.setdefault(_var, tempfile.mkdtemp())

import database  # noqa: F401  (app import order; avoids a debrid<->routes cycle)
import usenet.repair_engine as re_mod


class _StubbedProbe(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.mkv')
        os.write(fd, b'x')
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        for name, value in (('_media_duration_seconds', lambda *a, **k: None),
                            ('_media_lacks_video', lambda *a, **k: False)):
            orig = getattr(re_mod, name)
            setattr(re_mod, name, value)
            self.addCleanup(setattr, re_mod, name, orig)
        import time
        orig_sleep = time.sleep
        time.sleep = lambda *a, **k: None
        self.addCleanup(setattr, time, 'sleep', orig_sleep)

    def _probe_returns(self, *seq):
        seq = list(seq)
        orig = re_mod._probe_readable_once
        re_mod._probe_readable_once = lambda *a, **k: seq.pop(0) if len(seq) > 1 else seq[0]
        self.addCleanup(setattr, re_mod, '_probe_readable_once', orig)


class TestProbeFilePlayable(_StubbedProbe):
    def test_all_timeouts_is_inconclusive(self):
        self._probe_returns(None, None, None)
        self.assertIsNone(re_mod.probe_file_playable(self.path))
        # Repair of existing files keeps its conservative answer.
        self.assertTrue(re_mod._verify_file_readable(self.path))

    def test_clean_failures_is_dead(self):
        self._probe_returns(False)
        self.assertIs(re_mod.probe_file_playable(self.path), False)

    def test_readable(self):
        self._probe_returns(None, True)
        self.assertIs(re_mod.probe_file_playable(self.path), True)

    def test_no_video_stream_is_dead_without_reading(self):
        re_mod._media_lacks_video = lambda *a, **k: True
        self._probe_returns(True)
        self.assertIs(re_mod.probe_file_playable(self.path), False)
        self.assertIs(re_mod._verify_file_readable(self.path), False)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'needs ffmpeg/ffprobe')
class TestMediaLacksVideoReal(unittest.TestCase):
    def _make(self, *args):
        fd, path = tempfile.mkstemp(suffix='.mkv')
        os.close(fd)
        self.addCleanup(os.unlink, path)
        subprocess.run(['ffmpeg', '-v', 'error', '-y', *args, '-t', '1', path], check=True)
        return path

    def test_audio_only_container_lacks_video(self):
        path = self._make('-f', 'lavfi', '-i', 'sine=frequency=440')
        self.assertTrue(re_mod._media_lacks_video(path))

    def test_video_file_has_video(self):
        path = self._make('-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=5')
        self.assertFalse(re_mod._media_lacks_video(path))

    def test_garbage_is_not_proof(self):
        fd, path = tempfile.mkstemp(suffix='.mkv')
        os.write(fd, os.urandom(4096))
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertFalse(re_mod._media_lacks_video(path))


if __name__ == '__main__':
    unittest.main()
