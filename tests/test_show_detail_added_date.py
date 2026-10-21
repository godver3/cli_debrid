#!/usr/bin/env python3
"""Regression test: the added_date lookup in routes/library_routes.py
(show_detail_data) must tolerate a NULL episode_number.

The episode sort key used to be `ep.get('episode_number', 999)`. That default
only applies when the *key is absent*, but episode_number is always present in
the episode dict the route builds -- it is copied straight from the DB row,
where the column is nullable. A NULL therefore reached sorted() as None and
took down the whole handler with

    TypeError: '<' not supported between instances of 'int' and 'NoneType'

surfacing as a 500 from /library/show_detail_data. The episode query orders by
`season_number ASC, episode_number ASC` and SQLite sorts NULL first, so the
null-numbered row lands first in season 1 -- exactly the ordering that produces
the "'int' and 'NoneType'" wording above (the reverse ordering yields
"'NoneType' and 'int'").

Rather than re-implement the logic here, these tests lift the real added_date
block out of the source with ast and execute it, so a regression in the shipped
expression fails the test. routes.library_routes itself is far too heavy to
import in a unit test.
"""

import ast
import os
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_PATH = os.path.join(PROJECT_ROOT, "routes", "library_routes.py")

# The buggy form, kept verbatim so the guard test below is unambiguous.
BUGGY_SORT_KEY = "ep.get('episode_number', 999)"


def _read_source():
    with open(SOURCE_PATH, encoding="utf-8") as f:
        return f.read()


def _iter_statement_lists(node):
    """Yield every non-empty statement list under node (try/if/for bodies included)."""
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(child, field, None)
            if isinstance(statements, list) and statements and all(
                isinstance(item, ast.stmt) for item in statements
            ):
                yield statements


def _find_show_detail_data(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "show_detail_data":
            return node
    raise AssertionError(
        "show_detail_data() not found in routes/library_routes.py -- this test "
        "extracts its added_date block and needs updating if it was renamed."
    )


def _added_date_block():
    """
    Source of the real `added_date = None` + `if seasons_list: ...` block.

    Executable standalone: it only reads seasons_list and builtins.
    """
    function = _find_show_detail_data(ast.parse(_read_source()))
    for statements in _iter_statement_lists(function):
        for index, node in enumerate(statements[:-1]):
            is_added_date_init = (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "added_date"
                and isinstance(node.value, ast.Constant)
                and node.value.value is None
            )
            if not is_added_date_init:
                continue
            guard = statements[index + 1]
            if not (
                isinstance(guard, ast.If)
                and isinstance(guard.test, ast.Name)
                and guard.test.id == "seasons_list"
            ):
                raise AssertionError(
                    "`added_date = None` is no longer followed by `if seasons_list:` "
                    "-- update this test to match the restructured block."
                )
            return ast.unparse(ast.Module(body=[node, guard], type_ignores=[]))
    raise AssertionError(
        "Could not locate the `added_date = None` block in show_detail_data() "
        "-- update this test to match the restructured code."
    )


def _episode_sort_key():
    """The real episode sort key from that block, as a callable."""
    block = ast.parse(_added_date_block())
    for node in ast.walk(block):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sorted"
        ):
            for keyword in node.keywords:
                if keyword.arg == "key":
                    return eval(ast.unparse(keyword.value))
    raise AssertionError(
        "No `sorted(..., key=...)` call in the added_date block -- update this test."
    )


def _added_date(seasons_list):
    """Run the real block against seasons_list and return the added_date it picks."""
    # One namespace, used as globals: the block's generator expressions get their
    # own scope and would not see seasons_list if it were passed as locals.
    namespace = {"seasons_list": seasons_list}
    exec(_added_date_block(), namespace)
    return namespace["added_date"]


def _episode(episode_number, collected_at=None):
    # Mirrors the shape built in show_detail_data: episode_number is always
    # present, and carries the DB value verbatim -- including NULL.
    return {"episode_number": episode_number, "collected_at": collected_at}


def _season(season_number, episodes):
    return {"season_number": season_number, "episodes": episodes}


class TestAddedDateWithNullEpisodeNumber(unittest.TestCase):
    def test_null_episode_number_first_does_not_raise(self):
        # The ordering the DB actually returns (SQLite sorts NULL first), and the
        # one that produced the reported "'int' and 'NoneType'" TypeError.
        seasons_list = [
            _season(1, [
                _episode(None, "2024-01-05 00:00:00"),
                _episode(1, "2024-01-01 00:00:00"),
                _episode(2, "2024-01-02 00:00:00"),
            ])
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-01 00:00:00")

    def test_null_episode_number_last_does_not_raise(self):
        # The mirror-image ordering, which raised "'NoneType' and 'int'".
        seasons_list = [
            _season(1, [
                _episode(1, "2024-01-01 00:00:00"),
                _episode(2, "2024-01-02 00:00:00"),
                _episode(None, "2024-01-05 00:00:00"),
            ])
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-01 00:00:00")

    def test_all_episode_numbers_null(self):
        seasons_list = [_season(1, [_episode(None, "2024-01-05 00:00:00")])]
        self.assertEqual(_added_date(seasons_list), "2024-01-05 00:00:00")

    def test_null_episode_number_used_only_as_a_last_resort(self):
        # A null-numbered episode must not win over a numbered one that also has
        # a collected_at, whichever order the rows arrive in.
        seasons_list = [
            _season(1, [
                _episode(None, "2024-01-05 00:00:00"),
                _episode(3, "2024-01-03 00:00:00"),
            ])
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-03 00:00:00")


class TestAddedDateSelection(unittest.TestCase):
    """The behaviour the sort exists to provide, pinned so the fix kept it."""

    def test_uses_lowest_numbered_episode_with_a_collected_at(self):
        seasons_list = [
            _season(1, [
                _episode(3, "2024-01-03 00:00:00"),
                _episode(1, "2024-01-01 00:00:00"),
                _episode(2, "2024-01-02 00:00:00"),
            ])
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-01 00:00:00")

    def test_skips_uncollected_episodes(self):
        seasons_list = [
            _season(1, [
                _episode(1, None),
                _episode(2, "2024-01-02 00:00:00"),
            ])
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-02 00:00:00")

    def test_only_season_one_is_consulted(self):
        seasons_list = [
            _season(0, [_episode(1, "2023-01-01 00:00:00")]),
            _season(2, [_episode(1, "2023-06-01 00:00:00")]),
            _season(1, [_episode(1, "2024-01-01 00:00:00")]),
        ]
        self.assertEqual(_added_date(seasons_list), "2024-01-01 00:00:00")

    def test_no_season_one(self):
        seasons_list = [_season(2, [_episode(1, "2024-01-01 00:00:00")])]
        self.assertIsNone(_added_date(seasons_list))

    def test_no_seasons(self):
        self.assertIsNone(_added_date([]))

    def test_nothing_collected(self):
        seasons_list = [_season(1, [_episode(1, None), _episode(2, None)])]
        self.assertIsNone(_added_date(seasons_list))


class TestEpisodeSortKey(unittest.TestCase):
    def test_null_sorts_after_every_real_episode_number(self):
        key = _episode_sort_key()
        episodes = [_episode(None), _episode(2), _episode(1), _episode(10)]
        ordered = [ep["episode_number"] for ep in sorted(episodes, key=key)]
        self.assertEqual(ordered, [1, 2, 10, None])

    def test_key_is_never_none(self):
        key = _episode_sort_key()
        self.assertIsNotNone(key(_episode(None)))
        self.assertIsNotNone(key(_episode(0)))

    def test_episode_zero_is_not_treated_as_missing(self):
        # `or 999` would be a tempting shorter fix, but 0 is a real episode
        # number and must keep sorting first.
        key = _episode_sort_key()
        episodes = [_episode(1), _episode(0), _episode(None)]
        ordered = [ep["episode_number"] for ep in sorted(episodes, key=key)]
        self.assertEqual(ordered, [0, 1, None])

    def test_old_key_reproduced_the_reported_error(self):
        # Characterization of the root cause: a dict.get default cannot help when
        # the key is present with a None value.
        episodes = [_episode(None), _episode(1)]
        with self.assertRaises(TypeError) as caught:
            sorted(episodes, key=lambda ep: ep.get("episode_number", 999))
        self.assertIn("'int' and 'NoneType'", str(caught.exception))


class TestSourceGuard(unittest.TestCase):
    def test_buggy_sort_key_is_gone(self):
        # Guard against the fix being reverted to the absent-key default.
        # assertFalse rather than assertNotIn: the latter dumps the whole
        # 5k-line source into the failure output.
        self.assertFalse(
            BUGGY_SORT_KEY in _read_source(),
            f"routes/library_routes.py uses `{BUGGY_SORT_KEY}` again. The 999 "
            "default never applies -- episode_number is always present and may "
            "be None, which makes sorted() raise TypeError.",
        )


if __name__ == "__main__":
    unittest.main()
