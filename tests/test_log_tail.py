"""_tail_lines must return exactly what deque(open(path), maxlen=n) did,
while reading only the end of the file."""
import ast
import os
import tempfile
import unittest
from collections import deque

_ROUTES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routes", "log_viewer_routes.py")


def _load_tail_lines():
    # Pull the helper out of the module source: importing the routes package
    # pulls in the whole app and clashes with other tests' module stubs.
    tree = ast.parse(open(_ROUTES).read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tail_lines")
    ns = {"os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), _ROUTES, "exec"), ns)
    return ns["_tail_lines"]


_tail_lines = _load_tail_lines()


class TailLinesTest(unittest.TestCase):
    def _check(self, content, n, block_size):
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(content)
        self.addCleanup(os.remove, f.name)
        with open(f.name, "r", encoding="utf-8") as fh:
            expected = list(deque(fh, maxlen=n))
        self.assertEqual(_tail_lines(f.name, n, block_size=block_size), expected)

    def test_matches_deque(self):
        lines = [f"2026-10-15 12:00:{i % 60:02d},000 - m.py:f:1 - INFO - line {i} é漢\n" for i in range(500)]
        body = "".join(lines).encode("utf-8")
        for n in (1, 7, 100, 499, 500, 1000):
            for block in (5, 64, 4096, 65536):
                self._check(body, n, block)

    def test_partial_last_line_and_crlf(self):
        self._check(b"a\r\nb\r\nc", 2, 3)
        self._check(b"a\nb\nc", 5, 2)
        self._check(b"only", 3, 2)

    def test_empty(self):
        self._check(b"", 10, 4)


if __name__ == "__main__":
    unittest.main()
