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

    with mock.patch('utilities.deletion_manager.get_setting', side_effect=fake_get_setting), \
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
    assert tv_section.update.call_count == 5
    scanned = [c.kwargs['path'] for c in tv_section.update.call_args_list]
    assert scanned == season_dirs
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


@pytest.mark.parametrize('server_kind', ['plex', 'silo', 'jellyfin'])
def test_direct_mount_500_episodes_batches_media_server_calls(tmp_path, server_kind):
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

    with mock.patch('utilities.deletion_manager.get_setting', side_effect=fake_get_setting), \
         mock.patch('utilities.plex_functions.scan_cleanup.get_setting', side_effect=fake_get_setting), \
         mock.patch('utilities.silo_functions.is_silo_selected', return_value=(server_kind == 'silo')), \
         mock.patch('utilities.direct_mount.is_direct_mount_jellyfin', return_value=(server_kind == 'jellyfin')), \
         mock.patch('utilities.direct_mount.unconfigured_server', return_value=None), \
         mock.patch('database.database_reading.get_items_by_ids', return_value=items), \
         mock.patch('database.database_writing.delete_items_batch',
                    return_value={'success': True, 'deleted_count': 500, 'error': None}) as mock_db_delete, \
         mock.patch('plexapi.server.PlexServer', return_value=plex_instance) as mock_plex_cls, \
         mock.patch('utilities.plex_functions.remove_file_from_plex') as mock_remove_plex, \
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
    assert mock_remove_plex.call_count == 0
    assert mock_remove_silo.call_count == 0
    assert mock_remove_jf.call_count == 0
    assert (mock_dm_sleep.call_count + mock_scan_sleep.call_count) <= 1

    if server_kind == 'plex':
        assert mock_plex_cls.call_count == 1
        assert tv_section.update.call_count == 5
        assert [c.kwargs['path'] for c in tv_section.update.call_args_list] == season_dirs
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
