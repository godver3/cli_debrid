"""Regression coverage for Symlinked/Local library maintenance repairs."""
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from unittest import mock

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'library_maintenance_under_test', _ROOT / 'database' / 'maintenance.py'
)
maintenance = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(maintenance)


def _connection():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        """CREATE TABLE media_items (
            id INTEGER PRIMARY KEY,
            title TEXT,
            type TEXT,
            state TEXT,
            imdb_id TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            original_path_for_symlink TEXT,
            location_on_disk TEXT,
            version TEXT,
            last_updated TEXT
        )"""
    )
    return conn


def _insert(conn, item_id, *, location, original='/mount/source.mkv',
            state='Collected', episode=1, updated=None):
    conn.execute(
        """INSERT INTO media_items
           (id, title, type, state, imdb_id, season_number, episode_number,
            original_path_for_symlink, location_on_disk, version, last_updated)
           VALUES (?, 'Silo', 'episode', ?, 'tt14688458', 3, ?, ?, ?, '1080p', ?)""",
        (item_id, state, episode, original, location,
         updated or str(datetime.now() - timedelta(days=1)))
    )
    conn.commit()


def test_row_without_original_source_path_is_still_checked(tmp_path):
    conn = _connection()
    target = tmp_path / 'source.mkv'
    target.touch()
    link = tmp_path / 'Silo.mkv'
    link.symlink_to(target)
    _insert(conn, 1, location=link.name, original=None)

    broken, checked = maintenance.check_database_symlinks(conn.cursor(), str(tmp_path))

    assert broken == []
    assert str(tmp_path) in checked


def test_old_collected_row_without_location_is_repairable_but_recent_row_is_not():
    conn = _connection()
    _insert(conn, 1, location=None, updated=str(datetime.now() - timedelta(hours=7)))
    _insert(conn, 2, location='', episode=2, updated=str(datetime.now() - timedelta(hours=1)))

    broken, _ = maintenance.check_database_symlinks(conn.cursor(), '/library')

    assert broken == [(1, 'Silo', None, None, 'no_location', '1080p')]


def test_two_missing_database_copies_queue_one_repair_and_delete_duplicate(tmp_path):
    conn = _connection()
    _insert(conn, 1, location='missing-one.mkv')
    _insert(conn, 2, location='missing-two.mkv')
    moved = []

    def move_to_wanted(item_id, _title):
        moved.append(item_id)
        conn.execute(
            "UPDATE media_items SET state='Wanted', location_on_disk=NULL WHERE id=?",
            (item_id,)
        )
        conn.commit()

    debug_routes = types.ModuleType('routes.debug_routes')
    debug_routes.move_item_to_wanted = move_to_wanted
    routes = types.ModuleType('routes')
    routes.__path__ = []
    broken = [
        (1, 'Silo', str(tmp_path / 'missing-one.mkv'), None, 'missing_symlink', '1080p'),
        (2, 'Silo', str(tmp_path / 'missing-two.mkv'), None, 'missing_symlink', '1080p'),
    ]

    with mock.patch.dict(sys.modules, {'routes': routes, 'routes.debug_routes': debug_routes}):
        maintenance.handle_maintenance_actions(conn.cursor(), broken, [], str(tmp_path))

    assert moved == [1]
    assert conn.execute("SELECT id, state FROM media_items ORDER BY id").fetchall() == [(1, 'Wanted')]


def test_relative_location_counts_only_when_its_file_really_exists(tmp_path):
    conn = _connection()
    _insert(conn, 1, location='missing.mkv')
    _insert(conn, 2, location='present.mkv')
    (tmp_path / 'present.mkv').touch()

    maintenance.handle_maintenance_actions(
        conn.cursor(),
        [(1, 'Silo', str(tmp_path / 'missing.mkv'), None, 'missing_symlink', '1080p')],
        [],
        str(tmp_path),
    )

    assert conn.execute("SELECT id FROM media_items ORDER BY id").fetchall() == [(2,)]


def test_valid_orphaned_symlink_is_preserved(tmp_path):
    conn = _connection()
    target = tmp_path / 'source.mkv'
    target.touch()
    tracked = tmp_path / 'tracked.mkv'
    tracked.symlink_to(target)
    _insert(conn, 1, location=tracked.name)
    link = tmp_path / 'Silo (2026) - S03E01 - Episode - tt14688458 - 1080p.mkv'
    link.symlink_to(target)

    orphaned = maintenance.check_orphaned_symlinks(conn.cursor(), str(tmp_path))
    assert [item['path'] for item in orphaned] == [str(link)]

    maintenance.handle_maintenance_actions(
        conn.cursor(),
        [],
        orphaned,
        str(tmp_path),
    )

    assert link.is_symlink()
    assert link.exists()


def test_empty_library_mount_aborts_before_destructive_actions(tmp_path):
    values = {
        ('File Management', 'symlinked_files_path'): str(tmp_path),
        ('File Management', 'original_files_path'): str(tmp_path / 'source'),
    }

    def setting(section, key, default=None):
        return values.get((section, key), default)

    with mock.patch('utilities.settings.get_setting', side_effect=setting), \
         mock.patch.object(maintenance, 'handle_maintenance_actions') as actions:
        maintenance.run_symlink_library_maintenance()

    actions.assert_not_called()


def test_empty_source_mount_aborts_before_destructive_actions(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    (library / 'sentinel').touch()
    source = tmp_path / 'empty-source-mount'
    source.mkdir()
    values = {
        ('File Management', 'symlinked_files_path'): str(library),
        ('File Management', 'original_files_path'): str(source),
    }

    def setting(section, key, default=None):
        return values.get((section, key), default)

    with mock.patch('utilities.settings.get_setting', side_effect=setting), \
         mock.patch.object(maintenance, 'handle_maintenance_actions') as actions:
        maintenance.run_symlink_library_maintenance()

    actions.assert_not_called()
