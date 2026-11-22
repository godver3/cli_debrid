"""Bulk subtitle runs, provider pausing, the Bulk subtitles task, and
skip-tried / embedded bookkeeping."""
import os
import sqlite3
import tempfile
import time
from unittest import mock

import pytest

pytest.importorskip('subliminal')


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix='subs_bulk_')
    monkeypatch.setenv('USER_DB_CONTENT', tmp)
    import database  # noqa: F401
    from database import subtitle_history
    monkeypatch.setattr(subtitle_history, '_ready', False)
    conn = sqlite3.connect(os.path.join(tmp, 'media_items.db'))
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, imdb_id TEXT, title TEXT, year INTEGER,
        state TEXT, type TEXT, episode_title TEXT, season_number INTEGER, episode_number INTEGER,
        collected_at TEXT, filled_by_file TEXT, location_on_disk TEXT, version TEXT, ms_item_id TEXT)""")
    for i in range(1, 5):
        conn.execute("INSERT INTO media_items VALUES (?, 'tt1', 'Show', 2020, 'Collected', 'episode', NULL, 1, ?, "
                     "?, 'f.mkv', ?, '1080p', ?)", (i, i, f'2026-11-0{i}', f'/lib/S01E0{i}.mkv', str(100 + i)))
    conn.commit()
    conn.close()
    return subtitle_history


def _settings(mode='Plex'):
    vals = {('File Management', 'file_collection_management'): mode,
            ('Subtitle Settings', 'subtitle_languages'): 'eng',
            ('Subtitle Settings', 'enable_subtitles'): True}
    return mock.patch('utilities.settings.get_setting', side_effect=lambda s, k, d=None: vals.get((s, k), d))


def test_missing_items_respects_history_tried_and_embedded(db):
    from utilities import subtitle_status
    db.record(1, 'x', 'en', 'gestdown', 900, 971, [], '', 'srt', 'plex', None, 'auto')
    db.record_attempt(2, False)
    db.record_attempt(3, 'embedded')
    with _settings(), mock.patch('utilities.config.downsub_config.get_setting',
                                 side_effect=lambda s, k, d=None: 'eng' if k == 'subtitle_languages' else d):
        assert sorted(r['id'] for r in subtitle_status.missing_items()) == [2, 4]
        assert [r['id'] for r in subtitle_status.missing_items(skip_tried_hours=24)] == [4]


def test_bulk_search_runs_and_reports(db):
    from utilities import subtitle_bulk, downsub
    items = [{'id': 1, 'title': 'A', 'type': 'movie'}, {'id': 2, 'title': 'B', 'type': 'movie'}]
    with mock.patch.object(downsub, 'resolve_item_path', return_value='/x.mkv'), \
            mock.patch.object(downsub, 'download_subtitles_for_video', side_effect=[True, False]), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']):
        assert subtitle_bulk.run_now(items, pause=0)
    st = subtitle_bulk.status()
    assert (st['done'], st['found'], st['running']) == (2, 1, False)
    assert [r['ok'] for r in st['recent']] == [False, True]


def test_bulk_stops_when_every_provider_paused(db):
    from utilities import subtitle_bulk, downsub
    with mock.patch.object(downsub, 'selected_providers', return_value=[]), \
            mock.patch.object(downsub, 'download_subtitles_for_video') as dl:
        subtitle_bulk.run_now([{'id': 1, 'title': 'A'}], pause=0)
    assert not dl.called and 'paused' in subtitle_bulk.status()['stopped']


def test_quota_error_pauses_provider():
    from subliminal.exceptions import DownloadLimitExceeded
    from utilities import downsub
    downsub._BLOCKED.clear()
    pool = downsub._pool_class()(providers=['opensubtitlescom'])
    try:
        raise DownloadLimitExceeded()
    except DownloadLimitExceeded:
        pool._discard_provider('opensubtitlescom')
    assert 'daily download limit' in downsub.blocked_providers()['opensubtitlescom']
    with mock.patch.object(downsub.config, 'SUBTITLE_PROVIDERS', ['opensubtitlescom', 'podnapisi']):
        assert downsub.selected_providers() == ['podnapisi']
        assert downsub.selected_providers(include_blocked=True) == ['opensubtitlescom', 'podnapisi']
    downsub._BLOCKED['opensubtitlescom'] = (time.monotonic() - 1, 'x')
    assert 'opensubtitlescom' not in downsub.blocked_providers()


def test_task_uses_missing_list(db):
    from queues.runner.library_tasks import LibraryTasks
    with _settings(), mock.patch('utilities.subtitle_status.missing_items', return_value=[{'id': 4}]) as mi, \
            mock.patch('utilities.subtitle_bulk.run_now') as run:
        LibraryTasks.task_process_bulk_subtitles(object())
    mi.assert_called_once_with(skip_tried_hours=24, limit=200)
    assert run.call_args.kwargs['kind'] == 'search'


def test_task_off_when_subtitles_disabled(db):
    from queues.runner.library_tasks import LibraryTasks
    with mock.patch('utilities.settings.get_setting', return_value=False), \
            mock.patch('utilities.subtitle_status.missing_items') as mi:
        LibraryTasks.task_process_bulk_subtitles(object())
    assert not mi.called


def test_resync_skips_checked_content(db, tmp_path):
    from utilities import subtitle_resync, downsub
    sub = tmp_path / 'S01E01.en.srt'
    sub.write_bytes(b'old')
    item = {'id': 1, 'location_on_disk': str(tmp_path / 'S01E01.mkv')}
    with mock.patch.object(downsub, 'resolve_item_path', return_value=str(tmp_path / 'S01E01.mkv')), \
            mock.patch.object(downsub, 'sync_content', return_value=(b'new', 'synced: shifted -1.00s')) as sc:
        assert subtitle_resync.resync_sidecar(item, str(sub), skip_checked=True)['changed']
        again = subtitle_resync.resync_sidecar(item, str(sub), skip_checked=True)
    assert again['skipped'] and sc.call_count == 1 and sub.read_bytes() == b'new'
