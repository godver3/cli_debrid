"""Call-count regression tests for batched multi-item deletion (500 episodes / 5 seasons).

Verifies that bulk deletion avoids per-episode Plex/Silo/Jellyfin round trips,
per-episode sleeps, per-episode DB commits, and per-episode os.walk() NFO scans,
while preserving NFO/subtitle ownership guards and Direct Mount support.
"""

import os
from unittest import mock

import pytest

import database  # noqa: F401 - initialize database before plex_functions to avoid circular import
from utilities import nfo_writer as nw
from utilities.deletion_manager import DeletionManager
from utilities.plex_functions import scan_cleanup as _scan_cleanup  # noqa: F401


def _make_manager(*, using_symlinks=True, symlink_dir='', plex_enabled=True,
                  silo_enabled=False, jellyfin_direct_enabled=False):
    """Build a DeletionManager without touching live settings or disk probing."""
    dm = DeletionManager.__new__(DeletionManager)
    dm.debrid = None
    dm.deletion_log = []
    dm.file_collection_mode = 'Symlinked/Local' if using_symlinks else 'Plex'
    dm.symlink_dir = symlink_dir
    dm.original_files_path = ''
    dm.using_symlinks = using_symlinks
    dm.media_server = 'silo' if silo_enabled else 'jellyfin' if jellyfin_direct_enabled else 'plex'
    dm.mounted_location = '/mnt/debrid'
    dm.silo_enabled = silo_enabled
    dm.jellyfin_direct_enabled = jellyfin_direct_enabled
    dm.plex_enabled = plex_enabled
    dm.jellyfin_enabled = False
    dm.limited_env = False
    dm.db_content_dir = '/tmp'
    return dm


def _build_500_ep_show(tmp_path, *, create_files=True):
    """Create 500 episode dicts across 5 seasons (100 eps/season), optionally on disk."""
    symlink_root = tmp_path / 'symlinks'
    show_dir = symlink_root / 'TV Shows' / 'Big Show (2020)'
    items = []
    season_dirs = []
    item_id = 1
    for s in range(1, 6):
        s_dir = show_dir / f'Season {s:02d}'
        season_dirs.append(str(s_dir))
        if create_files:
            s_dir.mkdir(parents=True, exist_ok=True)
        for e in range(1, 101):
            ep_path = s_dir / f'Big Show (2020) - S{s:02d}E{e:03d} - Episode {e}.mkv'
            if create_files:
                os.symlink(f'/nonexistent/mount/s{s:02d}e{e:03d}.mkv', ep_path)
                (s_dir / f'Big Show (2020) - S{s:02d}E{e:03d} - Episode {e}.nfo').write_text(
                    f'<?xml version="1.0"?>\n{nw.MARKER}\n<episodedetails/>\n', encoding='utf-8'
                )
                (s_dir / f'Big Show (2020) - S{s:02d}E{e:03d} - Episode {e}.en.srt').write_text(
                    '1\n00:00:01,000 --> 00:00:02,000\nhi\n', encoding='utf-8'
                )
            items.append({
                'id': item_id,
                'type': 'episode',
                'title': 'Big Show',
                'episode_title': f'Episode {e}',
                'season_number': s,
                'episode_number': e,
                'state': 'Collected',
                'imdb_id': 'tt9999999',
                'location_on_disk': str(ep_path),
                'filled_by_file': str(ep_path),
            })
            item_id += 1
    if create_files:
        (show_dir / nw.SHOW_NFO).write_text(
            f'<?xml version="1.0"?>\n{nw.MARKER}\n<tvshow/>\n', encoding='utf-8'
        )
    return symlink_root, show_dir, season_dirs, items


def _fake_plex_server(location):
    section = mock.MagicMock()
    section.key = '1'
    section.type = 'show'
    section.title = 'TV Shows'
    section.locations = [str(location)]
    plex = mock.MagicMock()
    plex.library.sections.return_value = [section]
    return plex, section


def test_symlink_500_episodes_5_seasons_batches_plex_sleep_db_and_nfo(tmp_path):
    symlink_root, show_dir, season_dirs, items = _build_500_ep_show(tmp_path, create_files=True)
    dm = _make_manager(using_symlinks=True, symlink_dir=str(symlink_root), plex_enabled=True)
    item_ids = [it['id'] for it in items]

    plex_instance, tv_section = _fake_plex_server(symlink_root)
    real_walk = os.walk
    nfo_walk_calls = []

    def counting_walk(path, *args, **kwargs):
        nfo_walk_calls.append(path)
        return real_walk(path, *args, **kwargs)

    def fake_get_setting(section, key, default=None):
        table = {
            ('File Management', 'file_collection_management'): 'Symlinked/Local',
            ('File Management', 'symlinked_files_path'): str(symlink_root),
            ('File Management', 'plex_url_for_symlink'): 'http://plex:32400',
            ('File Management', 'plex_token_for_symlink'): 'tok',
            ('Debug', 'emby_jellyfin_url'): '',
            ('Debug', 'emby_jellyfin_token'): '',
        }
        return table.get((section, key), default)

    clyde_subs = {os.path.normpath(os.path.splitext(it['location_on_disk'])[0] + '.en.srt') for it in items}
    with mock.patch('utilities.deletion_manager.get_setting', side_effect=fake_get_setting), \
         mock.patch('database.subtitle_history.is_clyde_path', side_effect=lambda p, saved_via='sidecar': os.path.normpath(p) in clyde_subs), \
         mock.patch('utilities.plex_functions.scan_cleanup.get_setting', side_effect=fake_get_setting), \
         mock.patch('utilities.silo_functions.is_silo_selected', return_value=False), \
         mock.patch('utilities.direct_mount.is_direct_mount_jellyfin', return_value=False), \
         mock.patch('utilities.direct_mount.unconfigured_server', return_value=None), \
         mock.patch.object(nw, 'symlinked_mode', return_value=True), \
         mock.patch.object(nw, 'library_root', return_value=str(symlink_root)), \
         mock.patch.object(nw.os, 'walk', side_effect=counting_walk), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items) as mock_get_items, \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 500, 'error': None}) as mock_db_delete, \
         mock.patch('plexapi.server.PlexServer', return_value=plex_instance) as mock_plex_cls, \
         mock.patch('utilities.plex_functions.remove_file_from_plex') as mock_remove_file, \
         mock.patch('utilities.deletion_manager.time.sleep') as mock_dm_sleep, \
         mock.patch('utilities.plex_functions.scan_cleanup.time.sleep') as mock_scan_sleep:

        result = dm.delete_multiple_items(
            item_ids=item_ids,
            delete_from_media_server=True,
            delete_symlinks=True,
            delete_from_debrid=False,
            clear_cache=False,
            remove_from_content_source=False,
        )

    assert result['success'] is True
    assert result['deleted_count'] == 500
    assert result['failed_count'] == 0

    # Call-count assertions: O(seasons), never O(episodes)
    assert mock_plex_cls.call_count == 1
    assert mock_remove_file.call_count == 0
    # The 5 season folders are gone after the delete, and a Plex partial scan of
    # a removed folder does nothing: one scan of their nearest existing folder.
    from utilities.plex_functions.scan_cleanup import _nearest_existing_scan_paths
    expected = _nearest_existing_scan_paths(season_dirs, [tv_section])
    scanned = [c.kwargs['path'] for c in tv_section.update.call_args_list]
    assert scanned == expected and 1 <= len(expected) <= 5
    assert tv_section.emptyTrash.call_count == 1
    assert (mock_dm_sleep.call_count + mock_scan_sleep.call_count) <= 1
    assert mock_db_delete.call_count == 1
    assert mock_get_items.call_count == 1
    # tvshow.nfo emptiness check ran ONCE for the single show folder, not 500 times
    assert len(nfo_walk_calls) == 1

    # Filesystem: all 500 symlinks, 500 episode NFOs, 500 subtitles, tvshow.nfo, and empty folders gone
    assert not show_dir.exists()


def test_symlink_500_episodes_mocked_scan_and_empty_plex_trash(tmp_path):
    symlink_root, _show_dir, season_dirs, items = _build_500_ep_show(tmp_path, create_files=True)
    dm = _make_manager(using_symlinks=True, symlink_dir=str(symlink_root), plex_enabled=True)

    def fake_get_setting(section, key, default=None):
        if (section, key) == ('File Management', 'symlinked_files_path'):
            return str(symlink_root)
        return default

    with mock.patch('utilities.deletion_manager.get_setting', side_effect=fake_get_setting), \
         mock.patch.object(nw, 'symlinked_mode', return_value=True), \
         mock.patch.object(nw, 'library_root', return_value=str(symlink_root)), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 500, 'error': None}), \
         mock.patch('utilities.plex_functions.remove_file_from_plex') as mock_remove_file, \
         mock.patch('utilities.plex_functions.scan_and_empty_plex_trash',
                    return_value={'success': True, 'paths_scanned': season_dirs,
                                  'sections_cleaned': ['TV Shows'], 'errors': []}) as mock_scan_trash, \
         mock.patch('utilities.deletion_manager.time.sleep') as mock_sleep:

        result = dm.delete_multiple_items(
            item_ids=[it['id'] for it in items],
            delete_from_media_server=True,
            delete_symlinks=True,
            clear_cache=False,
        )

    assert result['success'] is True
    assert mock_remove_file.call_count == 0
    assert mock_sleep.call_count == 0
    assert mock_scan_trash.call_count == 1
    assert mock_scan_trash.call_args.kwargs['paths'] == season_dirs
    assert mock_scan_trash.call_args.kwargs['section_type'] == 'show'


def test_nfo_and_subtitle_guards_preserved_in_batch(tmp_path):
    """Same-stem video (.mp4 alongside .mkv), foreign NFO, and longer-stem subtitles stay."""
    symlink_root = tmp_path / 'symlinks'
    show_dir = symlink_root / 'TV Shows' / 'Guard Show (2021)'
    s1 = show_dir / 'Season 01'
    s1.mkdir(parents=True)

    # Episode 1 has both .mkv (deleted) and .mp4 (kept) -> shared .nfo must stay
    ep1_mkv = s1 / 'Guard Show - S01E01.mkv'
    ep1_mp4 = s1 / 'Guard Show - S01E01.mp4'
    os.symlink('/nonexistent/ep1.mkv', ep1_mkv)
    os.symlink('/nonexistent/ep1.mp4', ep1_mp4)
    ep1_nfo = s1 / 'Guard Show - S01E01.nfo'
    ep1_nfo.write_text(f'<?xml version="1.0"?>\n{nw.MARKER}\n<episodedetails/>\n', encoding='utf-8')

    # Episode 2 has a foreign NFO (no our marker) -> must stay
    ep2_mkv = s1 / 'Guard Show - S01E02.mkv'
    os.symlink('/nonexistent/ep2.mkv', ep2_mkv)
    ep2_nfo = s1 / 'Guard Show - S01E02.nfo'
    ep2_nfo.write_text('<episodedetails><title>user owned</title></episodedetails>\n', encoding='utf-8')

    # Episode 2 also has a longer-named REMUX sibling -> REMUX's subtitle must stay, ep2's goes
    ep2_remux = s1 / 'Guard Show - S01E02.REMUX.mkv'
    os.symlink('/nonexistent/ep2_remux.mkv', ep2_remux)
    ep2_sub = s1 / 'Guard Show - S01E02.en.srt'
    ep2_remux_sub = s1 / 'Guard Show - S01E02.REMUX.en.srt'
    ep2_sub.write_text('sub', encoding='utf-8')
    ep2_remux_sub.write_text('remux sub', encoding='utf-8')

    # Show tvshow.nfo -> must stay because ep1_mp4 and ep2_remux remain in the show folder
    show_nfo = show_dir / nw.SHOW_NFO
    show_nfo.write_text(f'<?xml version="1.0"?>\n{nw.MARKER}\n<tvshow/>\n', encoding='utf-8')

    dm = _make_manager(using_symlinks=True, symlink_dir=str(symlink_root), plex_enabled=False)
    items = [
        {'id': 1, 'type': 'episode', 'title': 'Guard Show', 'state': 'Collected', 'location_on_disk': str(ep1_mkv)},
        {'id': 2, 'type': 'episode', 'title': 'Guard Show', 'state': 'Collected', 'location_on_disk': str(ep2_mkv)},
    ]

    with mock.patch('utilities.deletion_manager.get_setting',
                    side_effect=lambda s, k, d=None: str(symlink_root) if k == 'symlinked_files_path' else d), \
         mock.patch('database.subtitle_history.is_clyde_path',
                    side_effect=lambda p, saved_via='sidecar': os.path.normpath(p) in {
                        os.path.normpath(str(ep2_sub)), os.path.normpath(str(ep2_remux_sub))}), \
         mock.patch.object(nw, 'symlinked_mode', return_value=True), \
         mock.patch.object(nw, 'library_root', return_value=str(symlink_root)), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 2, 'error': None}):
        res = dm.delete_multiple_items([1, 2], delete_from_media_server=False, clear_cache=False)

    assert res['success'] is True
    assert not os.path.lexists(ep1_mkv)
    assert not os.path.lexists(ep2_mkv)
    assert not ep2_sub.exists()
    # Guards preserved:
    assert ep1_nfo.exists()
    assert ep2_nfo.exists()
    assert ep2_remux_sub.exists()
    assert show_nfo.exists()


@pytest.mark.parametrize('server_kind', ['plex', 'plex_api_fails', 'silo', 'jellyfin'])
def test_direct_mount_500_episodes_batches_media_server_calls(tmp_path, server_kind):
    # Direct Mount + Plex without a show/season/movie delete: each item goes
    # through the Plex API (its file may still be on the mount, and a scan keeps
    # anything it still sees); only items the API couldn't remove fall back to
    # the one batched folder scan + trash empty.
    plex_api_ok = server_kind == 'plex'
    if server_kind == 'plex_api_fails':
        server_kind = 'plex'
    mount_root, _show_dir, season_dirs, items = _build_500_ep_show(tmp_path, create_files=False)
    dm = _make_manager(
        using_symlinks=False,
        plex_enabled=(server_kind == 'plex'),
        silo_enabled=(server_kind == 'silo'),
        jellyfin_direct_enabled=(server_kind == 'jellyfin'),
    )
    plex_instance, tv_section = _fake_plex_server(mount_root)

    def fake_get_setting(section, key, default=None):
        table = {
            ('File Management', 'file_collection_management'): 'Plex',
            ('Plex', 'url'): 'http://plex:32400',
            ('Plex', 'token'): 'tok',
            ('Plex', 'mounted_file_location'): str(mount_root),
            ('Debug', 'emby_jellyfin_url'): '',
            ('Debug', 'emby_jellyfin_token'): '',
        }
        return table.get((section, key), default)

    clyde_subs = {os.path.normpath(os.path.splitext(it['location_on_disk'])[0] + '.en.srt') for it in items}
    with mock.patch('utilities.deletion_manager.get_setting', side_effect=fake_get_setting), \
         mock.patch('database.subtitle_history.is_clyde_path', side_effect=lambda p, saved_via='sidecar': os.path.normpath(p) in clyde_subs), \
         mock.patch('utilities.plex_functions.scan_cleanup.get_setting', side_effect=fake_get_setting), \
         mock.patch('utilities.silo_functions.is_silo_selected', return_value=(server_kind == 'silo')), \
         mock.patch('utilities.direct_mount.is_direct_mount_jellyfin', return_value=(server_kind == 'jellyfin')), \
         mock.patch('utilities.direct_mount.unconfigured_server', return_value=None), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 500, 'error': None}) as mock_db_delete, \
         mock.patch('plexapi.server.PlexServer', return_value=plex_instance) as mock_plex_cls, \
         mock.patch('utilities.plex_functions.remove_file_from_plex',
                    return_value=plex_api_ok) as mock_remove_plex, \
         mock.patch('utilities.silo_functions.remove_file_from_silo') as mock_remove_silo, \
         mock.patch('utilities.silo_functions.scan_path', return_value=True) as mock_silo_scan, \
         mock.patch('utilities.jellyfin_direct.remove_file') as mock_remove_jf, \
         mock.patch('utilities.jellyfin_direct.scan_path', return_value=True) as mock_jf_scan, \
         mock.patch('utilities.deletion_manager.time.sleep') as mock_dm_sleep, \
         mock.patch('utilities.plex_functions.scan_cleanup.time.sleep') as mock_scan_sleep:

        result = dm.delete_multiple_items(
            item_ids=[it['id'] for it in items],
            delete_from_media_server=True,
            delete_symlinks=False,
            clear_cache=False,
        )

    assert result['success'] is True
    assert result['deleted_count'] == 500
    assert mock_db_delete.call_count == 1
    assert mock_remove_plex.call_count == (500 if server_kind == 'plex' else 0)
    assert mock_remove_silo.call_count == 0
    assert mock_remove_jf.call_count == 0
    assert (mock_dm_sleep.call_count + mock_scan_sleep.call_count) <= 1

    if server_kind == 'plex' and plex_api_ok:
        assert all(r['media_server_removed'] for r in result['results'])
        assert tv_section.update.call_count == 0
        assert tv_section.emptyTrash.call_count == 0
    elif server_kind == 'plex':
        assert mock_plex_cls.call_count == 1
        from utilities.plex_functions.scan_cleanup import _nearest_existing_scan_paths
        expected = _nearest_existing_scan_paths(season_dirs, [tv_section])
        assert [c.kwargs['path'] for c in tv_section.update.call_args_list] == expected
        assert 1 <= len(expected) <= 5
        assert tv_section.emptyTrash.call_count == 1
    elif server_kind == 'silo':
        assert mock_silo_scan.call_count == 5
        assert [c.args[0] for c in mock_silo_scan.call_args_list] == season_dirs
    elif server_kind == 'jellyfin':
        assert mock_jf_scan.call_count == 5
        assert [c.args[0] for c in mock_jf_scan.call_args_list] == season_dirs


def test_bulk_queue_action_delete_routes_through_delete_multiple_items():
    """Database bulk delete sends all 500 selected IDs to delete_multiple_items in one call."""
    from flask import Flask
    from routes.database_routes import database_bp

    app = Flask(__name__)
    app.secret_key = 'test'
    app.register_blueprint(database_bp, url_prefix='/database')

    fake_dm = mock.MagicMock()
    fake_dm.delete_multiple_items.return_value = {
        'success': True,
        'deleted_count': 500,
        'failed_count': 0,
        'errors': [],
        'database_locked': False,
    }

    with mock.patch('routes.models.is_user_system_enabled', return_value=False), \
         mock.patch('routes.program_operation_routes.get_program_runner', return_value=None), \
         mock.patch('utilities.deletion_manager.DeletionManager', return_value=fake_dm):
        client = app.test_client()
        resp = client.post(
            '/database/bulk_queue_action',
            data={
                'action': 'delete',
                'blacklist': 'false',
                'selected_items': [str(i) for i in range(1, 501)],
            },
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert '500' in body['message']
    assert fake_dm.delete_multiple_items.call_count == 1
    assert fake_dm.delete_multiple_items.call_args.kwargs['item_ids'] == list(range(1, 501))
    assert fake_dm.delete_multiple_items.call_args.kwargs['blacklist'] is False
    # restores what the single-item route did: cli_mount entry + file removal
    assert fake_dm.delete_multiple_items.call_args.kwargs['remove_climount_entries'] is True
    assert fake_dm.delete_multiple_items.call_args.kwargs['remove_original_files'] is True


def _batch_settings(mount_root):
    table = {
        ('Plex', 'mounted_file_location'): str(mount_root),
        ('Debug', 'emby_jellyfin_url'): '',
        ('Debug', 'emby_jellyfin_token'): '',
    }
    return lambda section, key, default=None: table.get((section, key), default)


def _run_batch(dm, items, mount_root, sibling_rows=(), **kwargs):
    """Run delete_multiple_items with all server/DB edges mocked; returns (result, climount, db, scan)."""
    conn = mock.MagicMock()
    conn.execute.return_value.fetchall.side_effect = lambda: [(i,) for i in sibling_rows]
    climount = mock.MagicMock()
    climount.is_enabled.return_value = True
    with mock.patch('utilities.deletion_manager.get_setting', side_effect=_batch_settings(mount_root)), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.get_db_connection', return_value=conn), \
         mock.patch('usenet.get_usenet_client', return_value=climount), \
         mock.patch('usenet.reset_usenet_client'), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': len(items), 'error': None}) as db, \
         mock.patch.object(dm, 'scan_and_empty_media_server_trash',
                           return_value={'success': True}) as scan, \
         mock.patch('utilities.deletion_manager.time.sleep') as sleep:
        res = dm.delete_multiple_items([i['id'] for i in items], clear_cache=False, **kwargs)
    assert sleep.call_count == 0
    return res, climount, db, scan


def _nzb_items(n, tid='nzb:abc123'):
    return [{'id': i, 'type': 'episode', 'title': 'Show', 'state': 'Collected',
             'filled_by_torrent_id': tid, 'location_on_disk': f'/mnt/debrid/shows/S/e{i}.mkv',
             'filled_by_file': f'e{i}.mkv'} for i in range(1, n + 1)]


def test_bulk_removes_climount_entry_once_per_job(tmp_path):
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(50)
    res, climount, db, scan = _run_batch(dm, items, tmp_path, remove_climount_entries=True)
    assert res['success'] is True
    assert climount.remove_nzb.call_count == 1
    assert climount.remove_nzb.call_args.args[0] == 'abc123'
    assert db.call_count == 1 and scan.call_count == 1


def test_bulk_climount_removal_uses_btih_for_torrents(tmp_path):
    dm = _make_manager(using_symlinks=False)
    h = 'a' * 40
    items = _nzb_items(3, tid='RD123')
    for it in items:
        it['filled_by_magnet'] = f'magnet:?xt=urn:btih:{h.upper()}'
    _, climount, _, _ = _run_batch(dm, items, tmp_path, remove_climount_entries=True)
    assert [c.args[0] for c in climount.remove_nzb.call_args_list] == [h]


def test_bulk_climount_removal_skipped_when_sibling_outside_batch(tmp_path):
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(5)
    # id 99 is not in the batch and still references the job; batch ids alone don't count
    res, climount, _, _ = _run_batch(dm, items, tmp_path, sibling_rows=(1, 2, 99),
                                     remove_climount_entries=True)
    assert climount.remove_nzb.call_count == 0
    res, climount, _, _ = _run_batch(dm, items, tmp_path, sibling_rows=(1, 2, 3),
                                     remove_climount_entries=True)
    assert climount.remove_nzb.call_count == 1


def test_bulk_climount_removal_off_by_default(tmp_path):
    dm = _make_manager(using_symlinks=False)
    _, climount, _, _ = _run_batch(dm, _nzb_items(3), tmp_path)
    assert climount.remove_nzb.call_count == 0


def test_bulk_direct_mount_removes_files_but_not_blacklisted_or_limited(tmp_path):
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(3)
    for it in items:
        f = tmp_path / f"{it['id']}.mkv"
        f.write_text('x')
        it['location_on_disk'] = str(f)
    items[2]['state'] = 'Blacklisted'
    res, _, db, scan = _run_batch(dm, items, tmp_path, remove_original_files=True)
    assert res['success'] is True
    assert not (tmp_path / '1.mkv').exists() and not (tmp_path / '2.mkv').exists()
    assert (tmp_path / '3.mkv').exists()  # already blacklisted: no physical ops
    assert db.call_count == 1 and scan.call_count == 1
    assert scan.call_args.kwargs['deleted_paths'] == [str(tmp_path)]

    dm.limited_env = True
    (tmp_path / '4.mkv').write_text('x')
    items = _nzb_items(1)
    items[0]['location_on_disk'] = str(tmp_path / '4.mkv')
    _run_batch(dm, items, tmp_path, remove_original_files=True)
    assert (tmp_path / '4.mkv').exists()


def test_bulk_direct_mount_file_removal_off_by_default(tmp_path):
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(1)
    f = tmp_path / 'keep.mkv'
    f.write_text('x')
    items[0]['location_on_disk'] = str(f)
    _run_batch(dm, items, tmp_path)
    assert f.exists()


def test_bulk_blacklist_updates_state_instead_of_deleting_rows(tmp_path):
    dm = _make_manager(using_symlinks=False)
    _, _, db, _ = _run_batch(dm, _nzb_items(4), tmp_path, blacklist=True)
    assert db.call_args.kwargs['blacklist'] is True and db.call_count == 1


def test_bulk_symlink_mode_removes_original_off_mount_only(tmp_path):
    symlink_root = tmp_path / 'symlinks'
    folder = symlink_root / 'TV Shows' / 'Show' / 'Season 01'
    folder.mkdir(parents=True)
    on_mount = '/mnt/debrid/x.mkv'
    off = tmp_path / 'orig.mkv'
    off.write_text('x')
    items = []
    for i, orig in enumerate([str(off), on_mount], start=1):
        link = folder / f'e{i}.mkv'
        os.symlink('/nonexistent', link)
        items.append({'id': i, 'type': 'episode', 'title': 'Show', 'state': 'Collected',
                      'location_on_disk': str(link), 'original_path_for_symlink': orig})
    dm = _make_manager(using_symlinks=True, symlink_dir=str(symlink_root))

    def gs(section, key, default=None):
        return {('File Management', 'symlinked_files_path'): str(symlink_root),
                ('Plex', 'mounted_file_location'): '/mnt/debrid'}.get((section, key), default)

    with mock.patch('utilities.deletion_manager.get_setting', side_effect=gs), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 2, 'error': None}), \
         mock.patch.object(dm, 'scan_and_empty_media_server_trash', return_value={}) as scan, \
         mock.patch('os.remove', wraps=os.remove) as rm:
        res = dm.delete_multiple_items([1, 2], clear_cache=False, remove_original_files=True)
    assert res['success'] is True
    assert not off.exists()
    assert [c.args[0] for c in rm.call_args_list if str(off) in c.args[0] or on_mount in c.args[0]] == [str(off)]
    assert scan.call_count == 1


# --- originals on a debrid mount are never os.remove'd (stale mount setting) ---

def _guard_dm(monkeypatch, mounts_text, settings):
    import builtins
    from utilities import deletion_manager as dmod
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path == '/proc/mounts':
            import io
            return io.StringIO(mounts_text)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, 'open', fake_open)
    monkeypatch.setattr(dmod, 'get_setting', lambda sec, key=None, default=None: settings.get((sec, key), default) if key else settings.get(sec, default))
    dm = dmod.DeletionManager.__new__(dmod.DeletionManager)
    dm.using_symlinks = True
    dm.limited_env = False
    return dm


def test_original_on_fuse_debrid_mount_not_removed_with_stale_setting(monkeypatch, tmp_path):
    mnt = tmp_path / 'decypharr_root' / 'mnt'
    f = mnt / '__all__' / 'Pack' / 'S01E01.mkv'
    f.parent.mkdir(parents=True)
    f.write_bytes(b'x')
    dm = _guard_dm(monkeypatch, f"clid {mnt} fuse.clid rw 0 0\n",
                   {('Plex', 'mounted_file_location'): '/mnt/zurg/__all__'})
    removed, err = dm._remove_original_file_for_batch(
        {'state': 'Collected', 'original_path_for_symlink': str(f)}, '/mnt/zurg/__all__')
    assert (removed, err) == (None, None)
    assert f.exists()


def test_local_download_on_real_disk_still_removed(monkeypatch, tmp_path):
    disk = tmp_path / 'pool'
    f = disk / 'downloads' / 'Movie.mkv'
    f.parent.mkdir(parents=True)
    f.write_bytes(b'x')
    # mergerfs / fuseblk are real disks: a Local-mode delete still removes the file
    dm = _guard_dm(monkeypatch, f"pool {disk} fuse.mergerfs rw 0 0\nsdb1 {disk} fuseblk rw 0 0\n", {})
    removed, err = dm._remove_original_file_for_batch(
        {'state': 'Collected', 'original_path_for_symlink': str(f)}, '')
    assert removed == str(f) and err is None
    assert not f.exists()


# --- Phase 0 removes torrents on providers without skip_hash_tracking ---

def test_accepts_kwarg_matches_provider_signatures():
    from utilities.deletion_manager import _accepts_kwarg

    class TorBoxLike:
        def remove_torrent(self, torrent_id, removal_reason=None):
            return True

    class RealDebridLike:
        def remove_torrent(self, torrent_id, removal_reason="Manual removal", skip_hash_tracking=False):
            return None

    class KwargsLike:
        def remove_torrent(self, torrent_id, **kw):
            return True

    assert not _accepts_kwarg(TorBoxLike().remove_torrent, 'skip_hash_tracking')
    assert _accepts_kwarg(RealDebridLike().remove_torrent, 'skip_hash_tracking')
    assert _accepts_kwarg(KwargsLike().remove_torrent, 'skip_hash_tracking')


def test_content_level_plex_delete_still_scans_and_empties_trash(tmp_path):
    # Plex answers a show delete with success but can keep the items when it
    # can't remove the files itself; when it still has the show after the
    # files are gone, Phase 3 must still scan + empty trash.
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(12)
    with mock.patch('utilities.plex_functions.remove_show_from_plex',
                    return_value={'success': True, 'rating_key': '122280'}) as show_del, \
         mock.patch('utilities.plex_functions.finish_plex_content_delete',
                    return_value=False) as finish:
        res, _, _, scan = _run_batch(dm, items, tmp_path, plex_deletion_type='show',
                                     plex_content_title='Show', plex_imdb_id='tt1')
    assert show_del.call_count == 1
    finish.assert_called_once_with('122280')
    assert res.get('plex_deleted') is True
    assert scan.call_count == 1


def test_content_level_plex_delete_confirmed_skips_scan(tmp_path):
    # Re-sent delete after the files are gone removed the show: no folder
    # scan (for a removed show folder that would be a whole-section scan).
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(12)
    with mock.patch('utilities.plex_functions.remove_show_from_plex',
                    return_value={'success': True, 'rating_key': '122280'}), \
         mock.patch('utilities.plex_functions.finish_plex_content_delete',
                    return_value=True) as finish:
        res, _, _, scan = _run_batch(dm, items, tmp_path, plex_deletion_type='show',
                                     plex_content_title='Show', plex_imdb_id='tt1')
    finish.assert_called_once_with('122280')
    assert res.get('plex_deleted') is True
    assert scan.call_count == 0


def test_content_level_400_resent_after_files_gone_skips_scan(tmp_path):
    # Plex refuses a show delete with 400 while the symlinks exist; once they
    # are gone the same delete works (seen live: This Hour Has 22 Minutes).
    dm = _make_manager(using_symlinks=False)
    items = _nzb_items(6)
    with mock.patch('utilities.plex_functions.remove_show_from_plex',
                    return_value={'success': False, 'rating_key': '113099',
                                  'error': 'Plex deletion disabled (400 Bad Request)'}), \
         mock.patch('utilities.plex_functions.finish_plex_content_delete',
                    return_value=True) as finish:
        res, _, _, scan = _run_batch(dm, items, tmp_path, plex_deletion_type='show',
                                     plex_content_title='Show', plex_imdb_id='tt1')
    finish.assert_called_once_with('113099')
    assert scan.call_count == 0
    assert not any('400' in e for e in res['errors'])
    # The delete summary reads these (it said "No items found on server")
    assert res['plex_deleted'] is True
    assert res['results'] and all(r['media_server_removed'] for r in res['results'])


def _fake_item(title='Detectorists'):
    item = mock.Mock()
    item.title = title
    return item


def test_finish_plex_content_delete_resends_when_kept():
    from plexapi.exceptions import NotFound
    from utilities.plex_functions.removal import finish_plex_content_delete
    item = _fake_item()
    plex = mock.Mock()
    plex.fetchItem.side_effect = [item, NotFound('gone')]
    assert finish_plex_content_delete('122280', plex=plex) is True
    item.delete.assert_called_once_with()


def test_finish_plex_content_delete_already_gone_no_delete():
    from plexapi.exceptions import NotFound
    from utilities.plex_functions.removal import finish_plex_content_delete
    plex = mock.Mock()
    plex.fetchItem.side_effect = NotFound('gone')
    assert finish_plex_content_delete('122280', plex=plex) is True


def test_finish_plex_content_delete_still_kept_returns_false():
    from utilities.plex_functions.removal import finish_plex_content_delete
    item = _fake_item()
    plex = mock.Mock()
    plex.fetchItem.return_value = item
    assert finish_plex_content_delete('122280', plex=plex) is False
    assert item.delete.call_count == 1
    assert finish_plex_content_delete(None, plex=plex) is False
