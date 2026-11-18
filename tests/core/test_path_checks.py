#!/usr/bin/env python3
"""Onboarding host-path checks (utilities/path_checks.py)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import path_checks as pc

# Real lines from a container: overlay root, an rslave bind (master:),
# a plain bind, an rshared bind with the engine's FUSE mount on top.
MOUNTINFO = '\n'.join([
    '10252 5640 0:99 / / rw,relatime master:50 - overlay overlay rw,lowerdir=/x',
    '10262 10252 259:1 /builtin_test/symlinks /symlinks ro,relatime - ext4 /dev/nvme0n1p1 rw',
    '10267 10252 259:1 /builtin_test/mnt2 /mnt/slave rw,relatime master:106 - ext4 /dev/nvme0n1p1 rw',
    '10268 10252 259:1 /media/My\\040Files /mnt/with\\040space rw - ext4 /dev/nvme0n1p1 rw',
    '10270 10252 259:1 /builtin_test/clid /mnt/clid rw,relatime shared:200 master:1 - ext4 /dev/nvme0n1p1 rw',
    '10300 10270 0:300 / /mnt/clid rw,nosuid,nodev,relatime shared:201 - fuse.clid clid-engine rw,user_id=0',
])


class MountinfoTests(unittest.TestCase):
    def setUp(self):
        self.entries = pc.parse_mountinfo(MOUNTINFO)

    def test_parse(self):
        by_mp = {}
        for e in self.entries:
            by_mp.setdefault(e['mount_point'], []).append(e)
        self.assertEqual(by_mp['/mnt/slave'][0]['propagation'], 'slave')
        self.assertEqual(by_mp['/symlinks'][0]['propagation'], 'private')
        self.assertEqual(by_mp['/mnt/with space'][0]['root'], '/media/My Files')
        self.assertEqual([e['fstype'] for e in by_mp['/mnt/clid']], ['ext4', 'fuse.clid'])

    def test_unmapped_folder_lives_in_the_container(self):
        ok, msg, warn = pc.host_mapping('/data/symlinks', 'Symlink folder', entries=self.entries)
        self.assertFalse(ok)
        self.assertIn('- /data/symlinks:/data/symlinks', msg)

    def test_mapped_folders(self):
        self.assertTrue(pc.host_mapping('/symlinks/Movies', 'Symlink folder', entries=self.entries)[0])
        self.assertTrue(pc.host_mapping('/mnt/with space/x', 'Mount folder', entries=self.entries)[0])

    def test_builtin_mount_needs_rshared_beneath_the_fuse_mount(self):
        self.assertTrue(pc.host_mapping('/mnt/clid', 'Built-in mount folder', need_shared=True, entries=self.entries)[0])
        ok, msg, _ = pc.host_mapping('/mnt/slave', 'Built-in mount folder', need_shared=True, entries=self.entries)
        self.assertFalse(ok)
        self.assertIn(':rshared', msg)

    def test_not_in_a_container_is_not_checked(self):
        with mock.patch.object(pc, 'in_container', return_value=False):
            self.assertIsNone(pc.host_mapping('/anything', 'x'))


class MediaServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.server = {'kind': 'plex', 'name': 'Plex', 'url': 'http://plex:32400', 'token': 't'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_marker_seen_and_removed(self):
        def seen(url, token, path):
            return set(os.listdir(path))  # Plex sees the same folder
        with mock.patch.object(pc, 'plex_list', side_effect=seen):
            ok, msg, warn = pc.server_sees_folder(self.server, self.dir, 'symlink folder')
        self.assertEqual((ok, warn), (True, False))
        self.assertEqual(os.listdir(self.dir), [], 'marker removed')

    def test_marker_missing_is_a_failure(self):
        with mock.patch.object(pc, 'plex_list', return_value={'Movies'}):
            ok, msg, warn = pc.server_sees_folder(self.server, self.dir, 'symlink folder')
        self.assertEqual((ok, warn), (False, False))
        self.assertIn(f'- {self.dir}:{self.dir}', msg)

    def test_unreachable_server_is_only_a_warning(self):
        with mock.patch.object(pc, 'plex_list', return_value=None):
            self.assertEqual(pc.server_sees_folder(self.server, self.dir, 'x')[:3:2], (True, True))

    def test_mount_compare(self):
        os.mkdir(os.path.join(self.dir, '__all__'))
        with mock.patch.object(pc, 'plex_list', return_value={'__all__', 'nzbs'}):
            self.assertTrue(pc.server_sees_mount(self.server, self.dir)[0])
        with mock.patch.object(pc, 'plex_list', return_value=set()):
            ok, msg, warn = pc.server_sees_mount(self.server, self.dir)
            self.assertEqual((ok, warn), (False, False))
            self.assertIn(':rslave', msg)
        os.rmdir(os.path.join(self.dir, '__all__'))
        with mock.patch.object(pc, 'plex_list', return_value=set()):
            self.assertEqual(pc.server_sees_mount(self.server, self.dir)[::2], (True, True), 'empty mount: nothing to compare')

    def test_plex_browse_request(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {'MediaContainer': {'Path': [{'path': '/symlinks/Movies', 'title': 'Movies'}],
                                                     'File': [{'path': '/symlinks/a.txt', 'title': 'a.txt'}]}}
        with mock.patch.object(pc.requests, 'get', return_value=resp) as get:
            self.assertEqual(pc.plex_list('http://plex:32400/', 'tok', '/symlinks'), {'Movies', 'a.txt'})
        self.assertEqual(get.call_args[0][0], 'http://plex:32400/services/browse/L3N5bWxpbmtz')

    def test_media_server_choice(self):
        self.assertEqual(pc.media_server({'Plex': {'url': 'u', 'token': 't'}})['kind'], 'plex')
        sym = {'File Management': {'file_collection_management': 'Symlinked/Local'},
               'Debug': {'emby_jellyfin_url': 'j', 'emby_jellyfin_token': 'k'}}
        self.assertEqual(pc.media_server(sym)['kind'], 'jellyfin')
        self.assertIsNone(pc.media_server({'File Management': {'file_collection_management': 'Symlinked/Local'}}))
        self.assertEqual(pc.mount_root('/mnt/clid/__all__/'), '/mnt/clid')


if __name__ == '__main__':
    unittest.main()
