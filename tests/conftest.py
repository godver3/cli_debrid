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

# No battery refresh worker: a test reaching DirectAPI would start it, and 30 s
# later it calls Trakt/TVDB in the middle of whatever test runs then
os.environ.setdefault("CLI_DEBRID_NO_BACKGROUND_WORKERS", "1")


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


# --- Shared fixtures ---------------------------------------------------------

@pytest.fixture(scope='session')
def _schema_template(tmp_path_factory):
    """A media_items.db with the real schema, built once per run and copied per test."""
    path = tmp_path_factory.mktemp('schema_template')
    old = os.environ.get('USER_DB_CONTENT')
    os.environ['USER_DB_CONTENT'] = str(path)
    try:
        import database  # noqa: F401  (the app imports the package before anything else)
        from database.schema_management import verify_database
        from database.core import get_db_connection
        verify_database()
        conn = get_db_connection()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
    finally:
        os.environ['USER_DB_CONTENT'] = old
    return path / 'media_items.db'


@pytest.fixture
def app_db(_schema_template, tmp_path, monkeypatch):
    """A fresh media_items database with the real schema for this test.

    Returns database.core.get_db_connection; every app function that opens the
    database without a path uses this one too, because USER_DB_CONTENT points here.
    """
    import shutil
    shutil.copy(_schema_template, tmp_path / 'media_items.db')
    monkeypatch.setenv('USER_DB_CONTENT', str(tmp_path))
    from database.core import get_db_connection
    return get_db_connection


@pytest.fixture
def app_settings():
    """The config get_setting() and load_config() return for this test, as a plain dict.

    Fill it in the test (app_settings['Plex Reported Issues'] = {'enabled': True}).
    Both read through utilities.settings._get_cached_entry, so this also reaches
    modules that imported get_setting/load_config by name.
    """
    from unittest import mock
    config = {}
    with mock.patch('utilities.settings._get_cached_entry', side_effect=lambda: (config, None)):
        yield config


def pytest_collection_modifyitems(items):
    # Mark each test with its folder so one area can be run on its own (pytest -m usenet).
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    for item in items:
        rel = os.path.relpath(str(item.fspath), tests_dir)
        area = rel.split(os.sep)[0] if os.sep in rel else None
        if area:
            item.add_marker(getattr(pytest.mark, area))
