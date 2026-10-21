#!/usr/bin/env python3
"""Queue-added NZB items must keep the chosen release's score and resolution.

AddingQueue.process wrote current_score/resolution only on the torrent path. The
NZB branch `continue`s before that block, so every NZB episode added by the queue
was saved with current_score 0 and resolution NULL (802 of 822 NZB episodes on a
live instance). Coalesced and pulled-in siblings share the pack's release, so they
copy the pack owner's score/resolution.
"""

import ast
import os
import unittest
from typing import Any, Dict, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _load_helper():
    # Importing queues.adding_queue cold pulls in `debrid`, which has a circular
    # import, so exec just the helper's source.
    src = _read('queues', 'adding_queue.py')
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == 'chosen_result_score_fields')
    ns = {'Optional': Optional, 'Dict': Dict, 'Any': Any}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'adding_queue.py', 'exec'), ns)
    return ns['chosen_result_score_fields']


chosen_result_score_fields = _load_helper()


class TestChosenResultScoreFields(unittest.TestCase):
    def test_score_and_resolution(self):
        result = {'score_breakdown': {'total_score': 417.83}, 'resolution': '1080p'}
        self.assertEqual(chosen_result_score_fields(result),
                         {'current_score': 417.83, 'resolution': '1080p'})

    def test_missing_values_are_left_out(self):
        self.assertEqual(chosen_result_score_fields({}), {})
        self.assertEqual(chosen_result_score_fields(None), {})
        self.assertEqual(chosen_result_score_fields({'score_breakdown': None, 'resolution': ''}), {})

    def test_zero_score_is_kept(self):
        # A real 0 is a score; only a missing one is left out.
        self.assertEqual(chosen_result_score_fields({'score_breakdown': {'total_score': 0}}),
                         {'current_score': 0})


class TestNzbPathsStoreScore(unittest.TestCase):
    def test_adding_queue_nzb_branch_writes_score(self):
        src = _read('queues', 'adding_queue.py')
        start = src.index("if torrent_info.get('_is_nzb'):")
        branch = src[start:src.index('# --- Process Files (Parse Once) ---', start)]
        self.assertIn('_nzb_score_kwargs = chosen_result_score_fields(chosen_result_info)', branch)
        self.assertIn('**_nzb_score_kwargs', branch)

    def test_coalesce_copies_sibling_score(self):
        src = _read('queues', 'scraping_queue.py')
        self.assertIn('"current_score, resolution "', src)
        self.assertIn("_coal_seg_kwargs['current_score'] = _sibling_nzb[6]", src)
        self.assertIn("_coal_seg_kwargs['resolution'] = _sibling_nzb[7]", src)

    def test_sibling_pulls_copy_pack_score(self):
        src = _read('queues', 'run_program.py')
        self.assertIn('**_seg_e_kw, **_score_e_kw)', src)
        self.assertIn('**_pull_score_kwargs,', src)


if __name__ == '__main__':
    unittest.main()
