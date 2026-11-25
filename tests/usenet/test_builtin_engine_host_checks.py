#!/usr/bin/env python3
"""Host checks for the built-in mount (utilities/builtin_engine.host_problems):
in a container, a mount needs /dev/fuse, SYS_ADMIN and an AppArmor profile
that allows mounting. Values measured on Docker 2026-11-24:

  devices only                       CapBnd without SYS_ADMIN, docker-default (enforce): mount denied
  + cap_add SYS_ADMIN                SYS_ADMIN, docker-default (enforce): mount still denied
  + security_opt apparmor:unconfined SYS_ADMIN, unconfined: mount works
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from utilities import builtin_engine as be

_cap_bounded = be._cap_bounded  # the real reader, pointed at a fake status file below

# CapBnd values from those containers (SYS_ADMIN is bit 21).
DOCKER_DEFAULT_CAPS = '00000000a80425fb'
WITH_SYS_ADMIN = '00000000a82425fb'


class HostChecksTests(unittest.TestCase):
    def _problems(self, caps, profile, fuse=True, container=True):
        real_exists = os.path.exists
        with tempfile.NamedTemporaryFile('w', suffix='status', delete=False) as f:
            f.write(f'Name:\tpython3\nCapEff:\t{caps}\nCapBnd:\t{caps}\n')
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(be, 'IS_WINDOWS', False), \
                mock.patch.object(be.os, 'access', return_value=True), \
                mock.patch.object(be.os.path, 'exists', side_effect=lambda p: fuse if p == '/dev/fuse' else real_exists(p)), \
                mock.patch.object(be, '_in_container', return_value=container), \
                mock.patch.object(be, '_cap_bounded', side_effect=lambda bit: _cap_bounded(bit, f.name)), \
                mock.patch.object(be, '_apparmor_profile', return_value=profile):
            return be.host_problems()

    def test_fuse_device_only_gives_one_line_with_the_whole_fix(self):
        # The reporter's container: /dev/fuse, no cap_add, no security_opt.
        out = self._problems(DOCKER_DEFAULT_CAPS, 'docker-default (enforce)')
        self.assertEqual(len(out), 1, out)
        self.assertIn('lacks SYS_ADMIN', out[0])
        for part in ('cap_add: [SYS_ADMIN]', 'apparmor:unconfined'):
            self.assertIn(part, out[0])

    def test_sys_admin_alone_still_blocked_by_apparmor(self):
        out = self._problems(WITH_SYS_ADMIN, 'docker-default (enforce)')
        self.assertEqual(len(out), 1, out)
        self.assertIn('AppArmor', out[0])

    def test_working_container_has_no_problems(self):
        self.assertEqual(self._problems(WITH_SYS_ADMIN, 'unconfined'), [])
        self.assertEqual(self._problems(WITH_SYS_ADMIN, ''), [], 'host without AppArmor')
        self.assertEqual(self._problems(WITH_SYS_ADMIN, 'docker-default (complain)'), [], 'not enforcing')

    def test_no_fuse_device_names_all_three_settings(self):
        out = self._problems(DOCKER_DEFAULT_CAPS, 'docker-default (enforce)', fuse=False)
        self.assertEqual(len(out), 1, out)
        for part in ('/dev/fuse', 'cap_add: [SYS_ADMIN]', 'apparmor:unconfined'):
            self.assertIn(part, out[0])

    def test_outside_a_container_caps_and_apparmor_are_not_checked(self):
        # Bare-metal Linux mounts through the setuid fusermount helper.
        self.assertEqual(self._problems(DOCKER_DEFAULT_CAPS, 'docker-default (enforce)', container=False), [])

    def test_unreadable_status_is_not_a_problem(self):
        self.assertIsNone(be._cap_bounded(21, '/nonexistent/status'))
        with mock.patch.object(be, 'IS_WINDOWS', False), mock.patch.object(be.os, 'access', return_value=True), \
                mock.patch.object(be.os.path, 'exists', return_value=True), \
                mock.patch.object(be, '_in_container', return_value=True), \
                mock.patch.object(be, '_cap_bounded', return_value=None), \
                mock.patch.object(be, '_apparmor_profile', return_value=''):
            self.assertEqual(be.host_problems(), [])

    def test_cap_bounded_reads_bit_21(self):
        for caps, want in ((DOCKER_DEFAULT_CAPS, False), (WITH_SYS_ADMIN, True), ('000001ffffffffff', True)):
            with tempfile.NamedTemporaryFile('w', delete=False) as f:
                f.write(f'CapBnd:\t{caps}\n')
            self.addCleanup(os.unlink, f.name)
            self.assertIs(be._cap_bounded(21, f.name), want, caps)


ROOT = '1001 900 0:50 / / rw,relatime {root} - overlay overlay rw,lowerdir=/x'
MNT = '1010 1001 259:3 /home/u/mnt /mnt rw,relatime {opt} - ext4 /dev/nvme0n1p3 rw'
FUSE = '1020 1010 0:60 / /mnt/decypharr rw,nosuid,nodev,relatime {opt} - fuse.decypharr decypharr rw,user_id=0'


class MountReachTests(unittest.TestCase):
    """After Switch the engine mounts on cli_mount's path: only a bind with
    shared propagation (rshared) lets the host and Plex see that mount.
    Checked on Docker 2026-11-24 with a real mount: default and rslave, the
    host saw nothing; rshared, the host saw it."""

    def _reach(self, *lines, path='/mnt/decypharr', container=True):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('\n'.join(lines) + '\n')
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(be, 'IS_WINDOWS', False), mock.patch.object(be, '_in_container', return_value=container):
            return be.mount_reaches_host(path, f.name)

    def test_bind_options(self):
        # docker -v /home/u/mnt:/mnt with no option, :rslave and :rshared
        self.assertEqual(self._reach(ROOT.format(root=''), MNT.format(opt='')), {'ok': False, 'bind': '/mnt'})
        self.assertEqual(self._reach(ROOT.format(root='master:1'), MNT.format(opt='master:2')),
                         {'ok': False, 'bind': '/mnt'})
        self.assertEqual(self._reach(ROOT.format(root='shared:1 master:1'), MNT.format(opt='shared:2 master:2')),
                         {'ok': True, 'bind': '/mnt'})

    def test_fuse_mount_on_the_path_is_looked_through(self):
        # cli_mount's own mount sits on the path: the bind under it decides.
        r = self._reach(ROOT.format(root=''), MNT.format(opt=''), FUSE.format(opt='master:3'))
        self.assertEqual(r, {'ok': False, 'bind': '/mnt'})
        r = self._reach(ROOT.format(root='shared:1'), MNT.format(opt='shared:2'), FUSE.format(opt='shared:3'))
        self.assertEqual(r, {'ok': True, 'bind': '/mnt'})

    def test_container_root_never_reaches_the_host(self):
        # With an rshared bind Docker marks the root shared too, but its peer
        # is the image layer, not anywhere Plex looks.
        r = self._reach(ROOT.format(root='shared:1'), MNT.format(opt='shared:2'), path='/opt/media')
        self.assertEqual(r, {'ok': False, 'bind': '/'})

    def test_longest_bind_wins_and_escaped_paths(self):
        lines = (ROOT.format(root=''), MNT.format(opt=''),
                 '1030 1010 259:3 /data/My\\040Media /mnt/My\\040Media rw shared:9 - ext4 /dev/x rw')
        self.assertEqual(self._reach(*lines, path='/mnt/My Media/decypharr'), {'ok': True, 'bind': '/mnt/My Media'})
        self.assertEqual(self._reach(*lines, path='/mnt/Other'), {'ok': False, 'bind': '/mnt'})

    def test_unknown_outside_a_container(self):
        self.assertIsNone(self._reach(ROOT.format(root=''), MNT.format(opt=''), container=False))
        with mock.patch.object(be, 'IS_WINDOWS', False), mock.patch.object(be, '_in_container', return_value=True):
            self.assertIsNone(be.mount_reaches_host('/mnt/x', '/nonexistent/mountinfo'))

    def test_problem_text_names_the_bind_to_change(self):
        with mock.patch.object(be, 'mount_reaches_host', return_value={'ok': False, 'bind': '/mnt'}):
            msg = be.mount_reach_problem('/mnt/decypharr')
        self.assertIn('/mnt:/mnt:rshared', msg)
        self.assertIn('recreate the cli_debrid container', msg)
        with mock.patch.object(be, 'mount_reaches_host', return_value={'ok': False, 'bind': '/'}):
            self.assertIn('/mnt/decypharr:/mnt/decypharr:rshared', be.mount_reach_problem('/mnt/decypharr'))
        for ok in ({'ok': True, 'bind': '/mnt'}, None):
            with mock.patch.object(be, 'mount_reaches_host', return_value=ok):
                self.assertEqual(be.mount_reach_problem('/mnt/decypharr'), '')


if __name__ == '__main__':
    unittest.main()
