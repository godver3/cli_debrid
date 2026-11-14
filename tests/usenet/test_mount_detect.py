"""Settings → Usenet Mount → Detect (usenet/mount_detect.py)."""
import os
import tempfile
import unittest
from unittest import mock

from usenet import mount_detect as md

MOUNTINFO = """\
22 1 0:20 / / rw,relatime - overlay overlay rw
30 22 0:5 / /proc rw - proc proc rw
41 22 8:1 /appdata/climount /climount_data rw,relatime - ext4 /dev/sda1 rw
42 22 0:44 / /mnt/nzbdav rw,nosuid - fuse.rclone nzbdav: rw
43 22 8:1 /media /media/My\\040Movies rw - ext4 /dev/sda1 rw
"""


class MountinfoTests(unittest.TestCase):
    def test_parse(self):
        mounts = md.parse_mountinfo(MOUNTINFO)
        self.assertIn(('/climount_data', 'ext4'), mounts)
        self.assertIn(('/mnt/nzbdav', 'fuse.rclone'), mounts)
        self.assertIn(('/media/My Movies', 'ext4'), mounts)

    def test_fuse_mounts(self):
        self.assertEqual(md.detect_mount_paths(md.parse_mountinfo(MOUNTINFO)), ['/mnt/nzbdav'])

    def test_data_path_needs_climount_files(self):
        with tempfile.TemporaryDirectory() as d:
            mounts = [(d, 'ext4'), ('/proc', 'proc')]
            with mock.patch.object(md, 'DATA_PATH_GUESSES', []):
                self.assertEqual(md.detect_data_paths(mounts), [])
                open(os.path.join(d, 'config.json'), 'w').close()
                os.mkdir(os.path.join(d, 'usenet'))
                self.assertEqual(md.detect_data_paths(mounts), [d])


class UrlTests(unittest.TestCase):
    def test_candidates_put_current_url_first(self):
        with mock.patch.object(md, '_default_gateway', return_value='172.17.0.1'):
            urls = md.candidate_urls('nzbdav', 'http://box:3000/')
        self.assertEqual(urls[0], 'http://box:3000')
        self.assertIn('http://nzbdav:3000', urls)
        self.assertIn('http://172.17.0.1:3000', urls)

    def test_only_answering_hosts_returned_once_per_server(self):
        answers = {'http://localhost:8888': (True, '2.5.13'), 'http://127.0.0.1:8888': (True, '2.5.13')}
        with mock.patch.object(md, '_default_gateway', return_value=None), \
                mock.patch.object(md, 'probe', side_effect=lambda p, u, t='': answers.get(u, (False, ''))):
            found = md.detect_urls('climount')
        self.assertEqual(found, [{'url': 'http://localhost:8888', 'version': '2.5.13'}])

    def test_nothing_found(self):
        with mock.patch.object(md, '_default_gateway', return_value=None), \
                mock.patch.object(md, 'probe', return_value=(False, '')):
            self.assertEqual(md.detect_urls('zurg'), [])


if __name__ == '__main__':
    unittest.main()
