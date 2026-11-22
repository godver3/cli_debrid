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
    st = subtitle_bulk.status()['search']
    assert (st['done'], st['found'], st['running']) == (2, 1, False)
    assert [r['ok'] for r in st['recent']] == [False, True]


def test_bulk_stops_when_every_provider_paused(db):
    from utilities import subtitle_bulk, downsub
    with mock.patch.object(downsub, 'selected_providers', return_value=[]), \
            mock.patch.object(downsub, 'download_subtitles_for_video') as dl:
        subtitle_bulk.run_now([{'id': 1, 'title': 'A'}], pause=0)
    assert not dl.called and 'paused' in subtitle_bulk.status()['search']['stopped']


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


def test_search_time_quota_error_pauses_with_its_reason():
    """subliminal discards a provider whose search failed outside the except
    block; the pool still knows why (quota: 6 h, not a 10-min 'unknown')."""
    from subliminal.core import ProviderPool
    from subliminal.exceptions import DownloadLimitExceeded
    from subliminal.video import Episode
    from babelfish import Language
    from utilities import downsub
    downsub._BLOCKED.clear()

    class Fake:
        def list_subtitles(self, video, languages):
            raise DownloadLimitExceeded()
    pool = downsub._pool_class()(providers=['podnapisi'])
    with mock.patch.object(ProviderPool, '__getitem__', return_value=Fake()):
        assert pool.list_subtitles(Episode('Show.S01E01.mkv', 'Show', 1, 1), {Language('eng')}) == []
    assert 'daily download limit' in downsub.blocked_providers()['podnapisi']
    assert downsub._BLOCKED['podnapisi'][0] - time.monotonic() > 3600
    downsub._BLOCKED.clear()


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


def test_queue_appends_while_running(db):
    import threading
    from utilities import subtitle_bulk, downsub
    gate = threading.Event()

    def slow(path, item=None):
        gate.wait(5)
        return True
    with mock.patch.object(downsub, 'resolve_item_path', return_value='/x.mkv'), \
            mock.patch.object(downsub, 'download_subtitles_for_video', side_effect=slow), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']):
        assert subtitle_bulk.start([{'id': 1, 'title': 'A'}], pause=0)[0] == 'started'
        how, n = subtitle_bulk.start([{'id': 1, 'title': 'A'}, {'id': 2, 'title': 'B'}], pause=0)
        assert how == 'queued' and n >= 1
        gate.set()
        for _ in range(100):
            if not subtitle_bulk.status()['search']['running']:
                break
            time.sleep(0.05)
    st = subtitle_bulk.status()['search']
    assert st['done'] == st['total'] and st['found'] == st['done']


def test_history_of_replaced_file_does_not_count(db):
    from utilities import subtitle_status
    db.record(1, 'x', 'en', 'gestdown', 900, 971, [], '', 'srt', 'plex', None, 'auto', media_key='101')
    db.record(2, 'x', 'en', 'gestdown', 900, 971, [], '', 'srt', 'plex', None, 'auto', media_key='old-key')
    with _settings(), mock.patch('utilities.config.downsub_config.get_setting',
                                 side_effect=lambda s, k, d=None: 'eng' if k == 'subtitle_languages' else d):
        missing = {r['id'] for r in subtitle_status.missing_items()}
    assert 1 not in missing and 2 in missing      # item 2's file is now ratingKey 102


def test_needs_check_skips_checked_sidecars(db, tmp_path):
    import hashlib
    from utilities import subtitle_resync
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    (tmp_path / 'Show - S01E01.en.srt').write_bytes(b'subtitle')
    item = {'id': 1, 'location_on_disk': str(video), 'ms_item_id': '101'}
    with _settings():
        assert subtitle_resync.needs_check(item)
        db.mark_sync_checked(hashlib.sha1(b'subtitle').hexdigest(), 'synced by Bazarr')
        assert not subtitle_resync.needs_check(item)
        # a Plex upload by cli_debrid for another language still needs checking
        db.record(1, 'x', 'es', 'gestdown', 900, 971, [], '', 'srt', 'plex', None, 'auto', media_key='101')
        assert subtitle_resync.needs_check(item)


class _Sub:
    provider_name = 'opensubtitlescom'
    hearing_impaired = False
    subtitle_format = 'srt'

    def __init__(self, content):
        from babelfish import Language
        self.language = Language('eng')
        self.content = content


def test_sync_existing_prefers_hash_match(db, tmp_path):
    import hashlib
    from utilities import subtitle_resync, downsub
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    sc = tmp_path / 'Show - S01E01.en.srt'
    sc.write_bytes(b'old name-matched subtitle')
    item = {'id': 1, 'location_on_disk': str(video), 'title': 'Show', 'type': 'episode',
            'season_number': 1, 'episode_number': 1}
    hashed = _Sub(b'1\n00:00:01,000 --> 00:00:02,000\nexact\n')
    ctx = {'hash': True, 'video': object(), 'item': item, 'original_path': video}
    with _settings('Symlinked/Local'), \
            mock.patch.object(downsub, 'resolve_item_path', return_value=str(video)), \
            mock.patch.object(downsub, 'prepare_video', return_value=ctx), \
            mock.patch.object(downsub, 'hash_matches', return_value=[hashed]), \
            mock.patch.object(downsub, 'fetch_subtitle', return_value=True), \
            mock.patch.object(downsub, '_final_format', return_value=('srt', '.srt')), \
            mock.patch.object(downsub, '_record_history'), \
            mock.patch.object(downsub, 'sync_content') as sync:
        changed, checked, notes = subtitle_resync.resync_item(item)
    assert changed == 1 and not sync.called and 'hash match' in notes[0]
    assert sc.read_bytes() == hashed.content
    assert db.sync_checked(hashlib.sha1(hashed.content).hexdigest())


def test_fix_existing_keeps_subtitle_without_release_match(db, tmp_path):
    from utilities import subtitle_resync, downsub
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    (tmp_path / 'Show - S01E01.en.srt').write_bytes(b'old')
    item = {'id': 1, 'location_on_disk': str(video)}
    with _settings('Symlinked/Local'), \
            mock.patch.object(downsub, 'resolve_item_path', return_value=str(video)), \
            mock.patch.object(downsub, 'prepare_video', return_value={'hash': True, 'video': object()}), \
            mock.patch.object(downsub, 'hash_matches', return_value=[]), \
            mock.patch.object(downsub, 'release_matches', return_value=[]), \
            mock.patch.object(downsub, 'selected_providers', return_value=['opensubtitlescom']), \
            mock.patch.object(downsub, 'sync_content') as sync:
        changed, checked, notes = subtitle_resync.resync_item(item)
        assert not subtitle_resync.needs_check(item)          # done, skipped next time
    assert changed == 0 and checked == 1 and not sync.called    # no file reads beyond the hash
    assert notes[0].startswith('eng: kept')


def test_undecided_sync_gets_one_hash_search(db, tmp_path):
    import hashlib
    from utilities import subtitle_resync, downsub
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    sc = tmp_path / 'Show - S01E01.en.srt'
    sc.write_bytes(b'old')
    digest = hashlib.sha1(b'old').hexdigest()
    db.mark_sync_checked(digest, 'left as is: no clearly better timing found (checked against audio)')
    item = {'id': 1, 'location_on_disk': str(video)}
    with _settings('Symlinked/Local'):
        assert subtitle_resync.needs_check(item)          # owed a hash search
        with mock.patch.object(downsub, 'resolve_item_path', return_value=str(video)), \
                mock.patch.object(downsub, 'prepare_video', return_value={'hash': True, 'video': object()}), \
                mock.patch.object(downsub, 'hash_matches', return_value=[]) as hm, \
                mock.patch.object(downsub, 'release_matches', return_value=[]), \
                mock.patch.object(downsub, 'selected_providers', return_value=['opensubtitlescom']), \
                mock.patch.object(downsub, 'sync_content') as sync:
            subtitle_resync.resync_item(item)
        assert hm.called and not sync.called              # no second futile sync
        assert subtitle_resync.KEPT in db.sync_result(digest)
        with mock.patch.object(downsub, 'selected_providers', return_value=['opensubtitlescom']):
            assert not subtitle_resync.needs_check(item)



def test_search_lane_runs_while_sync_lane_busy(db):
    import threading
    from utilities import subtitle_bulk, downsub, subtitle_resync
    gate = threading.Event()

    def slow_sync(item, skip_checked=True):
        gate.wait(5)
        return 0, 1, ['en: already in sync']
    with mock.patch.object(subtitle_resync, 'resync_item', side_effect=slow_sync), \
            mock.patch.object(downsub, 'resolve_item_path', return_value='/x.mkv'), \
            mock.patch.object(downsub, 'download_subtitles_for_video', return_value=True), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']):
        assert subtitle_bulk.start([{'id': 1, 'title': 'A'}], pause=0, kind='sync')[0] == 'started'
        assert subtitle_bulk.start([{'id': 2, 'title': 'B'}], pause=0, kind='search')[0] == 'started'
        for _ in range(100):
            if not subtitle_bulk.status()['search']['running']:
                break
            time.sleep(0.05)
        assert subtitle_bulk.status()['search']['found'] == 1        # done while sync still busy
        assert subtitle_bulk.status()['sync']['running']
        gate.set()


def test_fix_existing_uses_release_match_when_no_hash(db, tmp_path):
    from utilities import subtitle_resync, downsub
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    sc = tmp_path / 'Show - S01E01.en.srt'
    sc.write_bytes(b'old name-only subtitle')
    item = {'id': 1, 'location_on_disk': str(video), 'title': 'Show', 'type': 'episode',
            'season_number': 1, 'episode_number': 1}
    rel = _Sub(b'1\n00:00:01,000 --> 00:00:02,000\nsame release\n')
    rel.provider_name = 'gestdown'
    ctx = {'hash': False, 'video': object(), 'item': item, 'original_path': video}
    with _settings('Symlinked/Local'), \
            mock.patch.object(downsub, 'resolve_item_path', return_value=str(video)), \
            mock.patch.object(downsub, 'prepare_video', return_value=ctx), \
            mock.patch.object(downsub, 'hash_matches', return_value=[]), \
            mock.patch.object(downsub, 'release_matches', return_value=[(rel, 'release match')]), \
            mock.patch.object(downsub, 'fetch_subtitle', return_value=True), \
            mock.patch.object(downsub, '_final_format', return_value=('srt', '.srt')), \
            mock.patch.object(downsub, '_record_history'), \
            mock.patch.object(downsub, 'sync_content') as sync:
        changed, checked, notes = subtitle_resync.resync_item(item)
    assert changed == 1 and not sync.called and 'release match' in notes[0]
    assert sc.read_bytes() == rel.content


def test_kept_is_searched_again_when_a_provider_is_added(db, tmp_path):
    from utilities import subtitle_resync, downsub
    video = tmp_path / 'Show - S01E01.mkv'
    video.write_bytes(b'')
    (tmp_path / 'Show - S01E01.en.srt').write_bytes(b'old')
    item = {'id': 1, 'location_on_disk': str(video)}
    patches = dict(resolve_item_path=str(video), prepare_video={'hash': True, 'video': object()},
                   hash_matches=[], release_matches=[])
    with _settings('Symlinked/Local'):
        with mock.patch.multiple(downsub, **{k: mock.DEFAULT for k in patches}) as m, \
                mock.patch.object(downsub, 'selected_providers', return_value=['opensubtitlescom', 'gestdown']):
            for k, v in patches.items():
                m[k].return_value = v
            subtitle_resync.resync_item(item)
            assert not subtitle_resync.needs_check(item)            # same providers: done
        with mock.patch.object(downsub, 'selected_providers',
                               return_value=['opensubtitlescom', 'gestdown', 'subsource']):
            assert subtitle_resync.needs_check(item)                # provider added: search again


def test_syncing_search_waits_while_something_plays(db, monkeypatch):
    from utilities import subtitle_bulk, downsub
    monkeypatch.setattr(subtitle_bulk, 'PLAYBACK_WAIT_STEP', 0.05)
    playing = {'n': 1}
    with mock.patch.object(subtitle_bulk, 'playing_sessions', side_effect=lambda: playing['n']), \
            mock.patch.object(downsub.config, 'SYNC_SUBTITLES', 'always'), \
            mock.patch.object(downsub.config, 'reload'), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']), \
            mock.patch.object(subtitle_bulk, '_search_one', return_value=(True, 'saved', True)) as one:
        assert subtitle_bulk.start([{'id': 1, 'title': 'A'}], pause=0)[0] == 'started'
        time.sleep(0.3)
        assert not one.called and 'playing' in subtitle_bulk.status()['search']['waiting']
        playing['n'] = 0
        for _ in range(100):
            if not subtitle_bulk.status()['search']['running']:
                break
            time.sleep(0.05)
    assert one.call_count == 1 and 'waiting' not in subtitle_bulk.status()['search']


def test_fix_existing_lane_ignores_playback(db):
    """Fix existing reads only the 64 KB hash: nothing to wait for."""
    from utilities import subtitle_bulk
    with mock.patch.object(subtitle_bulk, 'playing_sessions', return_value=3), \
            mock.patch.object(subtitle_bulk, '_sync_one', return_value=(True, 'fixed', True)) as one:
        subtitle_bulk.run_now([{'id': 1, 'title': 'A'}], pause=0, kind='sync')
    assert one.call_count == 1


def test_task_stops_instead_of_waiting_for_playback(db):
    from utilities import subtitle_bulk, downsub
    with mock.patch.object(subtitle_bulk, 'playing_sessions', return_value=2), \
            mock.patch.object(downsub.config, 'SYNC_SUBTITLES', 'always'), \
            mock.patch.object(downsub.config, 'reload'), \
            mock.patch.object(downsub, 'download_subtitles_for_video') as dl:
        subtitle_bulk.run_now([{'id': 1, 'title': 'A'}], pause=0)
    assert not dl.called and 'next run' in subtitle_bulk.status()['search']['stopped']


def test_search_without_sync_ignores_playback(db):
    from utilities import subtitle_bulk, downsub
    with mock.patch.object(subtitle_bulk, 'playing_sessions', return_value=2), \
            mock.patch.object(downsub.config, 'SYNC_SUBTITLES', 'off'), \
            mock.patch.object(downsub.config, 'reload'), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']), \
            mock.patch.object(downsub, 'resolve_item_path', return_value='/x.mkv'), \
            mock.patch.object(downsub, 'download_subtitles_for_video', return_value=True) as dl:
        subtitle_bulk.run_now([{'id': 1, 'title': 'A'}], pause=0)
    assert dl.call_count == 1


def test_work_queued_after_stop_still_runs(db):
    import threading
    from utilities import subtitle_bulk, downsub
    gate = threading.Event()
    done = []

    def slow(path, item=None):
        gate.wait(5)
        done.append(item['id'])
        return True
    with mock.patch.object(downsub, 'resolve_item_path', return_value='/x.mkv'), \
            mock.patch.object(downsub, 'download_subtitles_for_video', side_effect=slow), \
            mock.patch.object(downsub, 'selected_providers', return_value=['podnapisi']):
        subtitle_bulk.start([{'id': 1, 'title': 'A'}, {'id': 2, 'title': 'B'}], pause=0)
        time.sleep(0.1)
        subtitle_bulk.stop('search')                       # drops B, stops after A
        assert subtitle_bulk.start([{'id': 3, 'title': 'C'}], pause=0)[0] == 'queued'
        gate.set()
        for _ in range(100):
            if not subtitle_bulk.status()['search']['running']:
                break
            time.sleep(0.05)
    assert done == [1, 3]


def test_paused_and_music_sessions_do_not_hold_lanes(monkeypatch):
    from utilities import subtitle_bulk
    monkeypatch.setattr(subtitle_bulk, '_playing_cache', {'at': 0.0, 'n': 0})
    sessions = [{'type': 'episode', 'Player': {'state': 'paused'}},
                {'type': 'track', 'Player': {'state': 'playing'}},
                {'type': 'movie', 'Player': {'state': 'buffering'}},
                {'type': 'episode'}]
    with mock.patch('utilities.media_sessions.sessions', return_value=sessions):
        assert subtitle_bulk.playing_sessions() == 2


def test_pause_lengths_by_error_class():
    from subliminal.providers.opensubtitlescom import DownloadLimitReached, Unauthorized
    from utilities import downsub
    from utilities.subtitle_providers import RateLimited
    assert downsub._block_seconds(RateLimited('429'))[0] == 900          # brief, not the 6 h quota pause
    assert downsub._block_seconds(DownloadLimitReached('quota'))[0] == 6 * 3600
    assert downsub._block_seconds(Unauthorized('bad login'))[0] == 3600  # subclass of AuthenticationError
    assert downsub._block_seconds(None)[0] == 600


def test_broken_symlink_is_reported_as_missing(db, tmp_path):
    from utilities import downsub
    link = tmp_path / 'Show - S01E01.mkv'
    link.symlink_to(tmp_path / 'gone' / 'x.mkv')
    item = {'id': 1, 'title': 'Show', 'type': 'episode', 'season_number': 1, 'episode_number': 1}
    with _settings('Symlinked/Local'):
        assert downsub.download_subtitles_for_video(str(link), item=item) is False
    assert downsub.last_result_note(item) == 'library file is missing (broken symlink)'


def test_provider_answering_403_is_paused_not_retried():
    import requests
    from subliminal.core import ProviderPool
    from subliminal.video import Episode
    from babelfish import Language
    from utilities import downsub
    downsub._BLOCKED.clear()

    class Blocked:
        def list_subtitles(self, video, languages):
            resp = requests.Response()
            resp.status_code = 403
            raise requests.HTTPError('403 Client Error: Forbidden', response=resp)
    pool = downsub._pool_class()(providers=['tvsubtitles'])
    with mock.patch.object(ProviderPool, '__getitem__', return_value=Blocked()):
        assert pool.list_subtitles(Episode('Show.S01E01.mkv', 'Show', 1, 1), {Language('eng')}) == []
    assert 'HTTP 403' in downsub.blocked_providers()['tvsubtitles']
    assert downsub._BLOCKED['tvsubtitles'][0] - time.monotonic() > 3 * 3600
    downsub._BLOCKED.clear()
