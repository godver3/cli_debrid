"""Overlay change detection: the checks that reset applied overlays to 'pending'.

Every reset is a full re-render and re-upload, so a check that recomputes a hash
from different inputs than the renderer re-renders the same posters every sync.
Live 2026-11-22: ~5,800 rows reset and ~170 posters re-uploaded per hourly sync,
from show status (renderer fell back to cli_battery, the check didn't) and from
sibling rows holding a stale Plex thumb URL.
"""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import overlays.overlay_manager as overlay_manager
import overlays.scheduled_tasks as scheduled_tasks
from overlays.overlay_manager import OverlayManager

RATINGS = {'imdb_rating': 8.5, 'tmdb_rating': 8.0, 'trakt_rating': 8.3,
           'rt_critics_score': 88, 'rt_user_score': 88}


def content_hash(ratings, status=None, version_count=None):
    return OverlayManager._compute_content_hash(status=status, version_count=version_count, **ratings)


class OverlayDbTestBase(unittest.TestCase):
    """Temp main DB (media_items, tv_shows + real overlay tables) and battery DB."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='overlay_test_')
        self.db_path = os.path.join(self.tmp, 'media_items.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript('''
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY, type TEXT, imdb_id TEXT, tmdb_id TEXT, title TEXT,
                year INTEGER, season_number INTEGER, episode_number INTEGER,
                location_on_disk TEXT, ms_item_id TEXT, state TEXT, release_date TEXT,
                ms_resolution TEXT, ms_hdr INTEGER, ms_dolby_vision INTEGER,
                ms_audio_codec TEXT, ms_audio_channels TEXT, ms_video_codec TEXT,
                ms_hdr_format TEXT, ms_media_container TEXT, ms_media_bitrate INTEGER,
                ms_network TEXT, ms_studio TEXT, ms_content_rating TEXT,
                filled_by_file TEXT, location_basename TEXT
            );
            CREATE TABLE tv_shows (imdb_id TEXT PRIMARY KEY, status TEXT);
        ''')
        conn.commit()
        conn.close()

        def _connect(db_path=None):
            c = sqlite3.connect(self.db_path, timeout=5)
            c.row_factory = sqlite3.Row
            return c

        self._connect = _connect
        patches = [
            patch('database.core.get_db_connection', _connect),
            patch.dict(os.environ, {'USER_DB_CONTENT': self.tmp}),
            patch.object(OverlayManager, '_ratings_cache', {}),
            patch.object(scheduled_tasks, '_rating_refresh_attempts', {}),
            patch.object(scheduled_tasks, 'get_setting', lambda *a, default=None, **k: default),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        from overlays.db_init import init_overlay_tables
        init_overlay_tables()

    def execute(self, sql, params=()):
        c = self._connect()
        try:
            rows = c.execute(sql, params).fetchall()
            c.commit()
            return rows
        finally:
            c.close()

    def battery(self, imdb_id, media_status):
        c = sqlite3.connect(os.path.join(self.tmp, 'cli_battery.db'))
        c.execute('CREATE TABLE IF NOT EXISTS items (imdb_id TEXT, type TEXT, media_status TEXT)')
        c.execute("INSERT INTO items VALUES (?, 'show', ?)", (imdb_id, media_status))
        c.commit()
        c.close()

    def cache_ratings(self, imdb_id, ratings, age_days=0.0):
        import json
        self.execute('INSERT OR REPLACE INTO overlay_ratings_cache (imdb_id, ratings, fetched_at) VALUES (?, ?, ?)',
                     (imdb_id, json.dumps(ratings), time.time() - age_days * 86400))

    def add_item(self, item_id, type_, imdb_id, ms_item_id, season=None, episode=None):
        self.execute('INSERT INTO media_items (id, type, imdb_id, ms_item_id, state, season_number, episode_number) '
                     "VALUES (?, ?, ?, ?, 'Collected', ?, ?)", (item_id, type_, imdb_id, ms_item_id, season, episode))

    def apply(self, item_id, content_hash_=None, thumb=None, applied_at="datetime('now', '-1 hour')"):
        self.execute(
            f"INSERT INTO media_overlay_state (media_item_id, status, reason, last_content_hash, "
            f"plex_thumb_url, overlay_applied_at, updated_at, created_at) "
            f"VALUES (?, 'applied', 'test', ?, ?, {applied_at}, {applied_at}, {applied_at})",
            (item_id, content_hash_, thumb))

    def statuses(self):
        return {r['media_item_id']: r['status'] for r in self.execute(
            'SELECT media_item_id, status FROM media_overlay_state')}


class _FakeManager:
    def __init__(self, data_by_imdb=None):
        self.calls = []
        self.data = data_by_imdb or {}

    def _fetch_ratings(self, imdb_id, item_type=None, plex_metadata=None, refresh=False):
        self.calls.append((imdb_id, item_type, refresh))
        return self.data.get(imdb_id, {})


class ShowStatusTests(OverlayDbTestBase):
    def test_tv_shows_first_then_battery(self):
        self.execute("INSERT INTO tv_shows VALUES ('tt1', 'Returning Series')")
        self.battery('tt1', 'ended')
        self.battery('tt2', 'ended')
        self.battery('tt3', 'Canceled')
        self.assertEqual(overlay_manager.show_statuses(['tt1', 'tt2', 'tt3', 'tt4', '']),
                         {'tt1': 'Returning', 'tt2': 'Ended', 'tt3': 'Canceled'})

    def test_airing_when_episode_due_within_two_weeks(self):
        self.execute("INSERT INTO tv_shows VALUES ('tt1', 'returning series')")
        self.execute("INSERT INTO media_items (id, type, imdb_id, release_date) "
                     "VALUES (1, 'episode', 'tt1', date('now', '+3 days'))")
        self.assertEqual(overlay_manager.show_statuses(['tt1']), {'tt1': 'Airing'})

    def test_renderer_uses_the_same_lookup(self):
        self.battery('tt2', 'ended')
        mgr = OverlayManager.__new__(OverlayManager)
        self.assertEqual(mgr._fetch_show_status('tt2', 'episode'), 'Ended')
        self.assertIsNone(mgr._fetch_show_status('tt2', 'movie'))


class ContentCheckTests(OverlayDbTestBase):
    def run_check(self, manager=None, limit=0):
        return scheduled_tasks._reset_content_changed_items(manager or _FakeManager(), refresh_limit=limit)

    def test_battery_only_status_does_not_loop(self):
        # The live loop: no tv_shows row, battery says ended, renderer stored 'Ended'.
        self.battery('tt1439629', 'ended')
        self.cache_ratings('tt1439629', RATINGS)
        stored = content_hash(RATINGS, status='Ended', version_count=0)
        for i, ep in enumerate((1, 2, 3), start=1):
            self.add_item(i, 'episode', 'tt1439629', '500', season=1, episode=ep)
            self.apply(i, stored)
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(set(self.statuses().values()), {'applied'})

    def test_rating_change_resets(self):
        self.add_item(1, 'movie', 'tt1', '10')
        self.apply(1, content_hash(RATINGS, version_count=1))
        self.cache_ratings('tt1', dict(RATINGS, imdb_rating=7.1))
        self.assertEqual(self.run_check(), 1)
        self.assertEqual(self.statuses()[1], 'pending')

    def test_no_ratings_at_all_is_skipped(self):
        self.add_item(1, 'movie', 'tt1', '10')
        self.apply(1, content_hash(RATINGS, version_count=1))
        self.assertEqual(self.run_check(), 0)

    def test_refresh_is_oldest_first_and_rotates(self):
        for i, imdb in enumerate(('ttA', 'ttB', 'ttC'), start=1):
            self.add_item(i, 'movie', imdb, str(i))
            self.apply(i, content_hash(RATINGS, version_count=1))
        self.cache_ratings('ttA', RATINGS, age_days=20)
        self.cache_ratings('ttB', RATINGS, age_days=30)
        self.cache_ratings('ttC', RATINGS, age_days=1)     # fresh: not due
        mgr = _FakeManager({'ttA': RATINGS, 'ttB': RATINGS})
        self.run_check(mgr, limit=1)
        self.run_check(mgr, limit=1)
        self.run_check(mgr, limit=1)
        # Oldest first, each title once, the fresh one never — even though the fake
        # doesn't persist, so ttA/ttB stay "old" in SQLite.
        self.assertEqual([c[0] for c in mgr.calls], ['ttB', 'ttA'])
        self.assertTrue(all(c[2] for c in mgr.calls), 'refresh must bypass the caches')

    def test_refreshed_ratings_are_compared(self):
        self.add_item(1, 'movie', 'tt1', '10')
        self.apply(1, content_hash(RATINGS, version_count=1))
        self.cache_ratings('tt1', RATINGS, age_days=30)
        self.run_check(_FakeManager({'tt1': dict(RATINGS, tmdb_rating=6.0)}), limit=5)
        self.assertEqual(self.statuses()[1], 'pending')

    def test_season_status_from_battery(self):
        self.battery('tt9', 'ended')
        self.cache_ratings('tt9', RATINGS)
        self.add_item(1, 'episode', 'tt9', '700', season=1, episode=1)
        self.execute("INSERT INTO season_overlay_state (show_ms_item_id, season_ms_item_id, season_number, "
                     "status, last_content_hash) VALUES ('700', '701', 1, 'applied', ?)",
                     (content_hash(RATINGS, status='Ended'),))
        self.assertEqual(self.run_check(), 0)


class SiblingPropagationTests(OverlayDbTestBase):
    def test_episode_siblings_get_the_rendered_poster_fields(self):
        for i in (1, 2, 3):
            self.add_item(i, 'episode', 'tt1', '500', season=1, episode=i)
        self.apply(1, 'content', thumb='/library/metadata/500/thumb/222')
        self.execute("UPDATE media_overlay_state SET last_metadata_hash='q', last_plex_upload_hash='up' "
                     "WHERE media_item_id=1")
        self.apply(2, 'old', thumb='/library/metadata/500/thumb/111')
        scheduled_tasks._mark_siblings_applied('500', 'episode', 1, 'content')
        rows = {r['media_item_id']: r for r in self.execute(
            'SELECT media_item_id, status, plex_thumb_url, last_metadata_hash, last_plex_upload_hash, '
            'last_content_hash FROM media_overlay_state')}
        for i in (2, 3):
            self.assertEqual(rows[i]['status'], 'applied')
            self.assertEqual(rows[i]['plex_thumb_url'], '/library/metadata/500/thumb/222')
            self.assertEqual(rows[i]['last_metadata_hash'], 'q')
            self.assertEqual(rows[i]['last_plex_upload_hash'], 'up')
            self.assertEqual(rows[i]['last_content_hash'], 'content')

    def test_merged_movie_versions_are_marked_too(self):
        self.add_item(1, 'movie', 'tt1', '76277')
        self.add_item(2, 'movie', 'tt1', '76277')
        self.add_item(3, 'movie', 'tt1', '99999')     # split item: not a sibling
        self.apply(1, 'c', thumb='/t/2')
        scheduled_tasks._mark_siblings_applied('76277', 'movie', 1, 'c')
        st = self.statuses()
        self.assertEqual(st.get(2), 'applied')
        self.assertNotIn(3, st)


class QualityTieTests(OverlayDbTestBase):
    def test_codec_tie_picks_the_same_episode_as_the_renderer(self):
        # Mr Inbetween S02 (live): 1080p DD+ 5.1 in both AVC and HEVC episodes.
        for i, codec in ((1, 'AVC'), (2, 'HEVC'), (3, 'AVC')):
            self.add_item(i, 'episode', 'tt7472896', '35129', season=2, episode=i)
            self.execute("UPDATE media_items SET ms_resolution='1080p', ms_hdr=0, ms_dolby_vision=0, "
                         "ms_audio_codec='DD+', ms_audio_channels='5.1', ms_video_codec=? WHERE id=?",
                         (codec, i))
        mgr = OverlayManager.__new__(OverlayManager)
        mgr.logger = scheduled_tasks.logger
        best = mgr._best_episode_for_season('tt7472896', 2)
        self.assertEqual(best['ms_video_codec'], 'HEVC')
        stored = OverlayManager._compute_quality_hash(
            best['ms_resolution'], best['ms_hdr'], best['ms_dolby_vision'],
            best['ms_audio_codec'], best['ms_audio_channels'], best['ms_video_codec'])
        self.execute("INSERT INTO season_overlay_state (show_ms_item_id, season_ms_item_id, season_number, "
                     "status, last_metadata_hash) VALUES ('35129', '35158', 2, 'applied', ?)", (stored,))
        self.assertEqual(scheduled_tasks._reset_quality_changed_items(), 0)


class _FakeResp:
    status_code = 200
    content = b'<MediaContainer><Directory key="1"/></MediaContainer>'


class _FakeThumbClient:
    thumbs = {}

    def __init__(self, *a, **k):
        self.session = self

    def get(self, *a, **k):
        return _FakeResp()

    def get_bulk_thumb_urls(self, section_id):
        return dict(self.thumbs)


class ThumbCheckTests(OverlayDbTestBase):
    def check(self, thumbs):
        _FakeThumbClient.thumbs = thumbs
        with patch('overlays.plex_client.PlexClient', _FakeThumbClient):
            return scheduled_tasks._reset_deselected_posters('http://plex', 'tok')

    def test_stale_sibling_url_is_not_a_poster_change(self):
        for i in (1, 2):
            self.add_item(i, 'episode', 'tt1', '75702', season=1, episode=i)
        self.apply(1, thumb='/library/metadata/75702/thumb/1792014598')
        self.apply(2, thumb='/library/metadata/75702/thumb/1792022488')
        self.assertEqual(self.check({'75702': '/library/metadata/75702/thumb/1792022488'}), 0)
        self.assertEqual(set(self.statuses().values()), {'applied'})

    def test_real_change_resets_one_row(self):
        for i in (1, 2):
            self.add_item(i, 'episode', 'tt1', '75702', season=1, episode=i)
            self.apply(i, thumb='/library/metadata/75702/thumb/1')
        self.assertEqual(self.check({'75702': '/library/metadata/75702/thumb/9'}), 1)
        self.assertEqual(sorted(self.statuses().values()), ['applied', 'pending'])

    def test_recently_applied_is_left_alone(self):
        self.add_item(1, 'movie', 'tt1', '10')
        self.apply(1, thumb='/t/1', applied_at="datetime('now')")
        self.assertEqual(self.check({'10': '/t/2'}), 0)


class NewItemWaitTests(OverlayDbTestBase):
    def test_waits_outside_the_work_slots_and_syncs_missing_keys_only(self):
        self.add_item(1, 'movie', 'tt1', None)
        calls = []

        def fake_sync(missing_only=False):
            calls.append(missing_only)
            if len(calls) == 2:
                self.execute("UPDATE media_items SET ms_item_id = '42' WHERE id = 1")

        def fake_sleep(_):
            # All three slots free while waiting
            self.assertEqual(scheduled_tasks._overlay_semaphore._value, 3)

        with patch.object(scheduled_tasks, '_sync_ms_keys_auto', fake_sync), \
                patch.object(scheduled_tasks.time, 'sleep', fake_sleep), \
                patch.object(scheduled_tasks, 'is_jellyfin_mode', lambda: False):
            self.assertTrue(scheduled_tasks._wait_for_ms_item_id(1))
        self.assertEqual(calls, [True, True])

    def test_missing_only_sync_skips_plex_when_nothing_is_missing(self):
        self.add_item(1, 'movie', 'tt1', '42')
        self.execute("UPDATE media_items SET location_on_disk='/m/a.mkv' WHERE id=1")
        with patch('overlays.plex_client.PlexClient', side_effect=AssertionError('no Plex call')):
            counts = scheduled_tasks._sync_library_keys_for_new_items('http://plex', 'tok', missing_only=True)
        self.assertEqual(counts, {'movies': 0, 'episodes': 0, 'errors': 0})


class JellyfinKeySyncTests(OverlayDbTestBase):
    def test_connection_closed_when_update_fails(self):
        self.add_item(1, 'movie', 'tt1', None)
        closed = []

        class _Cursor:
            def __init__(inner, real):
                inner.real = real

            def execute(inner, sql, params=()):
                if sql.strip().startswith('UPDATE'):
                    raise sqlite3.OperationalError('database is locked')
                return inner.real.execute(sql, params)

            def fetchone(inner):
                return inner.real.fetchone()

        class _Conn:
            def __init__(inner):
                inner.c = self._connect()

            def cursor(inner):
                return _Cursor(inner.c.cursor())

            def commit(inner):
                inner.c.commit()

            def close(inner):
                closed.append(True)
                inner.c.close()

        class _Jf:
            def __init__(self, *a):
                pass

            def get_all_items_with_guids(self, media_type):
                return [{'ratingKey': 'x' * 32, 'imdb_id': 'tt1'}]

        with patch.object(scheduled_tasks, '_get_db_connection', _Conn), \
                patch('overlays.jellyfin_client.JellyfinClient', _Jf), \
                patch.object(scheduled_tasks, 'get_jellyfin_url', lambda: ''), \
                patch.object(scheduled_tasks, 'get_jellyfin_token', lambda: ''):
            counts = scheduled_tasks._sync_library_keys_for_jellyfin()
        self.assertEqual(counts['errors'], 2)
        self.assertEqual(len(closed), 3)   # the count query + one per media type


if __name__ == '__main__':
    unittest.main()
