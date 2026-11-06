# cli_debrid tests

## Running them

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-linux.txt pytest pyflakes ruff
PYTHON=.venv/bin/python scripts/check.sh          # everything: tests, pyflakes, ruff, JS, cli_mount
PYTHON=.venv/bin/python scripts/check.sh --quick  # skip the slow tests
```

`check.sh` points `USER_CONFIG`, `USER_DB_CONTENT` and `USER_LOGS` at a throwaway folder, so tests never touch `/user`. To run pytest yourself, do the same (or let `conftest.py` pick a temp folder):

```bash
.venv/bin/python -m pytest                     # all (~1 minute)
.venv/bin/python -m pytest -m "not slow"       # skip tests that start a fresh interpreter
.venv/bin/python -m pytest -m usenet           # one area
.venv/bin/python -m pytest tests/plex/test_plex_reported_issues.py -k cap
```

## Layout

Tests are grouped by the part of the app they cover, and each test is automatically marked with its folder name:

| Folder | Covers |
|---|---|
| `plex/` | Plex, Jellyfin, Bazarr: libraries, watchlists, scans, Reported Issues |
| `content_sources/` | Overseerr, Trakt, Scrob, Discover, content-source runs |
| `usenet/` | NZB submission and health, cli_mount, nzbdav, Zurg, playback repair |
| `debrid/` | Debrid packs, cache checks, provider routing |
| `scraping/` | Result filtering, anime and episode-order matching, release parsing |
| `matching/` | Manual assign, magnet assign, the media matcher, match overrides |
| `queues/` | Queue states, sleeping and wake limits, the scheduler, task toggles |
| `metadata/` | Metadata refresh, release dates, TMDB/TVDB |
| `core/` | Settings, database, logging, stats, onboarding, Task Manager info, the UI |

## Writing a test

- **A database with the real schema:** take the `app_db` fixture. It copies a schema template into a temp folder and points `USER_DB_CONTENT` at it, so app code that opens the database without a path uses it too. It returns `get_db_connection`.
- **Settings:** take the `app_settings` fixture and fill the dict it gives you (`app_settings['Plex'] = {'url': ...}`). `get_setting` and `load_config` read it, including in modules that imported them by name.
- **Faking an app module:** use `monkeypatch` / `mock.patch`, or `mock.patch.dict(sys.modules)` to swap whole modules. To load a module under test against stand-ins at import time, decorate the loader with `tests._isolation.isolated_modules`.
- **Don't** put stand-ins into `sys.modules` (or assign fakes onto real modules) without restoring them. `conftest.py` fails the run when a test file does it while being imported, and fails the test when it happens at run time: a leaked stub replaces the real module for every later test, so results start depending on order.
- Import the `database` package before queues or routes when a test needs the real app (`main.py` does the same); importing `queues.scraping_queue` first runs into the app's import cycles.
- A test that has to start a fresh interpreter (e.g. to check import order) gets `pytestmark = pytest.mark.slow`.
