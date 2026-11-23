"""Regression tests for the 2026-11-23 Tools page review: admin checks on the
debug/Plex-label routes, Convert to Symlinks duplicate matching, Version
Propagator, Rclone scan base path, Run Task list, Verification Queue counts,
Duplicate Symlink Targets, Modify Symlink Base Paths, Fix IMDb ID, Get
Collected in Symlinked mode and per-queue Bulk Queue Actions."""

import ast
import os
from pathlib import Path
from unittest import mock

import pytest

import database  # noqa: F401  (app import order)

ROOT = Path(__file__).resolve().parents[2]


# ── Admin checks ─────────────────────────────────────────────────────────────

# Read-only and shown in the base layout to every user.
_OPEN_ROUTES = {'get_rate_limit_info'}


def _route_functions(path):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decos = [ast.unparse(d) for d in node.decorator_list]
        routes = [d for d in decos if '.route(' in d]
        if routes:
            yield node.name, decos, routes


@pytest.mark.parametrize('path', [ROOT / 'routes' / 'plex_labels_debug_routes.py',
                                  ROOT / 'routes' / 'metadata_routes.py',
                                  *sorted((ROOT / 'routes' / 'debug_routes').glob('*.py'))],
                         ids=lambda p: p.name)
def test_debug_routes_need_admin(path):
    missing = [name for name, decos, _ in _route_functions(path)
               if name not in _OPEN_ROUTES and 'admin_required' not in decos]
    assert not missing, f'{path.name}: routes without @admin_required: {missing}'


def test_fix_imdb_is_post_only():
    for name, _, routes in _route_functions(ROOT / 'routes' / 'plex_labels_debug_routes.py'):
        if name in ('fix_imdb_id', 'fix_imdb_id_by_tmdb'):
            assert "'GET'" not in routes[0], routes[0]


def _call(view, method='GET', **ctx):
    """Run a decorated view without the login/admin wrapper."""
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context(method=method, **ctx):
        rv = view.__wrapped__()
    resp, status = (rv if isinstance(rv, tuple) else (rv, rv.status_code))
    return resp.get_json(), status


def _insert(conn, **cols):
    cols.setdefault('state', 'Collected')
    cols.setdefault('version', '1080p')
    keys = ', '.join(cols)
    cur = conn.execute(f"INSERT INTO media_items ({keys}) VALUES ({', '.join('?' for _ in cols)})",
                       list(cols.values()))
    conn.commit()
    return cur.lastrowid


# ── Convert to Symlinks ──────────────────────────────────────────────────────

def test_convert_duplicate_check_matches_the_same_episode_only(app_db):
    from routes.debug_routes.symlink_convert import _has_same_item_copy
    conn = app_db()
    _insert(conn, title='Show', type='episode', imdb_id='tt1', season_number=1, episode_number=1)
    item_id = _insert(conn, title='Show', type='episode', imdb_id='tt1', season_number=1, episode_number=2)
    item = dict(conn.execute('SELECT * FROM media_items WHERE id = ?', (item_id,)).fetchone())
    # Another episode of the show is not a copy of this one.
    assert not _has_same_item_copy(conn.cursor(), item)
    _insert(conn, title='Show', type='episode', imdb_id='tt1', season_number=1, episode_number=2, state='Wanted')
    assert _has_same_item_copy(conn.cursor(), item)
    conn.close()


# ── Version Propagator ───────────────────────────────────────────────────────

def test_propagator_copies_metadata_and_handles_null_year(app_db):
    from routes.debug_routes.versions import _propagate
    conn = app_db()
    _insert(conn, title='Movie', type='movie', imdb_id='tt9', tmdb_id='9', year=None,
            genres='["anime"]', runtime=95, selected_folder='Kids', selected_folder_is_custom=1,
            content_source='Overseerr_1')
    assert _propagate(conn, '1080p', '2160p', 'all') == 1
    conn.commit()
    # A second run finds the copy (the old `year = ?` match never matched NULL).
    assert _propagate(conn, '1080p', '2160p', 'all') == 0
    row = conn.execute("SELECT * FROM media_items WHERE version = '2160p'").fetchone()
    assert row['state'] == 'Wanted'
    assert row['genres'] == '["anime"]'
    assert row['runtime'] == 95
    assert row['selected_folder'] == 'Kids'
    assert row['content_source'] == 'Overseerr_1'
    conn.close()


def test_propagator_media_type_filter(app_db):
    from routes.debug_routes.versions import _propagate
    conn = app_db()
    _insert(conn, title='Movie', type='movie', imdb_id='tt1', year=2020)
    _insert(conn, title='Show', type='episode', imdb_id='tt2', season_number=1, episode_number=1)
    assert _propagate(conn, '1080p', '2160p', 'episode') == 1
    conn.close()


# ── Rclone scan base path ────────────────────────────────────────────────────

def test_rclone_scan_no_longer_writes_the_symlink_setting():
    src = (ROOT / 'routes' / 'debug_routes' / 'rclone_scan.py').read_text()
    assert 'set_setting(' not in src
    assert 'symlink_root=symlink_base_path_str' in src


def test_get_symlink_path_uses_symlink_root(app_settings):
    from utilities.local_library_scan import get_symlink_path
    app_settings['File Management'] = {'symlinked_files_path': '/configured',
                                       'symlink_organize_by_type': True,
                                       'file_collection_management': 'Symlinked/Local'}
    item = {'title': 'Movie', 'year': 2020, 'type': 'movie', 'version': '1080p', 'imdb_id': 'tt1'}
    with mock.patch('utilities.local_library_scan.os.makedirs'):
        default = get_symlink_path(dict(item), 'Movie.2020.1080p.mkv', skip_jikan_lookup=True)
        override = get_symlink_path(dict(item), 'Movie.2020.1080p.mkv', skip_jikan_lookup=True,
                                    symlink_root='/elsewhere')
    assert default.startswith('/configured')
    assert override.startswith('/elsewhere')


# ── Run Task list ────────────────────────────────────────────────────────────

def test_run_task_list_comes_from_registry():
    from routes.debug_routes import tasks
    from queues.runner.task_registry import SPECS

    class FakeRunner:
        queue_processing_map = {'Wanted': 'process_wanted'}

        def get_content_sources(self):
            return {'Overseerr_1': {'enabled': True, 'display_name': 'Requests'}}

    runner = FakeRunner()
    for name in SPECS:
        if name.startswith('task_'):
            setattr(runner, name, lambda: None)
    with mock.patch.object(tasks, 'get_program_runner', return_value=runner):
        data, status = _call(tasks.get_available_tasks)
    ids = {t['id'] for t in data['task_map']}
    assert status == 200
    assert 'task_analyze_library' not in ids and 'task_process_pending_rclone_paths' not in ids
    assert {n for n in SPECS if n.startswith('task_')} <= ids
    assert 'Wanted' in ids and 'Scraping' not in ids  # only queues the runner can run
    assert 'task_Overseerr_1_wanted' in ids


# ── Verification Queue ───────────────────────────────────────────────────────

def test_verification_keeps_real_unverified_count_and_handles_missing_season():
    from routes.debug_routes import verification
    files = [{'type': 'episode', 'title': 'Show', 'season_number': None, 'episode_number': 3,
              'episode_title': None, 'verification_id': 1, 'filename': 'a.mkv', 'full_path': '/a.mkv',
              'media_item_id': 5, 'added_at': None, 'verification_attempts': 0, 'last_attempt': None}]
    with mock.patch.object(verification, 'get_unverified_files', return_value=files), \
            mock.patch.object(verification, 'get_verification_stats',
                              return_value={'total': 9000, 'verified': 0, 'unverified': 9000,
                                            'percent_verified': 0}):
        data, _ = _call(verification.get_verification_queue)
    assert data['success']
    assert data['stats']['unverified'] == 9000
    assert data['stats']['shown'] == 1
    assert data['files'][0]['title'] == 'Show - S?E3'


# ── Duplicate Symlink Targets ────────────────────────────────────────────────

def test_duplicate_scan_finds_links_outside_tv_shows(app_db, app_settings, tmp_path):
    from routes.debug_routes import duplicates
    root = tmp_path / 'links'
    target = tmp_path / 'src' / 'pack.mkv'
    target.parent.mkdir()
    target.write_text('x')
    paths = []
    for ep in (1, 2):  # version-first layout, not under "TV Shows"
        p = root / '1080p' / 'Documentary TV Shows' / 'Show' / f'S01E0{ep}.mkv'
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        paths.append(p)
    # The same episode in a second version folder pointing at the same file is fine.
    same = root / '2160p' / 'Other' / 'S01E05.mkv'
    same.parent.mkdir(parents=True)
    os.symlink(tmp_path / 'src' / 'pack.mkv', same)
    other = tmp_path / 'src' / 'other.mkv'
    other.write_text('y')
    v1 = root / '1080p' / 'Other' / 'S01E05.mkv'
    v1.parent.mkdir(parents=True)
    os.symlink(other, v1)
    v2 = root / '2160p' / 'Other2' / 'S01E05.mkv'
    v2.parent.mkdir(parents=True)
    os.symlink(other, v2)

    app_settings['File Management'] = {'symlinked_files_path': str(root)}
    conn = app_db()
    for ep, p in zip((1, 2), paths):
        _insert(conn, title='Show', type='episode', season_number=1, episode_number=ep, location_on_disk=str(p))
    _insert(conn, title='Other', type='episode', season_number=1, episode_number=5, location_on_disk=str(v1))
    _insert(conn, title='Other', type='episode', season_number=1, episode_number=5, location_on_disk=str(v2),
            version='2160p')
    conn.close()
    data, _ = _call(duplicates.scan_duplicate_symlinks, method='POST')
    assert data['success'], data
    assert data['group_count'] == 1
    assert {i['episode_number'] for i in data['groups'][0]['items']} == {1, 2}


# ── Modify Symlink Base Paths ────────────────────────────────────────────────

def test_modify_paths_matches_whole_folders(app_db):
    from routes.debug_routes import symlink_recovery
    conn = app_db()
    a = _insert(conn, title='A', type='movie', original_path_for_symlink='/mnt/zurg/A.mkv')
    b = _insert(conn, title='B', type='movie', original_path_for_symlink='/mnt/zurg_old/B.mkv')
    conn.close()
    data, _ = _call(symlink_recovery.modify_symlink_paths, method='POST',
                    data={'current_original_base': '/mnt/zurg/', 'new_original_base': '/mnt/rclone'})
    assert data['success'] and data['updated_count'] == 1
    conn = app_db()
    rows = {r['id']: r['original_path_for_symlink'] for r in conn.execute('SELECT id, original_path_for_symlink FROM media_items')}
    conn.close()
    assert rows[a] == '/mnt/rclone/A.mkv'
    assert rows[b] == '/mnt/zurg_old/B.mkv'


# ── Fix IMDb ID ──────────────────────────────────────────────────────────────

def _call_args(view, method, **view_args):
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context(method=method):
        rv = view.__wrapped__(**view_args)
    resp, status = (rv if isinstance(rv, tuple) else (rv, rv.status_code))
    return resp.get_json(), status


def test_fix_imdb_by_item_updates_whole_show(app_db):
    from routes import plex_labels_debug_routes as pl
    conn = app_db()
    e1 = _insert(conn, title='Show', type='episode', tmdb_id='77', imdb_id='ttbad', season_number=1, episode_number=1)
    _insert(conn, title='Show', type='episode', tmdb_id='77', imdb_id='ttbad', season_number=1, episode_number=2)
    movie = _insert(conn, title='Film', type='movie', tmdb_id='77', imdb_id='ttfilm')
    conn.close()
    data, status = _call_args(pl.fix_imdb_id, 'POST', item_id=e1, imdb_id='tt123')
    assert status == 200 and data['updated_count'] == 2
    conn = app_db()
    assert conn.execute('SELECT imdb_id FROM media_items WHERE id = ?', (movie,)).fetchone()[0] == 'ttfilm'
    conn.close()
    data, status = _call_args(pl.fix_imdb_id, 'POST', item_id=e1, imdb_id='nope')
    assert status == 400


def test_fix_imdb_by_tmdb_refuses_movie_and_show_on_one_id(app_db):
    from routes import plex_labels_debug_routes as pl
    conn = app_db()
    _insert(conn, title='Show', type='episode', tmdb_id='77', season_number=1, episode_number=1)
    _insert(conn, title='Film', type='movie', tmdb_id='77')
    conn.close()
    data, status = _call_args(pl.fix_imdb_id_by_tmdb, 'POST', tmdb_id='77', imdb_id='tt1')
    assert status == 409


# ── Get Collected / Bulk Queue ───────────────────────────────────────────────

def test_get_collected_all_reports_error_in_symlink_mode(app_settings):
    from routes.debug_routes import plex_scan
    app_settings['File Management'] = {'file_collection_management': 'Symlinked/Local'}
    for kind in ('all', 'recent'):
        result = plex_scan.async_get_collected_from_plex(kind)
        assert result['success'] is False


def test_bulk_queue_contents_returns_one_queue(app_db):
    from routes.debug_routes import maintenance
    conn = app_db()
    _insert(conn, title='W', type='movie', state='Wanted', year=2020)
    _insert(conn, title='S', type='movie', state='Sleeping', year=2020)
    conn.close()
    data, _ = _call(maintenance.get_queue_contents, query_string={'queue': 'Wanted'})
    assert [i['title'] for i in data['Wanted']] == ['W (2020)']
    assert data['Sleeping'] == []


# ── Indexer text is escaped ──────────────────────────────────────────────────

@pytest.mark.parametrize('path,raw', [
    ('static/js/scraper_tester.js', "filtered-title' : ''}\">${title}</div>"),
    ('static/js/scraper.js', "<div class=\"card-filename\">${torrent.title"),
    ('static/js/scraper.js', "${torrent.filter_reason}</div>"),
    ('static/js/scraper.js', "<span class=\"source-badge\">${p.trim()}</span>"),
    ('templates/reverse_parser.html', 'data-full-content="${cellContent}">${cellContent}'),
    ('static/js/pages/database-1.js', "String(rawCellContent).replace(/\"/g, '&quot;')"),
])
def test_indexer_text_is_not_inserted_raw(path, raw):
    assert raw not in (ROOT / path).read_text()


# ── Upgrade cleanup keeps shared packs ───────────────────────────────────────

def test_upgrade_cleanup_keeps_a_pack_other_episodes_use(app_db):
    from database import collected_items as ci
    conn = app_db()
    _insert(conn, title='Show', type='episode', season_number=1, episode_number=2,
            filled_by_torrent_id='PACK1')
    conn.close()
    with mock.patch('queues.adding_queue.AddingQueue') as aq:
        assert ci.remove_original_item_from_account({'title': 'Show', 'filled_by_torrent_id': 'PACK1'}) is False
        aq.assert_not_called()
        assert ci.remove_original_item_from_account({'title': 'Show', 'filled_by_torrent_id': 'OTHER'}) is True
        aq.return_value.remove_unwanted_torrent.assert_called_once_with('OTHER')


def test_upgrade_cleanup_checks_nzb_ids_both_ways(app_db):
    from database import collected_items as ci
    conn = app_db()
    _insert(conn, title='Show', type='episode', season_number=1, episode_number=2,
            filled_by_torrent_id='nzb:abc')
    conn.close()
    assert ci._old_release_still_used('abc')
    assert ci._old_release_still_used('nzb:abc')
    assert not ci._old_release_still_used('nzb:zzz')


def test_clear_hub_queue_keeps_magnets_of_items_in_flight(app_db):
    from routes import upgrade_hub_routes as uh
    from database import zilean_upgrade as zu
    zu._ensure_cache_tables()
    conn = app_db()
    waiting = _insert(conn, title='A', type='movie', state='Upgrading')
    adding = _insert(conn, title='B', type='movie', state='Adding')
    for iid in (waiting, adding):
        conn.execute("INSERT INTO upgrade_hub_queued_magnets VALUES (?, '{}', 'now')", (iid,))
    conn.commit()
    conn.close()
    data, _ = _call(uh.clear_hub_queue, method='POST')
    assert data['reverted'] == 1
    conn = app_db()
    left = [r[0] for r in conn.execute('SELECT item_id FROM upgrade_hub_queued_magnets')]
    state = conn.execute('SELECT state FROM media_items WHERE id = ?', (waiting,)).fetchone()[0]
    conn.close()
    assert left == [adding]
    assert state == 'Collected'


# ── Usenet transfer ──────────────────────────────────────────────────────────

def test_transfer_directions():
    from utilities.provider_transfer import parse_direction
    assert parse_direction('climount_to_nzbdav') == ('climount', 'nzbdav')
    assert parse_direction('nzbdav_to_builtin') == ('nzbdav', 'builtin')
    assert parse_direction('builtin_to_climount') == ('builtin', 'climount')
    for bad in ('nzbdav_to_nzbdav', 'zurg_to_climount', 'x', ''):
        assert parse_direction(bad) == (None, None)


def _resp(payload, status=200):
    r = mock.MagicMock(status_code=status)
    r.json.return_value = payload
    return r


def test_transfer_climount_source_uses_release_names(tmp_path):
    from utilities import provider_transfer as pt
    (tmp_path / 'id-1.nzb').write_bytes(b'<nzb/>')
    (tmp_path / 'id-2.nzb').write_bytes(b'<nzb><head><meta type="name">From.Meta.mkv</meta></head></nzb>')
    (tmp_path / 'id-3.nzb').write_bytes(b'<nzb/>')
    page = {'entries': [{'name': 'Show.S01E01.1080p', 'info_hash': 'id-1'}], 'total_pages': 1}
    with mock.patch.object(pt.requests, 'get', return_value=_resp(page)):
        items, err = pt.list_source_items('climount_to_nzbdav', 'http://cm', 'tok', str(tmp_path))
    assert err is None
    assert [i['name'] for i in items] == ['Show.S01E01.1080p', 'From.Meta.mkv', 'id-3']


def test_transfer_climount_target_existing_names_from_library():
    from utilities import provider_transfer as pt
    pages = [_resp({'entries': [{'name': 'A'}], 'total_pages': 2}),
             _resp({'entries': [{'name': 'B'}], 'total_pages': 2})]
    with mock.patch.object(pt.requests, 'get', side_effect=pages) as get:
        names = pt._target_existing_names('climount', 'http://cm', '')
    assert names == {'A', 'B'}
    assert get.call_args_list[0].args[0] == 'http://cm/api/browse/__all__'


def test_transfer_zurg_source_explains_it_has_no_nzbs():
    from utilities import provider_transfer as pt
    hist = {'history': {'slots': [{'name': 'x', 'status': 'Completed'}]}}
    with mock.patch.object(pt.requests, 'get', return_value=_resp(hist)):
        items, err = pt.list_source_items('nzbdav_to_climount', 'http://zurg', '')
    assert items is None and 'target' in err


def test_transfer_runs_one_job_at_a_time_and_sends_to_builtin():
    from utilities import provider_transfer as pt
    pt._jobs.clear()
    pt._jobs['busy'] = {'complete': False}
    job, err = pt.start_job({'direction': 'nzbdav_to_builtin'})
    assert job is None and 'already running' in err
    assert pt.active_job_id() == 'busy'
    pt._jobs.clear()

    client = mock.MagicMock()
    client.add_nzb_content.return_value = 'eng-1'
    client._usenet_jobs.return_value = []
    items = [{'name': 'Film.2020', 'ref': 'b', 'category': 'movies', 'status': ''}]
    with mock.patch.object(pt, '_builtin_client', return_value=client), \
            mock.patch.object(pt, 'list_source_items', return_value=(items, None)), \
            mock.patch.object(pt, '_read_nzb', return_value=(b'<nzb/>', None)):
        pt._jobs['j'] = {'status': 'starting', 'total': 0, 'done': 0, 'submitted': 0,
                         'skipped': 0, 'failed': 0, 'complete': False, 'log': [], 'error': None}
        pt._run_job('j', {'direction': 'nzbdav_to_builtin'})
    client.add_nzb_content.assert_called_once_with(b'<nzb/>', title='Film.2020', category='movies')
    assert pt._jobs['j']['submitted'] == 1 and pt._jobs['j']['complete']
    pt._jobs.clear()


# ── Create a Symlink ─────────────────────────────────────────────────────────

def test_symlink_tool_rows_carry_the_source_path(app_db):
    from routes import symlink_tools_routes as st
    item = {'title': 'Film', 'year': 2020, 'type': 'movie', 'version': '1080p', 'content_source': '',
            'imdb_id': 'tt5', 'tmdb_id': '5', 'filled_by_file': 'Film.mkv',
            'original_path_for_symlink': '/mnt/src/Film.2020.mkv'}
    row_id = st.add_to_database(item, '/links/Movies/Film (2020)/Film.mkv')
    conn = app_db()
    row = conn.execute('SELECT * FROM media_items WHERE id = ?', (row_id,)).fetchone()
    conn.close()
    assert row['original_path_for_symlink'] == '/mnt/src/Film.2020.mkv'
    assert row['filled_by_file'] == 'Film.2020.mkv'
    assert row['state'] == 'Collected' and row['collected_at']


def test_symlink_tool_browse_rejects_dotdot(app_settings):
    from routes import symlink_tools_routes as st
    app_settings['File Management'] = {'original_files_path': '', 'symlinked_files_path': ''}
    data, _ = _call(st.browse_files, method='POST', json={'path': '/mnt/../etc'})
    assert data['success'] is False and 'denied' in data['message']


# ── Trakt friends / Plex user tokens ─────────────────────────────────────────

def test_trakt_friend_delete_reloads_sources(tmp_path, monkeypatch):
    from routes import trakt_friends_routes as tf
    monkeypatch.setattr(tf, 'TRAKT_FRIENDS_DIR', str(tmp_path))
    (tmp_path / 'abc.json').write_text('{}')
    config = {'Content Sources': {'Friends Trakt Watchlist_1': {'auth_id': 'abc'}}}
    with mock.patch.object(tf, 'load_config', return_value=config), \
            mock.patch.object(tf, 'save_config'), \
            mock.patch('routes.settings_routes._reload_components_after_settings_change') as reload_:
        from flask import Flask
        with Flask(__name__).test_request_context(method='POST'):
            tf.delete_friend.__wrapped__('abc')
    reload_.assert_called_once_with('content_source')
    assert config['Content Sources'] == {}


def test_user_tokens_update_is_private_and_atomic(tmp_path, monkeypatch):
    from flask import Flask
    from routes import user_token_routes as ut
    monkeypatch.setattr(ut, 'get_config_dir', lambda: str(tmp_path))
    with Flask(__name__).app_context():
        ut.update_user_tokens(lambda t: t.__setitem__('alice', 'A'))
        ut.update_user_tokens(lambda t: t.__setitem__('bob', 'B'))
        assert ut.load_user_tokens() == {'alice': 'A', 'bob': 'B'}
        assert ut.update_user_tokens(lambda t: t.pop('alice', None) is not None) is True
        assert ut.load_user_tokens() == {'bob': 'B'}
    if os.name == 'posix':
        assert (tmp_path / ut.USER_TOKEN_FILE_NAME).stat().st_mode & 0o077 == 0
