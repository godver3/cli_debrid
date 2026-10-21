"""Shared pytest setup.

Many modules create /user/... directories at import time, so point the
USER_* locations at a throwaway directory before any test module imports
application code. Explicitly set env vars still win.
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="cli_debrid_tests_")
for _var, _sub in (("USER_CONFIG", "config"), ("USER_DB_CONTENT", "db_content"), ("USER_LOGS", "logs")):
    if not os.environ.get(_var):
        _path = os.path.join(_TMP, _sub)
        os.makedirs(_path, exist_ok=True)
        os.environ[_var] = _path
