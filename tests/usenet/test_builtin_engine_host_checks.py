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


if __name__ == '__main__':
    unittest.main()
