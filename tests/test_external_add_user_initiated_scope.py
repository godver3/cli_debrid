#!/usr/bin/env python3
"""Regression test: only a real user action may bypass the ghostlist.

add_media_item(user_initiated=True) skips the ghostlist/blacklist guard. #0e3608f
gave that to the rclone webhook, correctly -- content a person put in the debrid
account by hand is disproportionately likely to be ghostlisted, because that is
usually why they went and fetched it elsewhere.

It derived the flag rather than taking it as a parameter:

    is_external_add = trigger_plex_update_on_success
    add_media_item(item, user_initiated=is_external_add)

The reasoning was that deriving it meant "a future external-add caller cannot
silently reintroduce the bug". The opposite happened. utilities/external_mount_scan.py
(added days earlier) hardcodes trigger_plex_update_on_success=True on every call,
so the periodic scan silently acquired the bypass. That scan runs on a timer over
whatever is in the mount -- including cli_debrid's own leftovers -- so it began
importing the exact releases the queues had just blacklisted, one library row per
junk torrent. Observed live: 10 rows for a single episode, 11 for one season.

So: user_initiated is an explicit parameter, and the periodic scan must not pass it.
"""

import ast
import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TASK = '_run_rclone_to_symlink_task'
# Position of user_initiated in the task's positional signature.
USER_INITIATED_INDEX = 6


def _parse(relpath):
    with open(os.path.join(PROJECT_ROOT, relpath), encoding='utf-8') as f:
        return ast.parse(f.read())


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f'{name} not found')


def _const(node):
    return node.value if isinstance(node, ast.Constant) else '<not-a-literal>'


class TestTaskTakesAnExplicitFlag(unittest.TestCase):
    def setUp(self):
        self.fn = _find_function(_parse('routes/debug_routes.py'), TASK)

    def test_user_initiated_is_a_parameter(self):
        args = [a.arg for a in self.fn.args.args]
        self.assertIn('user_initiated', args,
                      'must be decided by the caller, not inferred inside the task')

    def test_it_defaults_to_false(self):
        args = [a.arg for a in self.fn.args.args]
        idx = args.index('user_initiated')
        default = self.fn.args.defaults[idx - (len(args) - len(self.fn.args.defaults))]
        self.assertIs(_const(default), False,
                      'a caller that says nothing must not get the bypass')

    def test_it_sits_where_the_callers_pass_it(self):
        args = [a.arg for a in self.fn.args.args]
        self.assertEqual(args.index('user_initiated'), USER_INITIATED_INDEX,
                         'callers pass this positionally; moving it silently rebinds them')

    def test_the_flag_is_not_derived_from_the_plex_trigger(self):
        """The specific regression: two unrelated concerns sharing one variable."""
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Assign):
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == 'is_external_add':
                    self.fail('is_external_add is back -- user_initiated must not be '
                              'derived from trigger_plex_update_on_success')

    def test_add_media_item_is_passed_the_parameter_itself(self):
        for node in ast.walk(self.fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'add_media_item'):
                kw = {k.arg: k.value for k in node.keywords}
                self.assertIn('user_initiated', kw)
                value = kw['user_initiated']
                self.assertTrue(isinstance(value, ast.Name) and value.id == 'user_initiated',
                                'must forward the parameter verbatim')
                return
        self.fail('no add_media_item call found in the task')


class TestCallersPassTheRightValue(unittest.TestCase):
    """Three callers, three different answers."""

    def _task_call_args(self, relpath):
        """Positional args of every threading.Thread(target=<task>, args=(...))."""
        tree = _parse(relpath)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            target = kw.get('target')
            if not (isinstance(target, ast.Name) and target.id == TASK):
                continue
            args = kw.get('args')
            if isinstance(args, ast.Tuple):
                found.append(args.elts)
        return found

    def test_rclone_webhook_keeps_the_bypass(self):
        calls = self._task_call_args('routes/webhook_routes.py')
        self.assertEqual(len(calls), 1)
        elts = calls[0]
        self.assertEqual(len(elts), USER_INITIATED_INDEX + 1,
                         'webhook must pass user_initiated positionally')
        self.assertIs(_const(elts[USER_INITIATED_INDEX]), True,
                      'a person put this content in the mount; the bypass is intended here')

    def test_manual_bulk_scan_does_not_get_the_bypass(self):
        calls = self._task_call_args('routes/debug_routes.py')
        self.assertEqual(len(calls), 1)
        elts = calls[0]
        self.assertEqual(len(elts), USER_INITIATED_INDEX + 1)
        self.assertIs(_const(elts[USER_INITIATED_INDEX]), False,
                      'the bulk scan sweeps the whole mount -- it must not unghost '
                      'everything still sitting there')

    def test_periodic_mount_scan_does_not_get_the_bypass(self):
        """The regression itself. This call passes trigger_plex_update_on_success=True,
        which is exactly what used to hand it the bypass."""
        tree = _parse('utilities/external_mount_scan.py')
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == TASK]
        self.assertEqual(len(calls), 1)
        elts = calls[0].args
        self.assertEqual(len(elts), USER_INITIATED_INDEX + 1,
                         'the periodic scan must state user_initiated explicitly rather '
                         'than leaving it to a default that could later change')
        self.assertIs(_const(elts[USER_INITIATED_INDEX]), False)
        # And confirm the trap is still present: it does pass the plex trigger.
        self.assertIs(_const(elts[4]), True,
                      'if this ever becomes False the derivation bug is masked rather '
                      'than fixed, and this test stops proving anything')


if __name__ == '__main__':
    unittest.main(verbosity=2)
