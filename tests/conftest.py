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



# C extensions (Cython, OpenSSL/cryptography bindings) register modules without a
# __spec__; names starting with '_' are private load targets that can't shadow anything.
_NOT_STUBS = ('_', 'cython_runtime', 'cryptography.', 'xml.parsers.expat.')


def _stub_modules():
    """Modules in sys.modules that were never really imported (types.ModuleType stand-ins)."""
    return {
        name for name, mod in list(sys.modules.items())
        if mod is not None and getattr(mod, '__spec__', None) is None
        and name not in sys.builtin_module_names and name != '__main__'
        and not name.startswith(_NOT_STUBS)
    }


_LEAKS = {}


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # Importing a test file happens while its Module collector runs; anything it leaves
    # as a stand-in in sys.modules replaces the real module for every other test.
    if not isinstance(collector, pytest.Module):
        yield
        return
    before = _stub_modules()
    attrs_before = _guarded_attr_ids()
    yield
    leaked = sorted(_stub_modules() - before) + _changed_attrs(attrs_before)
    if leaked:
        _LEAKS[collector.nodeid] = leaked


def pytest_collection_finish(session):
    if _LEAKS:
        lines = "\n".join(f"  {f}: {', '.join(m)}" for f, m in sorted(_LEAKS.items()))
        raise pytest.UsageError(
            "Test files left stub modules in sys.modules while being imported:\n" + lines
            + "\nLoad stubs with tests._isolation.isolated_modules (or monkeypatch) instead.")


# Attributes of real modules that tests commonly fake. Assigning a fake straight onto the
# real module (instead of mock.patch / monkeypatch) changes them for every later test.
_GUARDED_ATTRS = (
    ('utilities.settings', 'get_setting'),
    ('utilities.settings', 'load_config'),
    ('routes.api_tracker', 'api'),
)


def _guarded_attr_ids():
    ids = {}
    for mod_name, attr in _GUARDED_ATTRS:
        mod = sys.modules.get(mod_name)
        if mod is not None and getattr(mod, '__spec__', None) is not None and hasattr(mod, attr):
            ids[(mod_name, attr)] = (id(mod), id(getattr(mod, attr)))
    return ids


def _changed_attrs(before):
    after = _guarded_attr_ids()
    return sorted(f"{m}.{a}" for (m, a), ids in before.items() if (m, a) in after and after[(m, a)] != ids)


@pytest.fixture(autouse=True)
def _no_stub_modules_left_behind():
    # Same rules at run time: a test that swaps in a stand-in module, or a fake attribute on
    # a real one, has to put the original back (mock.patch / monkeypatch).
    before = _stub_modules()
    attrs_before = _guarded_attr_ids()
    yield
    leaked = sorted(_stub_modules() - before)
    if leaked:
        pytest.fail("test left stub modules in sys.modules: " + ", ".join(leaked)
                    + " (use mock.patch.dict(sys.modules) or monkeypatch)", pytrace=False)
    changed = _changed_attrs(attrs_before)
    if changed:
        pytest.fail("test replaced real module attributes and didn't restore them: " + ", ".join(changed)
                    + " (use mock.patch or monkeypatch)", pytrace=False)
